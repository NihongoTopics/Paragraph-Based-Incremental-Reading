# -*- coding: utf-8 -*-
"""表示中のカードの文書に対する PBIR メニューの操作（Open Document、Modify Document、Move Document）と New Deck。"""

import html as html_lib
import os
import subprocess
import tempfile
from urllib.parse import urlparse

from anki.utils import is_mac, no_bundled_libs
from aqt import mw
from aqt.operations import CollectionOp
from aqt.qt import QDesktopServices, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QUrl
from aqt.utils import openLink, showWarning, tooltip

from . import core
from .widgets import fill_deck_menu


def current_ir_note():
    """学習画面で表示中のカードがアドオンのノート（段落、cloze、quiz）なら、そのノートを返す。"""
    if mw.state != "review" or not mw.reviewer or not mw.reviewer.card:
        return None
    note = mw.reviewer.card.note()
    if note.note_type()["name"] not in core.IR_MODELS:
        return None
    return note


def _docid(note) -> str:
    return note["DocId"].strip() if note else ""


def doc_exists(docid: str) -> bool:
    """文書の作業コピーのファイルがあるか。"""
    return os.path.exists(core.doc_path(mw.col, docid))


def source_url(note) -> str:
    """ノートの文書の Source（core.doc_source）。http か https の URL でなければ空文字。"""
    url = core.doc_source(mw.col, _docid(note)) if _docid(note) else ""
    return url if urlparse(url).scheme.lower() in ("http", "https") else ""


def doc_file(note, orig: bool) -> str:
    """ノートの文書のファイル（orig なら原本、そうでなければ作業コピー）。無ければ空文字。"""
    docid = _docid(note)
    if not docid:
        return ""
    path = core.doc_path(mw.col, docid, orig=orig)
    return path if os.path.exists(path) else ""


# ---------------------------------------------------------------- Open Document

def open_source() -> None:
    """PBIR → Open Document → Source。文書の Source の URL を既定のブラウザで開く。"""
    url = source_url(current_ir_note())
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
    note = current_ir_note()
    path = doc_file(note, orig)
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


# ---------------------------------------------------------------- Modify Document

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
    学習画面は描画し直さない（カードの表示に Source は出ない）。Open Document → Source の灰色表示は、
    メニューを開くときに合わせる（reviewer._sync_menu）。
    """
    note = current_ir_note()
    if not doc_file(note, orig=False):
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
    tooltip("Source saved" if changed else "Source unchanged")


# ---------------------------------------------------------------- Move Document と New Deck

def fill_move_menu(menu) -> None:
    """PBIR → Move Document を開く直前に、デッキの一覧で作り直す（fill_deck_menu）。
    チェックは文書のカードの元のデッキ。
    """
    docid = _docid(current_ir_note())
    if not docid:
        fill_deck_menu(menu, set(), None)  # 移す文書が無い。デッキは灰色で出す
        return
    fill_deck_menu(menu, core.doc_decks(mw.col, docid), lambda did, name: move_document(docid, did, name))


def move_document(docid: str, deck_id: int, name: str) -> None:
    """PBIR → Move Document → デッキ。文書のカードをそのデッキへ移す（core.move_document）。学習画面はそのまま。

    文書は変えずカードのデッキだけを変えるので、undo の履歴は空にしない（Undo で Change Deck が戻る）。
    表示中のカードは読み直す。覚えている did が古いままだと、このあと作る cloze などが元のデッキに入る（reviewer._deck_id）。
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
