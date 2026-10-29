# -*- coding: utf-8 -*-
"""取り込みダイアログ（PBIR → Import ...）と PBIR → Check Duplicates。"""

import json
import os
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from aqt import mw
from aqt.operations import QueryOp
from aqt.qt import (
    QDialog, QDialogButtonBox, QEvent, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QObject,
    QPushButton, QTimer, QUrl, QWebEnginePage, QWebEngineSettings, QWebEngineView, QWidget, Qt,
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


def _warning_label() -> QLabel:
    """取り込みダイアログの重複の警告。_show_lines で出す。"""
    label = QLabel()
    label.setWordWrap(True)
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


_VENDOR = os.path.join(os.path.dirname(__file__), "vendor")

# Readability を動かす関数。ページの HTML を DOMParser で読むので、ページのスクリプトは動かず、
# 画像も読み込まれない（不活性な文書）。<base> に URL を入れておくと、Readability が相対 URL を
# 絶対 URL に直す（Readability.js の _fixRelativeUris は document.baseURI を使う）。
# Readability は題、著者、日付を本文とは別に返す（parse() の title, byline, publishedTime）。
# Firefox のリーダービューと同じく、それを本文の前に置く: 題は h1、著者と日付は 1 行の div（カードにならない）。
_EXTRACT_JS = r"""
function irExtract(html, url) {
  try {
    var doc = new DOMParser().parseFromString(html, 'text/html');
    if (url) {
      var base = doc.createElement('base');
      base.setAttribute('href', url);
      (doc.head || doc.documentElement).prepend(base);
    }
    var art = new Readability(doc).parse();
    if (!art) return JSON.stringify({error: 'no article was found'});
    // 結果を組み立てる文書も不活性にする（画像を読まず、イベント属性を動かさない）
    var out = document.implementation.createHTMLDocument('');
    out.body.innerHTML = art.content;
    var date = /^\d{4}-\d{2}-\d{2}/.exec(art.publishedTime || '');
    var line = [art.byline, date && date[0]].filter(function (s) { return s && s.trim(); }).join(', ');
    if (line) {
      var by = out.createElement('div');
      by.textContent = line;
      out.body.prepend(by);
    }
    if (art.title) {
      var h = out.createElement('h1');
      h.textContent = art.title;
      out.body.prepend(h);
    }
    return JSON.stringify({content: out.body.innerHTML});
  } catch (e) {
    return JSON.stringify({error: String(e && e.message || e)});
  }
}
"""


class _ArticleExtractor:
    """ページから記事の本文だけを取り出す。Mozilla Readability 0.6.0（vendor/Readability.js、
    Apache-2.0。Firefox のリーダービューの処理）を、見えない QWebEnginePage で動かす。

    extract(html, url, callback) の callback は (本文の HTML, 失敗の理由) のどちらか片方を受け取る。
    """

    def __init__(self, parent):
        self.page = QWebEnginePage(parent)
        self._ready = False
        self._queue = []
        self.page.loadFinished.connect(self._loaded)
        self.page.setHtml(_BLANK)

    def _loaded(self, _ok: bool) -> None:
        with open(os.path.join(_VENDOR, "Readability.js"), encoding="utf-8") as f:
            src = f.read()
        self.page.runJavaScript(src + "\n" + _EXTRACT_JS, lambda _r: self._set_ready())

    def _set_ready(self) -> None:
        self._ready = True
        queue, self._queue = self._queue, []
        for args in queue:
            self._run(*args)

    def extract(self, html: str, url: str, callback) -> None:
        if self._ready:
            self._run(html, url, callback)
        else:
            self._queue.append((html, url, callback))

    def _run(self, html: str, url: str, callback) -> None:
        def done(result) -> None:
            try:
                r = json.loads(result)
            except (TypeError, ValueError):
                callback(None, "the extractor did not return a result")
                return
            callback(r.get("content"), r.get("error"))

        self.page.runJavaScript(f"irExtract({json.dumps(html)}, {json.dumps(url)})", done)


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
        """元を替える（None なら元が無い）。seq を増やして、前の元の取り出しの結果を捨てる（ImportDialog._extract）。"""
        self.seq += 1
        self.origin = origin   # 説明に出す名前（読み込みが済んだ URL かファイル名）
        self.html = None       # 取り出した記事の本文（平文と Markdown は HTML にしたもの）
        self.error = None      # 本文を取り出せなかった理由


class ImportDialog(QDialog):
    """取り込みダイアログ。左半分が入力欄、右半分がプレビュー。

    貼り付けの欄があったころと同じ規則で、貼り付けの代わりにファイルを使う。
      - URL 欄は常に文書の Source になる。
      - URL を入れる（貼り付ける）と、すぐに裏で読み込む。読み込めたらプレビューに出す。
      - URL のページも HTML のファイルも、Readability で記事の本文を取り出して（_ArticleExtractor）出す。
        平文と Markdown はアドオンが HTML に起こすので、そのまま出す。
      - ファイルを選ぶと、プレビューはファイルに替わる。取り込むのはファイルで、URL から
        読み込んだ文書は使わない（URL は Source としてだけ使う）。
      - ファイルを外すと（Remove）、URL から読み込んだ文書がプレビューに戻る。
      - ファイルは File の行かプレビューへのドラッグ＆ドロップでも選べる（Choose File ... と同じ扱い。_FileDrop）。
    プレビューは取り込まれる文書全体（カードに分ける前の形。core.prepare_article の結果）。
    OK で取り込むのはプレビューに出ている文書で、出ていなければ OK は灰色。
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
        self._shown = None         # プレビューに出している HTML
        self._ready_html = None    # OK で取り込む HTML（プレビューに出ていて段落があるもの）
        self._closed = False
        self._preview_path = None
        root = QHBoxLayout(self)

        left = QWidget()
        form = QFormLayout(left)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://...")
        self.url.textChanged.connect(self._url_changed)
        self.url.returnPressed.connect(self._fetch_now)
        form.addRow("URL", self.url)
        # URL 欄の URL が、取り込み済みの文書の Source と同じなら、ここに出す（_check_url_dupes）
        self.url_dupes = _warning_label()
        form.addRow("", self.url_dupes)
        self._extractor = _ArticleExtractor(self)
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.timeout.connect(self._fetch_now)
        self.file_label = QLabel("No file chosen")
        self.file_label.setWordWrap(True)
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
        deck_menu = QMenu(self.deck)
        deck_menu.aboutToShow.connect(
            lambda: fill_deck_menu(deck_menu, {self._deck_id}, self._pick_deck))
        self.deck.setMenu(deck_menu)
        form.addRow("Deck", self.deck)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
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
                html, plain = fut.result()
            except Exception as e:  # noqa: BLE001
                self._url_error = str(e) or repr(e)
                self._refresh()
                return
            self._from_url.origin = url
            self._extract(self._from_url, html, url, plain)
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

    def _extract(self, src: _Article, html: str, url: str, plain: bool) -> None:
        """src（URL かファイル）の取り込む文書を作り、src.html か src.error に入れる。HTML は Readability で本文を取り出す。
        平文と Markdown はアドオンが HTML に起こしたもので、取り出す本文も周りも無いので、そのまま使う。
        取り出しが終わる前に src の元が替わっていたら（seq が違う）、結果を捨てる。ファイルなら重複の警告も出す。"""
        seq = src.seq

        def done(content, error) -> None:
            if self._closed or seq != src.seq:
                return
            if content is not None:
                src.html = content
                if src is self._from_file:
                    self._check_file_dupes(content)
            else:
                src.error = error or "unknown error"
            self._refresh()

        if plain:
            done(html, None)
        else:
            self._extractor.extract(html, url, done)

    # ------------------------------------------------ file

    def choose_file(self) -> None:
        global _last_dir
        # 静的関数の QFileDialog は macOS では標準のファイル選択（NSOpenPanel）を使う
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an article to import", _last_dir,
            "Web pages, text and Markdown (*.html *.htm *.xhtml *.txt *.md *.markdown);;All files (*)")
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
            html, plain = core.read_article_file(path)
        except Exception as e:  # noqa: BLE001
            showWarning(f"PBIR: could not read the file: {e!r}", parent=self)
            return
        self._from_file.reset(os.path.basename(path))
        self.file_label.setText(self._from_file.origin)
        self.remove_file.show()
        self.file_dupes.hide()
        self._extract(self._from_file, html, "", plain)
        self._refresh()

    MAX_DUPE_LINES = 5  # ファイルの重複の警告に出す文書の数。残りは数だけ出す

    def _check_file_dupes(self, html: str) -> None:
        """ファイルの段落が取り込み済みの文書の原本にもあれば、File の行の下に、文書ごとに共通の段落の数を出す
        （core.shared_paragraphs。全部が同じでなくても出す）。取り込みは止めない。"""
        try:
            _soup, units = core.prepare_article(html)
        except ValueError:  # 段落が無い。取り込めないことは _refresh が出す
            units = []
        found = core.shared_paragraphs(mw.col, units)
        if not found:
            _show_lines(self.file_dupes, [])
            return
        total = len(core.para_texts(units))
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
        html, origin, error = src.html, src.origin, src.error
        waiting = None  # プレビューに出すものが無いときの説明
        if html is None and origin is not None:
            waiting = (f"Could not extract the article from {origin}: {error}." if error else
                       f"Extracting the article from {origin} ...")
        lines = []
        self._ready_html = None
        if html is None:
            self._show_preview(None)
            lines.append(waiting or self._url_status() or
                         "Paste a URL, or choose or drop a file. The article is shown on the right "
                         "as it will be imported.")
        else:
            try:
                soup, units = core.prepare_article(html)
            except ValueError as e:
                self._show_preview(None)
                lines.append(f"Cannot import {origin}: {e}.")
            else:
                self._ready_html = html
                if self._shown is not html:
                    self._show_preview(core.preview_html(soup, night=theme_manager.night_mode), html)
                n = len(units)
                lines.append(f"Showing {origin}: {core.plural(n, 'paragraph')}, {core.plural(n, 'card')}.")
            if has_file and self.url.text().strip():
                lines.append("The file is imported. The URL is recorded as the source; "
                             "the page loaded from it is not imported.")
                if self._url_status():
                    lines.append(self._url_status())
        self.summary.setText("\n".join(lines))
        self.ok.setEnabled(self._ready_html is not None)

    def _show_preview(self, page, html=None) -> None:
        """page を右に出す。None なら空にする。html はその元の文書（同じものを出し直さないため）。"""
        if page is None:
            if self._shown is None and self._preview_path is not None:
                return
            page = _BLANK
        self._shown = html
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

        失敗したら警告を出してダイアログを開いたままにし、入力をそのまま直せるようにする。
        """
        if self._ready_html is None:
            return
        try:
            docid, n = core.import_article(
                mw.col, self._ready_html, self._deck_name,
                source_url=self.url.text().strip(),
            )
        except Exception as e:  # noqa: BLE001
            showWarning(f"PBIR: import failed: {e!r}", parent=self)
            return
        super().accept()
        mw.reset()
        tooltip(f"Imported {n} paragraph notes (doc {docid})")


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


def fetch_url(url: str) -> str:
    """URL を読み、文字列にする。文字コードの決め方は core.decode_text（ファイルと同じ）。"""
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Anki PBIR addon)"})
    with urlopen(req, timeout=30) as r:
        ctype = (r.headers.get_content_type() or "").lower()
        data = r.read()
        # 平文と Markdown では <meta> を探さない
        is_html = ctype != "text/plain" and not core.is_markdown_name(urlparse(url).path)
        return core.decode_text(data, is_html=is_html, http_charset=r.headers.get_content_charset())


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
