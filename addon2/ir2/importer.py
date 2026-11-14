# -*- coding: utf-8 -*-
"""取り込みダイアログ（PBIR → Import ...）と PBIR → Check Duplicates。"""

import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import (
    QBuffer, QByteArray, QDialog, QDialogButtonBox, QEvent, QFileDialog, QFormLayout, QHBoxLayout, QImageIOHandler,
    QImageReader, QIODevice, QLabel, QLineEdit, QMenu, QObject, QPushButton, QSizePolicy, QTimer, QUrl, QWebEnginePage,
    QWebEngineSettings, QWebEngineView, QWidget, Qt,
)
from aqt.theme import theme_manager
from aqt.utils import showText, showWarning, tooltip

from . import core
from .widgets import doc_label, fill_deck_menu, menu_text


def _button(text: str, on_click=None) -> QPushButton:
    """ダイアログのボタン。Return で押されないように既定のボタンにしない。"""
    b = QPushButton(text)
    b.setAutoDefault(False)
    if on_click:
        b.clicked.connect(on_click)
    return b


class _WrapLabel(QLabel):
    """折り返す文字の欄。中身の長さでダイアログの幅を広げない。

    Qt の折り返しは空白などでしか切れないので、長い URL やファイル名がそのまま欄の最小の幅になり、左半分が
    広がってプレビューを押しつぶしていた。そこで横の大きさは中身から決めず（Ignored）、URL やパスの区切り
    （/ ? & = など）のあとに幅の無い空白（U+200B）を入れてそこで切れるようにする。それでも収まらない部分は欄で切れる。
    文書の題名などをそのまま出すので、HTML としては読まない（PlainText）。
    """

    _BREAK_AFTER = re.compile(r"([/?&=#_.,:;-])(?=\S)")

    def __init__(self, text: str = ""):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        policy = self.sizePolicy()  # setWordWrap が付けた heightForWidth を残す
        policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self.setSizePolicy(policy)
        self.setText(text)

    def setText(self, text: str) -> None:
        super().setText(self._BREAK_AFTER.sub("\\1\u200b", text))


def _warning_label() -> QLabel:
    """取り込みダイアログの重複の警告。_show_lines で出す。"""
    label = _WrapLabel()
    label.hide()
    return label


def _show_lines(label: QLabel, lines: list) -> None:
    """lines があれば 1 行ずつ出し、無ければ隠す。"""
    label.setText("\n".join(lines))
    label.setVisible(bool(lines))


# ---------------------------------------------------------------- 取り込みダイアログ

class _PreviewPage(QWebEnginePage):
    """プレビューの中のリンクを押しても、ページを移動しない。"""

    def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:
        return nav_type != QWebEnginePage.NavigationType.NavigationTypeLinkClicked


_last_dir = os.path.expanduser("~")  # ファイル選択で最後に開いたフォルダ（Anki を閉じるまで）


def _looks_like_url(text: str) -> bool:
    u = urlparse(text)
    return u.scheme.lower() in ("http", "https") and bool(u.netloc)


_BLANK = "<!doctype html><html><body></body></html>"


_HERE = os.path.dirname(__file__)


class _ImportPage:
    """取り込みの JS を動かす、見えない QWebEnginePage。Mozilla Readability 0.6.0（vendor/Readability.js、Apache-2.0。
    Firefox のリーダービューの処理）と、記事を文書の形にする import.js（Pilcrow と同じファイル）を読み込んでおく。

    call(name, args, callback) は import.js の関数 name を args で呼び、戻り値（JSON で受け取ったもの）を callback に渡す。
    動かなかったときは None を渡す。
    """

    def __init__(self, parent):
        self.page = QWebEnginePage(parent)
        self._ready = False
        self._queue = []
        self.page.loadFinished.connect(self._loaded)
        self.page.setHtml(_BLANK)

    def _loaded(self, _ok: bool) -> None:
        src = ""
        for name in ("vendor/Readability.js", "import.js"):
            with open(os.path.join(_HERE, name), encoding="utf-8") as f:
                src += f.read() + "\n"
        self.page.runJavaScript(src, lambda _r: self._set_ready())

    def _set_ready(self) -> None:
        self._ready = True
        queue, self._queue = self._queue, []
        for args in queue:
            self._run(*args)

    def call(self, name: str, args: list, callback) -> None:
        if self._ready:
            self._run(name, args, callback)
        else:
            self._queue.append((name, args, callback))

    def _run(self, name: str, args: list, callback) -> None:
        def done(result) -> None:
            try:
                callback(json.loads(result))
            except (TypeError, ValueError):
                callback(None)

        self.page.runJavaScript(f"JSON.stringify({name}(...{json.dumps(args)}))", done)


