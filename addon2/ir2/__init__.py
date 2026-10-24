# -*- coding: utf-8 -*-
"""Paragraph-Based Incremental Reading: aqt との接続層。

コアのロジックは core.py（aqt 非依存）。ここにあるのは
取り込みダイアログ、reviewer からの pycmd の受け口、メニューだけ。
"""

import html as html_lib
import json
import os
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from anki.errors import NotFoundError
from anki.utils import is_mac, no_bundled_libs
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QAction, QDesktopServices, QDialog, QDialogButtonBox, QEvent, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMenu, QObject, QPushButton, QTimer, QUrl, QWebEnginePage,
    QWebEngineSettings, QWebEngineView, QWidget, Qt,
)
from aqt.theme import theme_manager
from aqt.utils import openLink, showText, showWarning, tooltip

from . import core


def _menu_text(name: str) -> str:
    """Qt の項目とボタンの文字。& は Qt では次の文字の印になるので && にする。"""
    return name.replace("&", "&&")


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


# ---------------------------------------------------------------- import UI

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
        # 取り込み先のデッキ。PBIR → Move Document と同じメニュー（_fill_deck_menu）から選ぶ
        current = mw.col.decks.current()
        self._deck_id, self._deck_name = current["id"], current["name"]
        self.deck = _button(_menu_text(self._deck_name))
        deck_menu = QMenu(self.deck)
        deck_menu.aboutToShow.connect(
            lambda: _fill_deck_menu(deck_menu, {self._deck_id}, self._pick_deck))
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
        lines = [_doc_label(mw.col, docid) for docid in core.docs_with_source(mw.col, url)]
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
        self.deck.setText(_menu_text(name))

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
            lines.append(f"{k} of {core.plural(total, 'paragraph')}: {_doc_label(mw.col, docid)}")
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


def _doc_label(col, docid: str) -> str:
    """重複の警告と PBIR → Check Duplicates で、文書を見分けるための 1 行。
    取り込んだ日時（DocId は取り込んだ時刻のミリ秒の 16 進。core.new_docid）、デッキ、DocId、原本の最初の見出しか段落。"""
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(docid, 16) / 1000))
    except ValueError:
        when = "?"
    decks = ", ".join(sorted(col.decks.name(did) for did in core.doc_decks(col, docid)))
    snippet = core.doc_snippet(col, docid)
    return f"imported {when} into {decks}, doc {docid}" + (f": {snippet}" if snippet else "")


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
            out += [f"    {_doc_label(col, d)}" for d in docids]
        out.append("")
    if r["shared"]:
        out.append(f"Shared paragraphs ({core.plural(len(r['shared']), 'pair')}):")
        for a, b, k in r["shared"]:
            out.append(f"  {core.plural(k, 'paragraph')} in common:")
            for d in (a, b):
                out.append(f"    {_doc_label(col, d)} ({r['paras'][d]} paragraphs)")
        out.append("")
    out.append("To see a document's cards, search for DocId:<doc> in the Browser.")
    return "\n".join(out)


# ---------------------------------------------------------------- reviewer bridge

