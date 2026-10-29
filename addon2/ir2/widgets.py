# -*- coding: utf-8 -*-
"""取り込みダイアログと PBIR メニューの両方が使う部品。"""

import time

from aqt import mw
from aqt.qt import QAction, QMenu

from . import core


def menu_text(name: str) -> str:
    """Qt の項目とボタンの文字。& は Qt では次の文字の印になるので && にする。"""
    return name.replace("&", "&&")


def fill_deck_menu(menu, checked: set, on_pick) -> None:
    """menu をデッキの一覧で作り直す。PBIR → Move Document と取り込みダイアログの Deck で使う。

    on_pick(deck_id, name) は項目を選んだときに呼ぶ。None ならデッキの項目を灰色にする（下位メニューは開ける）。
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
    decks = mw.col.decks.all_names_and_ids(include_filtered=False, skip_empty_default=True)
    parents = {d.name.rpartition("::")[0] for d in decks}  # 下位のデッキを持つデッキの名前
    on_path = set()  # チェックの付くデッキと、その上の階層の名前
    for d in decks:
        if d.id in checked:
            parts = d.name.split("::")
            on_path.update("::".join(parts[:i]) for i in range(1, len(parts) + 1))
    menus = {"": menu}

    def label(name: str) -> str:
        return menu_text(name.rpartition("::")[2])

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
        act.setEnabled(on_pick is not None)
        if on_pick is not None:
            act.triggered.connect(lambda _=False, did=d.id, name=d.name: on_pick(did, name))
        where.addAction(act)
        if d.name in parents:
            where.addSeparator()


def doc_label(col, docid: str) -> str:
    """重複の警告と PBIR → Check Duplicates で、文書を見分けるための 1 行。
    取り込んだ日時（DocId は取り込んだ時刻のミリ秒の 16 進。core.new_docid）、デッキ、DocId、原本の最初の見出しか段落。"""
    try:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(docid, 16) / 1000))
    except ValueError:
        when = "?"
    decks = ", ".join(sorted(col.decks.name(did) for did in core.doc_decks(col, docid)))
    snippet = core.doc_snippet(col, docid)
    return f"imported {when} into {decks}, doc {docid}" + (f": {snippet}" if snippet else "")