class _FileDrop(QObject):
    """付けたウィジェットに落とした手元のファイル 1 個を受け、on_drop(path) を呼ぶ（eventFilter として付ける）。

    取り込みダイアログの File の行とプレビューに付ける。
    - File の行: 中のラベルとボタンはドロップを受けないので、その上に落としたものも行に来る（Qt はカーソルの下から
      親へたどり、ドロップを受けるウィジェットを探す。qtbase 6.11 の qwidgetwindow.cpp の findDnDTarget）。
    - プレビュー: QWebEngineView は自分でもドロップを受ける（setAcceptDrops(true)。qtwebengine 6.11 の qwebengineview.cpp）
      ので、ファイルのときだけ横取りする。中の WebEngineQuickWidget はドラッグの事象を処理せずに false を返し
      （同じファイル）、QApplication::notify が親の QWebEngineView に渡す（qtbase 6.11 の qapplication.cpp）。
    """

    def __init__(self, parent, on_drop):
        super().__init__(parent)
        self._on_drop = on_drop

    def eventFilter(self, obj, event) -> bool:
        if event.type() not in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            return False
        path = _dropped_file(event.mimeData())
        if path is None:
            return False
        # 元のファイルを動かさない（Finder に移動と受け取られないように）
        if event.possibleActions() & Qt.DropAction.CopyAction:
            event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if event.type() == QEvent.Type.Drop:
            # ドロップを終えてから読む（読めないときの警告をドラッグの途中で出さない）
            QTimer.singleShot(0, lambda: self._on_drop(path))
        return True


class _Article:
    """取り込みダイアログの、取り込む文書の元（URL かファイル）1 個の状態。"""

    def __init__(self):
        self.seq = 0
        self.reset(None)

    def reset(self, origin) -> None:
        """元を替える（None なら元が無い）。seq を増やして、前の元の取り出しの結果を捨てる（ImportDialog._prepare）。"""
        self.seq += 1
        self.origin = origin   # 説明に出す名前（読み込みが済んだ URL かファイル名）
        self.prepared = None   # 取り込む文書（import.js の irImport の結果: html, paragraphs, images）
        self.error = None      # 文書を作れなかった理由