def on_js_message(handled, message: str, context):
    if not message.startswith("ir2:"):
        return handled
    try:
        _, cmd, rest = message.split(":", 2)
        docid, anchor, payload = rest.split(":", 2)
        inner_html = unquote(payload)
        if cmd == "save":
            n, msgs = core.save_edit(mw.col, docid, anchor, inner_html)
            tooltip("<br>".join([f"Saved ({n} notes regenerated)"] + msgs))
            # 保存したあとの段落の HTML。Source Text の表示に使う（使わない呼び出しでは捨てられる）
            return (True, core.para_source(mw.col, docid, anchor))
        elif cmd == "source":
            # PBIR → Source Text: 段落の HTML（文書に書かれているとおり）
            return (True, core.para_source(mw.col, docid, anchor))
        elif cmd == "font":
            # PBIR → Font と Create → Cloze のチェック。段落の選択範囲が変わったときと、Font と Cloze の操作のあとに届く
            st = json.loads(inner_html)
            _font.update({k: bool(st.get(k)) for k in _CHECK_KEYS})
            _sync_menu()
        elif cmd == "note":
            # reviewer 側からの短い通知。黙って失敗させないためのもの。
            tooltip(inner_html)
        elif cmd == "state":
            # カードの JS が準備できたとき、問いと答えの編集の開始・終了などのたびに届く
            st = json.loads(inner_html)
            _state.update({k: type(v)(st.get(k) or v) for k, v in _STATE_DEFAULT.items()}, ready=True)
            _sync_menu()
            # カードの準備ができたときに JS が Source Text を続けるかどうかを受け取る
            return (True, {"source": _source_text})
        elif cmd == "cloze":
            # PBIR → Create → Cloze。選択範囲の data-cloze マーク 1 個とノート 1 件。
            # ノートの Text は段落の中のその語の出現をすべて隠す
            cloze_id, word, n = core.make_cloze(mw.col, docid, anchor, inner_html, _deck_id())
            _discard_undo()
            tooltip(f"Cloze created ({cloze_id}: {core.plural(n, 'occurrence')} "
                    f"of \"{html_lib.escape(word)}\")")
            # JS 側は受け取った id で DOM の PENDING を置き換える
            return (True, cloze_id)
        elif cmd == "draft":
            # Basic、True or False、Multiple Choice、Enumeration の、編集中のフィールドの今の中身。表示中のカードのノートとデッキを
            # 一緒に覚える。保存するのはカードを離れるときと PBIR → Create → Edit のチェックを外したとき（_commit_draft）
            card = mw.reviewer.card
            _draft.clear()
            _draft.update(json.loads(inner_html), docid=docid, anchor=anchor, nid=card.nid, did=card.did)
        elif cmd == "extract":
            # 選択の始まりと終わりの印の入った段落の中身
            ids = core.extract_paragraph(mw.col, docid, anchor, inner_html, _deck_id())
            parts = [f"the text {where} the selection is now {i}" for where, i in zip(("before", "after"), ids) if i]
            _after_structure_change(f"Extract: {' and '.join(parts)} "
                                    f"({'its card is' if len(parts) == 1 else 'their cards are'} at the end of the new cards)")
            return (True, "ok")
        elif cmd == "merge":
            direction, _, html = inner_html.partition(":")
            core.save_edit(mw.col, docid, anchor, html)   # 先に今の中身を保存する
            other, n_moved, n_del = core.merge_paragraph(mw.col, docid, anchor, direction)
            _after_structure_change(
                f"Merged {other} into {anchor}"
                + (f", moved {core.plural(n_moved, 'note')} to {anchor}" if n_moved else "")
                + (", deleted its paragraph note" if n_del else ""))
            return (True, "ok")
        else:
            return handled
    except core.Refused as e:
        tooltip(f"{cmd[:1].upper() + cmd[1:]} failed: {e}")
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: operation failed: {e!r}")
    return (True, None)


def _commit_draft() -> None:
    """編集中の問いと答えの下書き（ir:draft）を、下書きを書いたカードのノートに合わせて保存し、下書きを捨てる。

    カードを離れるとき（評価したとき、ほかのカードに替わるとき、学習画面を終えるとき。on_answer_card と _reset_state）と、
    PBIR → Create → Edit のチェックを外したとき（_toggle_qa_edit）に呼ぶ。
    ノートが段落のノートなら（Create → Basic など）同じ段落に新しいノートを作り、Basic、True or False、Multiple Choice のノートなら
    そのフィールドを書き換え（Create → Edit）、Enumeration のノートなら捨てて同じ段落に作り直す（Create → Recreate Enumeration。
    core.recreate_derived）。空のフィールドもそのまま保存する。文書は変えないので undo の履歴は空にしない。
    下書きを書いたあとにそのノートが消されていれば（Anki の Delete Note など）何もしない。
    """
    d = dict(_draft)
    _draft.clear()
    if not d or mw.col is None:
        return
    try:
        note = mw.col.get_note(d["nid"])
    except NotFoundError:
        return
    kind, ntype = d["kind"], note.note_type()["name"]
    name = core.DERIVED[kind][1]
    recreate = ntype == core.ENUM_MODEL
    try:
        if recreate:
            core.recreate_derived(mw.col, d["docid"], d["anchor"], kind, note.id, d["fields"], d["did"])
        else:
            core.save_derived(mw.col, d["docid"], d["anchor"], kind, None if ntype == core.PARA_MODEL else note.id,
                              d["fields"], d["did"])
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: could not save the {name} card: {e!r}")
        return
    # 表示中のカードがこのノートなら読み直す。Card は描画を覚えていて、読み直さないと古い問いのまま出る（pylib で確かめた）
    card = mw.reviewer.card
    if card and card.nid == note.id and not recreate:
        card.load()
    mw.update_undo_actions()
    tooltip(f"{name} recreated" if recreate else "Saved" if ntype != core.PARA_MODEL
            else f"{name} {'cards' if kind == 'enum' else 'card'} created")