class ImportDialog(QDialog):
    """取り込みダイアログ。左半分が入力欄、右半分がプレビュー。

    貼り付けの欄があったころと同じ規則で、貼り付けの代わりにファイルを使う。
      - URL 欄は常に文書の Source になる。
      - URL を入れる（貼り付ける）と、すぐに裏で読み込む。読み込めたらプレビューに出す。
      - URL のページも HTML のファイルも、Readability で記事の本文を取り出して出す。平文（Markdown も）はそのまま。どちらも
        import.js が文書の形にする（_ImportPage、_prepare）。
      - ファイルを選ぶと、プレビューはファイルに替わる。取り込むのはファイルで、URL から
        読み込んだ文書は使わない（URL は Source としてだけ使う）。
      - ファイルを外すと（Remove）、URL から読み込んだ文書がプレビューに戻る。
      - ファイルは File の行かプレビューへのドラッグ＆ドロップでも選べる（Choose File ... と同じ扱い。_FileDrop）。
    プレビューは取り込まれる文書全体（import.js の irImport の結果）。
    OK で取り込むのはプレビューに出ている文書で、出ていなければ OK は灰色。文書の画像は media のファイルにする
    （web の画像はダウンロードしてから。accept、core.store_images、import.js の irPlaceImages）。
    Deck はボタンで、PBIR → Move Document と同じデッキのメニューから選ぶ。初期値は選択中のデッキ。
    URL が取り込み済みの文書の Source と同じなら、URL 欄の下に警告を出す。ファイルの段落が取り込み済みの文書の原本にも
    あれば、File の行の下に警告を出す（どちらも取り込みは止めない）。
    """

    FETCH_DELAY_MS = 400  # 打ち込んでいる途中で読み込みに行かないよう、入力が止まるのを待つ

    def __init__(self):
        super().__init__(mw)
        self.setWindowTitle("PBIR: Import Article")
        self._from_url = _Article()    # URL 欄の URL から読み込んだ文書（origin は読み込みが済んだ URL）
        self._from_file = _Article()   # 選んだファイル（origin はファイル名）
        self._url_error = None     # 読み込めなかった理由（今の URL のもの）
        self._fetching = None      # 読み込み中の URL
        self._shown = None         # プレビューに出している文書（_Article.prepared）
        self._ready = None         # OK で取り込む文書（プレビューに出ていて段落があるもの）
        self._closed = False
        self._preview_path = None
        root = QHBoxLayout(self)

        left = QWidget()
        form = QFormLayout(left)
        # macOS の既定（FieldsStayAtSizeHint）では URL 欄が狭いままになる。欄を左半分の幅いっぱいに広げる
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://...")
        self.url.textChanged.connect(self._url_changed)
        self.url.returnPressed.connect(self._fetch_now)
        form.addRow("URL", self.url)
        # URL 欄の URL が、取り込み済みの文書の Source と同じなら、ここに出す（_check_url_dupes）
        self.url_dupes = _warning_label()
        form.addRow("", self.url_dupes)
        self._page = _ImportPage(self)
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.timeout.connect(self._fetch_now)
        self.file_label = _WrapLabel("No file chosen")
        choose = _button("Choose File ...", self.choose_file)
        # ファイルを外すため。ファイルを選んでいるときだけ出す
        self.remove_file = _button("Remove", self.clear_file)
        self.remove_file.hide()
        self._file_drop = _FileDrop(self, self._set_file)
        file_row = QWidget()
        file_row.setAcceptDrops(True)
        file_row.installEventFilter(self._file_drop)
        row = QHBoxLayout(file_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.file_label, 1)
        row.addWidget(self.remove_file)
        row.addWidget(choose)
        form.addRow("File", file_row)
        # ファイルの段落が、取り込み済みの文書の原本にもあれば、ここに出す（_check_file_dupes）
        self.file_dupes = _warning_label()
        form.addRow("", self.file_dupes)
        # 取り込み先のデッキ。PBIR → Move Document と同じメニュー（fill_deck_menu）から選ぶ
        current = mw.col.decks.current()
        self._deck_id, self._deck_name = current["id"], current["name"]
        self.deck = _button(menu_text(self._deck_name))
        # 欄を広げる設定（AllNonFixedFieldsGrow）でも、デッキのボタンは名前の幅のまま
        self.deck.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        deck_menu = QMenu(self.deck)
        deck_menu.aboutToShow.connect(
            lambda: fill_deck_menu(deck_menu, {self._deck_id}, self._pick_deck))
        self.deck.setMenu(deck_menu)
        form.addRow("Deck", self.deck)
        self.summary = _WrapLabel()
        form.addRow(self.summary)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        # Return は URL 欄での読み込みに使う。OK を既定のボタンにしない
        for b in bb.buttons():
            b.setAutoDefault(False)
            b.setDefault(False)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

        self.preview = QWebEngineView()
        self.preview.setPage(_PreviewPage(self.preview))
        self.preview.installEventFilter(self._file_drop)
        # 記事の HTML を出すだけなので JS は動かさない（取り込みで script は落とすが、
        # onerror などの属性は残る）
        self.preview.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        root.addWidget(left, 1)
        root.addWidget(self.preview, 1)
        self.resize(1200, 760)
        self._refresh()

    # ------------------------------------------------ URL

    def _url_changed(self, text: str) -> None:
        """URL 欄が変わった。前の URL の文書は捨て、入力が止まったら読み込む。"""
        self._from_url.reset(None)
        self._url_error = self._fetching = None
        self.url_dupes.hide()
        if _looks_like_url(text.strip()):
            self._fetch_timer.start(self.FETCH_DELAY_MS)
        else:
            self._fetch_timer.stop()
        self._refresh()

    def _fetch_now(self) -> None:
        self._fetch_timer.stop()
        url = self.url.text().strip()
        if not _looks_like_url(url) or url in (self._fetching, self._from_url.origin):
            return
        self._check_url_dupes(url)
        seq = self._from_url.seq
        self._fetching = url

        def task() -> tuple:
            return core.article_html(fetch_url(url), urlparse(url).path)

        def on_done(fut) -> None:
            if self._closed or seq != self._from_url.seq:
                return  # ダイアログが閉じたか、その間に URL が変わった
            self._fetching = None
            try:
                text, kind = fut.result()
            except Exception as e:  # noqa: BLE001
                self._url_error = str(e) or repr(e)
                self._refresh()
                return
            self._from_url.origin = url
            self._prepare(self._from_url, text, kind, url)
            self._refresh()

        # 通信だけでコレクションに触らないので uses_collection=False（taskman.py の説明のとおり）
        mw.taskman.run_in_background(task, on_done, uses_collection=False)
        self._refresh()

    def _check_url_dupes(self, url: str) -> None:
        """url が取り込み済みの文書の Source と同じなら、URL 欄の下に文書ごとに 1 行で出す。取り込みは止めない。"""
        lines = [doc_label(mw.col, docid) for docid in core.docs_with_source(mw.col, url)]
        if lines:
            n = len(lines)
            lines.insert(0, f"Warning: {core.plural(n, 'document')} already "
                            f"{'has' if n == 1 else 'have'} this URL as the source.")
        _show_lines(self.url_dupes, lines)

    def _prepare(self, src: _Article, text: str, kind: str, url: str) -> None:
        """src（URL かファイル）の取り込む文書を import.js の irImport で作り、src.prepared か src.error に入れる。
        kind は core.article_html の種類で、html なら Readability で本文を取り出す。作り終わる前に src の元が替わって
        いたら（seq が違う）、結果を捨てる。ファイルなら重複の警告も出す。"""
        seq = src.seq

        def done(result) -> None:
            if self._closed or seq != src.seq:
                return
            if result is None or result.get("error"):
                src.error = result and result["error"] or "the import script did not return a result"
            else:
                src.prepared = result
                if src is self._from_file:
                    self._check_file_dupes(result)
            self._refresh()

        self._page.call("irImport", [text, kind, url], done)

    # ------------------------------------------------ file

    def choose_file(self) -> None:
        global _last_dir
        # 静的関数の QFileDialog は macOS では標準のファイル選択（NSOpenPanel）を使う
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an article to import", _last_dir,
            "Web pages, text and Markdown (*.html *.htm *.xhtml *.txt *.text *.md *.markdown);;All files (*)")
        if not path:
            return
        _last_dir = os.path.dirname(path)
        self._set_file(path)

    def _pick_deck(self, deck_id: int, name: str) -> None:
        self._deck_id, self._deck_name = deck_id, name
        self.deck.setText(menu_text(name))

    def _set_file(self, path: str) -> None:
        """path を取り込むファイルにする。Choose File ... とドロップの両方から。"""
        try:
            text, kind = core.read_article_file(path)
        except Exception as e:  # noqa: BLE001
            showWarning(f"PBIR: could not read the file: {e!r}", parent=self)
            return
        self._from_file.reset(os.path.basename(path))
        self.file_label.setText(self._from_file.origin)
        self.remove_file.show()
        self.file_dupes.hide()
        self._prepare(self._from_file, text, kind, "")
        self._refresh()

    MAX_DUPE_LINES = 5  # ファイルの重複の警告に出す文書の数。残りは数だけ出す

    def _check_file_dupes(self, prepared: dict) -> None:
        """ファイルの段落が取り込み済みの文書の原本にもあれば、File の行の下に、文書ごとに共通の段落の数を出す
        （core.shared_paragraphs。全部が同じでなくても出す）。取り込みは止めない。"""
        texts = core.prepared_texts(prepared)
        found = core.shared_paragraphs(mw.col, texts)
        if not found:
            _show_lines(self.file_dupes, [])
            return
        total = len(texts)
        n = len(found)
        lines = [f"Warning: paragraphs of this file are already in {core.plural(n, 'document')}."]
        for docid, k in found[:self.MAX_DUPE_LINES]:
            lines.append(f"{k} of {core.plural(total, 'paragraph')}: {doc_label(mw.col, docid)}")
        if n > self.MAX_DUPE_LINES:
            lines.append(f"... and {n - self.MAX_DUPE_LINES} more (PBIR \u2192 Check Duplicates lists them all after import).")
        _show_lines(self.file_dupes, lines)

    def clear_file(self) -> None:
        """ファイルを外す。URL から読み込んだ文書があれば、それがプレビューに戻る。"""
        self._from_file.reset(None)
        self.file_label.setText("No file chosen")
        self.remove_file.hide()
        self.file_dupes.hide()
        self._refresh()

    # ------------------------------------------------ preview

    def _url_status(self) -> str:
        url = self.url.text().strip()
        if not url:
            return ""
        if self._fetching or self._fetch_timer.isActive():
            return f"Loading {url} ..."
        if self._url_error:
            return f"Could not load {url}: {self._url_error}"
        return ""

    def _refresh(self) -> None:
        """今の状態に合わせてプレビュー、説明、OK を揃える。

        プレビューに出すのは、ファイルを選んでいればファイル、いなければ URL から読み込んだ文書。
        """
        has_file = self._from_file.origin is not None
        src = self._from_file if has_file else self._from_url
        prepared, origin, error = src.prepared, src.origin, src.error
        waiting = None  # プレビューに出すものが無いときの説明
        if prepared is None and origin is not None:
            waiting = (f"Could not extract the article from {origin}: {error}." if error else
                       f"Extracting the article from {origin} ...")
        lines = []
        self._ready = None
        if prepared is None:
            self._show_preview(None)
            lines.append(waiting or self._url_status() or
                         "Paste a URL, or choose or drop a file. The article is shown on the right "
                         "as it will be imported.")
        else:
            n = len(prepared["paragraphs"])
            if not n:
                self._show_preview(None)
                lines.append(f"Cannot import {origin}: no paragraphs found in the article.")
            else:
                self._ready = prepared
                if self._shown is not prepared:
                    self._show_preview(core.preview_html(prepared, night=theme_manager.night_mode), prepared)
                lines.append(f"Showing {origin}: {core.plural(n, 'paragraph')}, {core.plural(n, 'card')}.")
            if has_file and self.url.text().strip():
                lines.append("The file is imported. The URL is recorded as the source; "
                             "the page loaded from it is not imported.")
                if self._url_status():
                    lines.append(self._url_status())
        self.summary.setText("\n".join(lines))
        self.ok.setEnabled(self._ready is not None)

    def _show_preview(self, page, prepared=None) -> None:
        """page を右に出す。None なら空にする。prepared はその元の文書（同じものを出し直さないため）。"""
        if page is None:
            if self._shown is None and self._preview_path is not None:
                return
            page = _BLANK
        self._shown = prepared
        # setHtml は 2 MB を超える内容を出せない（data: URL にして読み込むため。Qt の
        # QWebEngineView::setHtml の説明）。画像を埋め込んだ記事は超えるので、一時ファイルに書いて読み込む
        self._remove_preview_file()
        fd, self._preview_path = tempfile.mkstemp(prefix="ir-preview-", suffix=".html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        self.preview.load(QUrl.fromLocalFile(self._preview_path))

    def _remove_preview_file(self) -> None:
        if self._preview_path:
            try:
                os.remove(self._preview_path)
            except OSError:
                pass
            self._preview_path = None

    def done(self, r: int) -> None:
        self._closed = True
        self._fetch_timer.stop()
        self._remove_preview_file()
        super().done(r)

    def accept(self) -> None:
        """OK でプレビューに出ている文書を取り込む。Source は URL 欄。閉じるのは成功したときだけ。

        まず文書の web の画像を裏でダウンロードし（fetch_images。その間はダイアログを触れない）、済んだら画像を media の
        ファイルにして（core.store_images）、その名前と大きさを文書に入れ（import.js の irPlaceImages）、取り込む（_import）。
        失敗したら警告を出してダイアログを開いたままにし、入力をそのまま直せるようにする。
        """
        if self._ready is None:
            return
        prepared, deck, source = self._ready, self._deck_name, self.url.text().strip()
        # 画像を頼むときの Referer は記事のページ。ファイルを取り込むときは付けない
        referer = "" if self._from_file.origin is not None else self._from_url.origin
        urls = [src for src in prepared["images"] if not src.startswith("data:")]
        self.setEnabled(False)
        QueryOp(parent=self, op=lambda _col: fetch_images(urls, referer),
                success=lambda fetched: self._place(prepared, deck, source, fetched)
                ).failure(self._failed).without_collection().with_progress(
                    "Downloading the article's images").run_in_background()

    def _place(self, prepared: dict, deck: str, source: str, fetched: dict) -> None:
        if self._closed:
            return
        docid = core.new_docid()
        try:
            placed = core.store_images(mw.col, docid, prepared["images"], fetched,
                                       size_of=lambda data: image_info(data)[1])
        except Exception as e:  # noqa: BLE001
            self._failed(e)
            return

        def done(result) -> None:
            if result is None:
                self._failed(RuntimeError("the import script could not place the images"))
            else:
                self._import(docid, result, deck, source)

        self._page.call("irPlaceImages", [prepared["html"], prepared["paragraphs"], placed], done)

    def _import(self, docid: str, prepared: dict, deck: str, source: str) -> None:
        if self._closed:
            return
        try:
            n = core.import_article(mw.col, docid, prepared, deck, source_url=source)
        except Exception as e:  # noqa: BLE001
            self._failed(e)
            return
        super().accept()
        mw.reset()
        tooltip(f"Imported {n} paragraph notes (doc {docid})")

    def _failed(self, e: Exception) -> None:
        self.setEnabled(True)
        showWarning(f"PBIR: import failed: {e!r}", parent=self)


def _dropped_file(mime):
    """ドラッグされているものが手元のファイル 1 個なら、そのパス。そうでなければ None。

    macOS の Finder からのドラッグは public.file-url で、Qt が text/uri-list のファイルの URL にする
    （qtbase 6.11 の src/gui/platform/darwin/qutimimeconverter.mm の QMacMimeFileUri）。
    """
    if mime is None or not mime.hasUrls():
        return None
    urls = mime.urls()
    if len(urls) != 1 or not urls[0].isLocalFile():
        return None
    path = urls[0].toLocalFile()
    return path if os.path.isfile(path) else None


_USER_AGENT = "Mozilla/5.0 (Anki PBIR addon)"
# URL の中で % 表記にしない ASCII の記号（区切りと、すでに % 表記になっている部分の %）。英数字と _ . - ~ は quote が残す
_URL_SAFE = "!#$%&'()*+,/:;=?@[]"


def ascii_url(url: str) -> str:
    """url を ASCII だけの形にする。ホスト名の ASCII でない文字は Punycode に、ほかの ASCII でない文字と空白は UTF-8 の
    % 表記にする（ブラウザがアドレスを送るときと同じ）。すでに ASCII だけの URL は変わらない。

    urlopen は ASCII でない文字や空白のある URL を読めない（https://ja.wikipedia.org/wiki/猫 は UnicodeEncodeError、
    空白は InvalidURL。Anki 26.09.2 の Python 3.13 で確かめた）。日本語のページのアドレスや、記事の中の画像のアドレスには
    そういうものがある。Referer のヘッダも同じ（ヘッダは latin-1 でしか送れない）。
    """
    parts = urlsplit(url.strip())
    netloc = parts.netloc
    if not netloc.isascii():
        try:
            netloc = netloc.encode("idna").decode("ascii")
        except UnicodeError:
            netloc = quote(netloc, safe=_URL_SAFE)
    return urlunsplit((parts.scheme, netloc, *(quote(p, safe=_URL_SAFE) for p in parts[2:])))


def _request(url: str, referer: str = "") -> Request:
    """url を読むための Request。url と referer は ASCII の形にする（ascii_url）。"""
    headers = {"User-Agent": _USER_AGENT}
    if referer:
        headers["Referer"] = ascii_url(referer)
    return Request(ascii_url(url), headers=headers)


def fetch_url(url: str) -> str:
    """URL を読み、文字列にする。文字コードの決め方は core.decode_text（ファイルと同じ）。"""
    with urlopen(_request(url), timeout=30) as r:
        ctype = (r.headers.get_content_type() or "").lower()
        data = r.read()
        # 平文（種類か拡張子で。ファイルと同じく拡張子でも決める）では <meta> を探さない
        is_html = ctype != "text/plain" and not urlparse(url).path.lower().endswith(core.PLAIN_TEXT_EXTS)
        return core.decode_text(data, is_html=is_html, http_charset=r.headers.get_content_charset())


def fetch_images(urls: list, referer: str) -> dict:
    """urls の画像を並行してダウンロードする。戻り値はアドレス → (中身, MIME)（core.store_images の fetched）。
    取れなかった画像は入らない（文書では web のアドレスのまま残る）。"""
    with ThreadPoolExecutor(max_workers=8) as pool:
        images = pool.map(lambda url: _fetch_image(url, referer), urls)
        return {url: image for url, image in zip(urls, images) if image}


def _fetch_image(url: str, referer: str):
    """url の画像の (中身, MIME)。2xx の答えで（urlopen はほかの答えで HTTPError を出す）、MIME が image/* か、
    Qt が画像として読めるものだけ。それ以外は None。"""
    try:
        with urlopen(_request(url, referer), timeout=30) as r:
            mime, data = r.headers.get_content_type().lower(), r.read()
    except Exception:  # noqa: BLE001  画像が取れなくても取り込みは続ける
        return None
    if not mime.startswith("image/"):
        fmt = image_info(data)[0]
        mime = "image/" + fmt if fmt else ""
    return (data, mime) if data and mime else None


def image_info(data: bytes) -> tuple:
    """Qt が data のヘッダから読む画像の形式（jpeg、png など。画像として読めなければ空文字）と、表示される向きの
    (幅, 高さ)（分からなければ None）。画像の中身は読まない。

    QImageReader.size() は EXIF の向きを直す前の大きさなので（Anki 26.09.3 の Qt 6.11 で確かめた）、向き 5〜8（90 度回す）は
    幅と高さを入れ替える。SVG は大きさを返さない。Qt は width と height の無い SVG にも viewBox から大きさを
    返すが（viewBox="0 0 40 10" なら 40×10）、Chromium はその SVG を列の幅で出す（Qt WebEngine 6.11 で確かめた）。
    """
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf)
    fmt, size = bytes(reader.format()).decode(), reader.size()
    if fmt == "svg" or size.isEmpty():
        return fmt, None
    w, h = size.width(), size.height()
    if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
        w, h = h, w
    return fmt, (w, h)


def on_import() -> None:
    ImportDialog().exec()


# ---------------------------------------------------------------- Check Duplicates

def check_duplicates() -> None:
    """PBIR → Check Duplicates。取り込み済みの文書どうしの重複を調べ、結果を文字で出す。見るのはアドオンのノートだけ
    （core.library_duplicates。比べ方は取り込みダイアログの警告と同じ）。原本を全部読むので裏で動かす。何も変えない。"""
    QueryOp(parent=mw, op=_duplicates_report,
            success=lambda text: showText(text, parent=mw, title="PBIR: Duplicates", copyBtn=True)
            ).with_progress("Checking documents for duplicates").run_in_background()


def _duplicates_report(col) -> str:
    r = core.library_duplicates(col)
    n = r["docs"]
    out = [f"Checked {core.plural(n, 'document')} (only notes of the add-on's note types).", ""]
    if not r["same_source"] and not r["shared"]:
        out.append("No duplicates found.")
        return "\n".join(out)
    if r["same_source"]:
        out.append(f"Same source URL ({len(r['same_source'])}):")
        for src, docids in r["same_source"]:
            out.append(f"  {src}")
            out += [f"    {doc_label(col, d)}" for d in docids]
        out.append("")
    if r["shared"]:
        out.append(f"Shared paragraphs ({core.plural(len(r['shared']), 'pair')}):")
        for a, b, k in r["shared"]:
            out.append(f"  {core.plural(k, 'paragraph')} in common:")
            for d in (a, b):
                out.append(f"    {doc_label(col, d)} ({r['paras'][d]} paragraphs)")
        out.append("")
    out.append("To see a document's cards, search for DocId:<doc> in the Browser.")
    return "\n".join(out)