def _deck_id() -> int:
    card = mw.reviewer.card
    return card.did if card else mw.col.decks.current()["id"]


def _discard_undo() -> None:
    """文書とノートの両方を変えたあと。Anki の undo はノートだけを戻し、文書（media）は
    戻さないので、履歴ごと空にする（core.discard_undo）。Edit メニューの Undo も灰色にする。"""
    core.discard_undo(mw.col)
    mw.update_undo_actions()


def _after_structure_change(msg: str) -> None:
    """Extract と結合のあと。undo の履歴を空にし、表示中のカードのデータを読み直す。

    描画し直しはカードの JS が行う（"ok" を受け取ると文書を読み直す。core._doc_js の render）。
    Anki の mw.reviewer._redraw_current_card() は使わない。問題面では _showQuestion を通り、下の欄の時計を 0 に戻し
    （reviewer-bottom.ts の showQuestion が time = 0）、音声を流し直し、自動で答えへ進む待ち時間を始め直す（26.09.2 のソース）。
    card.load() はノートとカードのデータを読み直すだけで、回答時間の起点（timer_started）には触らない
    （pylib/anki/cards.py の _load_from_backend_card）。
    """
    _discard_undo()
    tooltip(msg)
    if mw.state == "review" and mw.reviewer.card:
        mw.reviewer.card.load()


def on_answer_card(_reviewer, card, _ease) -> None:
    """カードを評価したあと、次のカードを出す前（26.09.2 の reviewer.pyc の _after_answering は
    reviewer_did_answer_card を呼んでから nextCard を呼ぶ）。まず、編集中の問いと答えの下書きを保存する（_commit_draft。
    次のカードを出すときの _reset_state でも保存するが、下の後始末が段落を消す前に保存するため、ここで呼ぶ）。
    次に、評価したカードの文書から、中身の無い段落と
    その段落のノート、段落のノートが消された段落の cloze などのノート、cloze の無い cloze のノート、
    ノートの無い cloze のマークを消す（core.remove_empty）。

    文書を変えたとき（段落かマークを消したとき）は、cloze などと同じく undo の履歴を空にする（評価も取り消せなくなる）。
    ノートだけを消したときは空にしない（Undo で戻るのはノートだけで、文書と食い違わない）。
    ここで例外を出すと nextCard が呼ばれず、次のカードが出ないので、すべて受け止めて知らせる。
    """
    try:
        note = card.note()
        if note.note_type()["name"] not in core.IR_MODELS:
            return
        _commit_draft()
        docid = note["DocId"]
        if not os.path.exists(core.doc_path(mw.col, docid)):
            return
        paras, n, marks = core.remove_empty(mw.col, docid)
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: could not remove empty paragraphs and cards: {e!r}")
        return
    if paras or marks:
        _discard_undo()
    elif n:
        mw.update_undo_actions()
    if paras or n or marks:
        tooltip("Removed " + ", ".join(
            ([f"{core.plural(len(paras), 'empty paragraph')} ({', '.join(paras)})"] if paras else [])
            + ([core.plural(n, "note")] if n else [])
            + ([f"{core.plural(marks, 'cloze mark')} without a note"] if marks else [])))


def on_answer_buttons(buttons, _reviewer, card):
    """アドオンのノートタイプのカードでは、回答ボタンの Again（評価 1）を Not Yet と書く。評価の番号は変えない。
    26.09.2 の reviewer.pyc の _answerButtonList は、(評価, ラベル) の組を reviewer_will_init_answer_buttons に通し、
    返されたものでボタンを作る。Show Answer のボタンには通さない。
    """
    if card.note_type()["name"] not in core.IR_MODELS:
        return buttons
    return tuple((ease, "Not Yet" if ease == 1 else label) for ease, label in buttons)


# ---------------------------------------------------------------- PBIR menu
#
# メニューバーの PBIR メニュー。項目にキーショートカットは付けない。
# 付けると Qt が webview より先にキーを取り、編集中の文字入力を奪うため。

# card はカードの種類と面（段落のカードは "para:q" と "para:a"、Basic などのカードは "basic:a" や "enum:q" など）。
# derived は、問いと答えを出していること（Basic、True or False、Multiple Choice、Enumeration のカードか、段落のカードで Create → Basic などを開いている間）。
# editing は、問いと答えの編集中のこと（Create → Edit のチェックと、Create → Basic などと Recreate Enumeration で開いている間）
_STATE_DEFAULT = {"ready": False, "card": "", "editing": False, "derived": False, "source": False, "prev": False, "next": False}
_state = dict(_STATE_DEFAULT)
# 編集中の問いと答えの下書き（ir:draft の {kind, fields} と、docid, anchor, 書いたカードの nid と did）。保存するのは _commit_draft
_draft: dict = {}
# PBIR → Source Text のチェック。カードが替わっても続く（Anki を閉じるまで）
_source_text = False
# PBIR → Font のチェック。表示中の段落の選択範囲に付いている装飾（JS が ir:font で知らせる）
_FONT_KEYS = ("highlight", "bold", "italic", "subscript", "superscript")
# PBIR → Create の項目のうち、段落から作るもの
_CREATE_KEYS = ("cloze",) + tuple(core.DERIVED)
# 選択範囲で決まるチェック。Font の項目と、Create → Cloze（選択範囲の文字がすべて cloze の箇所にあるか。JS が ir:font で知らせる）
_CHECK_KEYS = _FONT_KEYS + ("cloze",)
_font = dict.fromkeys(_CHECK_KEYS, False)
_actions: dict = {}
_menu = None  # Python 側で参照を持っておかないと Qt のオブジェクトが消える
_move_menu = None  # PBIR → Move Document。開くたびにデッキの一覧で作り直す（_fill_move_menu）


def _reset_state(*_args) -> None:
    """カードの描画が替わった、または学習画面を離れたとき。JS からの通知で再び有効になる。下書きは保存する（_commit_draft）。"""
    _commit_draft()
    _state.update(_STATE_DEFAULT)
    _font.update(dict.fromkeys(_CHECK_KEYS, False))
    _sync_menu()


def _on_state_change(new_state, _old_state) -> None:
    if new_state != "review":
        _reset_state()


def _available() -> bool:
    return mw.state == "review" and _state["ready"]


def _sync_menu() -> None:
    if not _actions:
        return
    ok = _available()
    card, editing = (_state["card"], _state["editing"]) if ok else ("", False)
    # 描画された段落に対する操作は、問いと答えを出している間（Create → Basic など）と Source Text の間は使えない
    para = card.startswith("para:") and not _state["derived"]
    rendered = para and not _state["source"]
    front, back = rendered and card == "para:q", rendered and card == "para:a"   # 問題面では段落を編集できる
    # Create → Edit は Basic、True or False、Multiple Choice のカードの回答面だけ
    qa_card = card in ("basic:a", "tf:a", "mc:a")
    _actions["source_text"].setEnabled(para)
    # setChecked は triggered を出さない（メニューの操作はクリックのときだけ）
    for key, checked in (("source_text", _source_text), ("qa_edit", qa_card and editing),
                         *((k, ok and _font[k]) for k in _CHECK_KEYS)):
        _actions[key].setChecked(checked)
    # 段落から作るのは回答面だけ。Enumeration のカードの回答面では Enumeration の項目が Recreate Enumeration になる
    for key in _CREATE_KEYS:
        _actions[key].setEnabled(back or (key == "enum" and card == "enum:a" and not editing))
    _actions["enum"].setText("Recreate Enumeration" if card.startswith("enum:") else "Enumeration")
    _actions["qa_edit"].setEnabled(qa_card)
    # Create の項目がどれも使えないときは Create を灰色にする
    _show_submenu("create", any(_actions[k].isEnabled() for k in _CREATE_KEYS + ("qa_edit",)))
    for key in _FONT_KEYS + ("extract",):
        _actions[key].setEnabled(front)
    _actions["merge_prev"].setEnabled(front and _state["prev"])
    _actions["merge_next"].setEnabled(front and _state["next"])
    # Open Document の項目はノートと文書のファイルから決めるので、JS の準備を待たない（cloze カードでも使える）
    note = _current_ir_note()
    _actions["open_source"].setEnabled(bool(_source_url(note)))
    _actions["open_original"].setEnabled(bool(_doc_file(note, orig=True)))
    _actions["open_current"].setEnabled(bool(_doc_file(note, orig=False)))
    _actions["edit_source"].setEnabled(bool(_doc_file(note, orig=False)))
    _show_submenu("move_doc", note is not None)


def _show_submenu(key: str, ok: bool) -> None:
    """下位メニュー key を、使えるときは出し、使えないときは隠して同じ名前の灰色の普通の項目（key_off）を出す。

    macOS のメニューバーでは、下位メニューを持つ項目は無効にしても灰色にならない（Qt 6.11.0 の qcocoamenu.mm の
    setAttachedItem: 下位メニューの項目は NSMenuValidation に加わらず、enabled = hasSubmenu。無効にすると中の項目だけが
    灰色になる。実機のデッキ一覧で見つかった）。
    """
    _actions[key].setEnabled(ok)
    _actions[key].setVisible(ok)
    _actions[key + "_off"].setVisible(not ok)


def _current_ir_note():
    """学習画面で表示中のカードがアドオンのノート（段落、cloze、Basic、True or False、Multiple Choice、Enumeration）なら、そのノートを返す。"""
    if mw.state != "review" or not mw.reviewer or not mw.reviewer.card:
        return None
    note = mw.reviewer.card.note()
    if note.note_type()["name"] not in core.IR_MODELS:
        return None
    return note


def _docid(note) -> str:
    return note["DocId"].strip() if note else ""


def _source_url(note) -> str:
    """ノートの文書の Source（core.doc_source）。http か https の URL でなければ空文字。"""
    url = core.doc_source(mw.col, _docid(note)) if _docid(note) else ""
    return url if urlparse(url).scheme.lower() in ("http", "https") else ""


def _doc_file(note, orig: bool) -> str:
    """ノートの文書のファイル（orig なら原本、そうでなければ作業コピー）。無ければ空文字。"""
    docid = _docid(note)
    if not docid:
        return ""
    path = core.doc_path(mw.col, docid, orig=orig)
    return path if os.path.exists(path) else ""


def open_source() -> None:
    """PBIR → Open Document → Source。文書の Source の URL を既定のブラウザで開く。"""
    url = _source_url(_current_ir_note())
    if not url:
        tooltip("This note has no source URL")
        return
    openLink(url)  # QDesktopServices.openUrl。http(s) は既定のブラウザに渡る


def open_document(orig: bool) -> None:
    """PBIR → Open Document → Original（orig）と Current。文書を既定のブラウザで表示する。

    Original は取り込み時に保存した原本（_ir2_<docid>.orig.html）、Current は今の作業コピー
    （_ir2_<docid>.html。編集、線、cloze の印を含む）。どちらのファイルも変更しない。
    文書には文字コードの宣言が無く、ブラウザによっては UTF-8 と判定せずに文字化けするため、
    一時フォルダに <meta charset="utf-8"> と <base>（画像を media フォルダから読むため）を
    付けた表示用の HTML を書き、文書の中身を続けて、それを開く。
    """
    note = _current_ir_note()
    path = _doc_file(note, orig)
    if not path:
        tooltip(f"The {'original' if orig else 'current'} document for this note was not found")
        return
    with open(path, encoding="utf-8") as f:
        body = f.read()
    docid = _docid(note)
    kind = "Original" if orig else "Current"
    base = QUrl.fromLocalFile(mw.col.media.dir() + os.sep).toString(
        QUrl.ComponentFormattingOption.FullyEncoded)
    view_dir = os.path.join(tempfile.gettempdir(), "anki-ir")
    os.makedirs(view_dir, exist_ok=True)
    view_path = os.path.join(view_dir, f"{docid}.{kind.lower()}.html")
    with open(view_path, "w", encoding="utf-8") as f:
        f.write(
            '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
            f'<base href="{html_lib.escape(base, quote=True)}">'
            f"<title>{kind}: {html_lib.escape(docid)}</title></head><body>\n"
            # 文書は変えないが、表示用の複製からはスクリプトを動かしうるものを外す（core.sanitize）
            + core.sanitize_html(body) + "\n</body></html>\n"
        )
    open_in_browser(QUrl.fromLocalFile(view_path).toString(QUrl.ComponentFormattingOption.FullyEncoded))


class SourceDialog(QDialog):
    """PBIR → Modify Document → Add/Edit Source のダイアログ。URL の欄と Cancel, Save のボタンだけ。

    exec() で開くので、閉じるまで Anki のほかのウィンドウ（学習画面を含む）は操作を受けない
    （Qt の QDialog の説明: exec() は既定で application modal）。学習画面はそのまま残る。
    """

    def __init__(self, url: str):
        super().__init__(mw)
        self.setWindowTitle("PBIR: Source")
        form = QFormLayout(self)
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("https://...")
        self.url.setMinimumWidth(480)
        form.addRow("URL", self.url)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


def edit_source() -> None:
    """PBIR → Modify Document → Add/Edit Source。表示中のカードの文書の Source を書き換える。

    欄の初期値は文書の Source（core.doc_source。無ければ空）。Save で文書の先頭のコメントを書き換える
    （core.set_doc_source）。ノートは変えないので、undo の履歴は空にしない。
    学習画面は描画し直さない（カードの表示に Source は出ない）。
    """
    note = _current_ir_note()
    if not _doc_file(note, orig=False):
        tooltip("The document for this note was not found")
        return
    docid = _docid(note)
    dlg = SourceDialog(core.doc_source(mw.col, docid))
    if not dlg.exec():
        return
    try:
        changed = core.set_doc_source(mw.col, docid, dlg.url.text())
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: could not save the source: {e!r}")
        return
    _sync_menu()
    tooltip("Source saved" if changed else "Source unchanged")


def _fill_move_menu() -> None:
    """PBIR → Move Document を開く直前に、デッキの一覧で作り直す（_fill_deck_menu）。
    チェックは文書のカードの元のデッキ。
    """
    docid = _docid(_current_ir_note())
    if not docid:
        _fill_deck_menu(_move_menu, set(), None)
        return
    _fill_deck_menu(_move_menu, core.doc_decks(mw.col, docid),
                    lambda did, name: move_document(docid, did, name))


def _fill_deck_menu(menu, checked: set, on_pick) -> None:
    """menu をデッキの一覧で作り直す。PBIR → Move Document と取り込みダイアログの Deck で使う。

    on_pick(deck_id, name) は項目を選んだときに呼ぶ。None なら menu を空にするだけ。
    一覧は Anki の Change Deck と同じく、フィルターデッキを除き、空の Default を除く
    （26.09.2 の studydeck.pyc の all_names_and_ids(include_filtered=..., skip_empty_default=True)）。
    フィルターデッキには Anki がカードを移させない（FilteredDeckError）。
    デッキの階層（名前の ::）どおりに入れ子にする。下位のデッキを持つデッキは下位メニューになり、その先頭の項目が
    そのデッキ自身（選べる）、区切り線のあとに下位のデッキが並ぶ。項目の名前は階層の最後の部分。
    チェックは checked のデッキ。下位メニューを開かなくても分かるよう、その上の階層の下位メニューにもチェックを付ける。
    """
    # clear は項目を消すが、前に作った下位メニュー（menu の子の QMenu）は残るので、先に消す
    for m in menu.findChildren(QMenu):
        m.deleteLater()
    menu.clear()
    if on_pick is None:
        return
    decks = mw.col.decks.all_names_and_ids(include_filtered=False, skip_empty_default=True)
    parents = {d.name.rpartition("::")[0] for d in decks}  # 下位のデッキを持つデッキの名前
    on_path = set()  # チェックの付くデッキと、その上の階層の名前
    for d in decks:
        if d.id in checked:
            parts = d.name.split("::")
            on_path.update("::".join(parts[:i]) for i in range(1, len(parts) + 1))
    menus = {"": menu}

    def label(name: str) -> str:
        return _menu_text(name.rpartition("::")[2])

    def submenu(path: str):
        if path not in menus:
            m = submenu(path.rpartition("::")[0]).addMenu(label(path))
            m.menuAction().setCheckable(True)
            m.menuAction().setChecked(path in on_path)
            menus[path] = m
        return menus[path]

    for d in decks:
        where = submenu(d.name if d.name in parents else d.name.rpartition("::")[0])
        act = QAction(label(d.name), where)
        act.setMenuRole(QAction.MenuRole.NoRole)
        act.setCheckable(True)
        act.setChecked(d.id in checked)
        act.triggered.connect(lambda _=False, did=d.id, name=d.name: on_pick(did, name))
        where.addAction(act)
        if d.name in parents:
            where.addSeparator()


def move_document(docid: str, deck_id: int, name: str) -> None:
    """PBIR → Move Document → デッキ。文書のカードをそのデッキへ移す（core.move_document）。学習画面はそのまま。

    文書は変えずカードのデッキだけを変えるので、undo の履歴は空にしない（Undo で Change Deck が戻る）。
    表示中のカードは読み直す。覚えている did が古いままだと、このあと作る cloze などが元のデッキに入る（_deck_id）。
    """
    try:
        n = core.move_document(mw.col, docid, deck_id)
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: could not move the document: {e!r}")
        return
    if not n:
        tooltip(f"The document is already in {html_lib.escape(name)}")
        return
    if mw.reviewer.card:
        mw.reviewer.card.load()
    mw.update_undo_actions()
    tooltip(f"Moved {core.plural(n, 'card')} to {html_lib.escape(name)}")


def new_deck() -> None:
    """PBIR → New Deck。空のデッキ PBIR（あれば PBIR 2, ...）と下位のデッキを作る（core.create_pbir_deck）。

    Anki の操作（CollectionOp）として行うので、デッキの一覧の画面は操作の結果（OpChanges）で描き直され、Edit の Undo も
    合わせられる。
    """
    made = {}

    def op(col):
        made["name"], changes = core.create_pbir_deck(col)
        return changes

    CollectionOp(parent=mw, op=op) \
        .success(lambda _changes: tooltip(f"Created the deck {html_lib.escape(made['name'])}")).run_in_background()


# macOS の既定のブラウザを NSWorkspace で調べて、そのアプリで開く。
# file:// の URL を普通に開くと、ブラウザではなく .html の既定アプリ（エディタなど）で
# 開かれることがあるため。CPython の main の webbrowser.py（MacOSXOSAScript）と同じ方法。
# Anki が同梱する Python 3.13 の webbrowser にはこの処理がまだ無い。
_MAC_OPEN_IN_BROWSER = """
use framework "AppKit"
use AppleScript version "2.4"
use scripting additions

property NSWorkspace : a reference to current application's NSWorkspace
property NSURL : a reference to current application's NSURL

set http_url to NSURL's URLWithString:"https://example.com"
set browser_url to (NSWorkspace's sharedWorkspace)'s URLForApplicationToOpenURL:http_url
set app_path to browser_url's relativePath as text

tell application app_path
    activate
    open location "%s"
end tell
"""


def open_in_browser(url: str) -> None:
    if is_mac:
        script = _MAC_OPEN_IN_BROWSER % url.replace('"', "%22")
        try:
            with no_bundled_libs():
                r = subprocess.run(["/usr/bin/osascript", "-"], input=script,
                                   text=True, capture_output=True, timeout=30)
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    with no_bundled_libs():
        QDesktopServices.openUrl(QUrl(url))


def _run(cmd: str, arg=None) -> None:
    if not _available():
        return
    web = mw.reviewer.web
    web.setFocus()  # メニューを閉じたあとキー入力が webview に戻るように
    web.eval(f"window._ir2Cmd && window._ir2Cmd({json.dumps(cmd)}, {json.dumps(arg)});")


def _toggle_source_text(checked: bool) -> None:
    """PBIR → Source Text。チェックはカードをまたいで続き、JS は次のカードの準備ができたときに受け取る。"""
    global _source_text
    _source_text = bool(checked)
    _run("source", _source_text)


def _toggle_qa_edit(checked: bool) -> None:
    """PBIR → Create → Edit（Basic、True or False、Multiple Choice のカードの回答面）。チェックを入れると問いと答えの編集を始める。
    外すと下書きを保存し（_commit_draft）、編集を終える。"""
    if not checked:
        _commit_draft()
    _run("edit", bool(checked))


def _font_cmd(cmd: str) -> None:
    """PBIR → Font の項目と Create → Cloze。Qt はクリックした時点でチェックを反転するので、いったん今の状態に戻す。
    操作で装飾か cloze が変わったら、JS が ir:font で新しい状態を知らせる（断られたときは変わらない）。"""
    _run(cmd)
    _sync_menu()


def _add(menu, key: str, label: str, handler, checkable: bool = False):
    """項目を足す。checkable なら handler にクリックのあとのチェックの状態を渡し、そうでなければ引数なしで呼ぶ。"""
    act = QAction(label, mw)
    # macOS は項目名から About や Preferences などをアプリメニューへ移すことがある。
    # それを止める。
    act.setMenuRole(QAction.MenuRole.NoRole)
    act.setCheckable(checkable)
    act.triggered.connect(handler if checkable else lambda _checked=False: handler())
    menu.addAction(act)
    _actions[key] = act
    return act


def _add_submenu(menu, key: str, title: str):
    """下位メニューと、使えないときに代わりに出す灰色の普通の項目（_show_submenu）を足す。"""
    sub = menu.addMenu(title)
    _actions[key] = sub.menuAction()
    _add(menu, key + "_off", title, lambda: None).setEnabled(False)
    return sub


def setup_menu() -> None:
    global _menu, _move_menu
    menu = QMenu("PBIR", mw)
    _add(menu, "source_text", "Source Text", _toggle_source_text, checkable=True)
    create = _add_submenu(menu, "create", "Create")
    # Cloze は、選択範囲の文字がすべて cloze の箇所にあるとチェックが付き、選ぶとその cloze を外す（Font → Highlight と同じ）
    _add(create, "cloze", "Cloze", lambda _checked: _font_cmd("cloze"), checkable=True)
    for key, label in [(k, v[1]) for k, v in core.DERIVED.items()]:
        _add(create, key, label, lambda k=key: _run(k))
    _add(create, "qa_edit", "Edit", _toggle_qa_edit, checkable=True)
    font = menu.addMenu("Font")
    # 普通のエディタと同じく、選択範囲に付いている装飾にチェックが付く（_sync_menu）
    for key, label in zip(_FONT_KEYS, ("Highlight", "Bold", "Italic", "Subscript", "Superscript")):
        _add(font, key, label, lambda _checked, k=key: _font_cmd(k), checkable=True)
    _add(menu, "extract", "Extract", lambda: _run("extract"))
    _add(menu, "merge_prev", "Merge Previous", lambda: _run("merge", "prev"))
    _add(menu, "merge_next", "Merge Next", lambda: _run("merge", "next"))
    opendoc = menu.addMenu("Open Document")
    _add(opendoc, "open_source", "Source", open_source)
    _add(opendoc, "open_original", "Original", lambda: open_document(orig=True))
    _add(opendoc, "open_current", "Current", lambda: open_document(orig=False))
    moddoc = menu.addMenu("Modify Document")
    _add(moddoc, "edit_source", "Add/Edit Source", edit_source)
    _move_menu = _add_submenu(menu, "move_doc", "Move Document")
    _move_menu.aboutToShow.connect(_fill_move_menu)
    _add(menu, "new_deck", "New Deck", new_deck)
    _add(menu, "import", "Import ...", on_import)
    _add(menu, "check_dupes", "Check Duplicates", check_duplicates)
    # 開く直前にも合わせる。Open Document などの項目は文書のファイルの有無で決まり、カードの JS からの知らせを待たない
    for m in (menu, create, font, opendoc, moddoc):
        m.aboutToShow.connect(_sync_menu)
    mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    _menu = menu
    _sync_menu()


# ---------------------------------------------------------------- setup

def setup() -> None:
    setup_menu()
    gui_hooks.webview_did_receive_js_message.append(on_js_message)
    gui_hooks.reviewer_did_show_question.append(_reset_state)
    gui_hooks.reviewer_did_show_answer.append(_reset_state)
    gui_hooks.reviewer_will_end.append(_reset_state)
    gui_hooks.reviewer_did_answer_card.append(on_answer_card)
    gui_hooks.reviewer_will_init_answer_buttons.append(on_answer_buttons)
    gui_hooks.state_did_change.append(_on_state_change)


# プロファイルを開くたびに、アドオンのノートタイプのテンプレートと CSS を今の内容にする。Anki はアドオンを読み込んでから
# プロファイルを開き、そのあとで main_window_did_init を呼ぶ（26.09.2 の aqt/main.py の AnkiQt.__init__ と on_window_init）ので、
# ここで登録する
gui_hooks.profile_did_open.append(lambda: core.update_models(mw.col))
gui_hooks.main_window_did_init.append(setup)
