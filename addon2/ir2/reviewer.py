# -*- coding: utf-8 -*-
"""学習画面との接続: カードの JS（card.js）からの pycmd の受け口、評価の前後の処理、メニューバーの PBIR メニュー。"""

import html as html_lib
import json
from urllib.parse import unquote

from anki.errors import NotFoundError
from aqt import gui_hooks, mw
from aqt.qt import QAction, QMenu
from aqt.utils import showWarning, tooltip

from . import core, documents, importer


# ---------------------------------------------------------------- 状態

# 段落のカードの JS が ir2:state で知らせる状態（card.js の pushState）。ready はカードの JS の準備ができたこと（段落のカードだけが
# 知らせるので、cloze と quiz のカードでは False のまま）。side は面（q か a）。creating は Create → Basic などで問いと答えを
# 書いている間。source は Source Text の間。prev と next は前後の段落と結合できるか
_STATE_DEFAULT = {"ready": False, "side": "", "creating": False, "source": False, "prev": False, "next": False}
_state = dict(_STATE_DEFAULT)
# Create → Basic などで書いている問いと答えの下書き（ir2:draft の {kind, fields} と、docid, anchor, 書いた段落のカードの nid と did）。
# カードを離れるときに quiz のノートにする（_commit_draft）
_draft: dict = {}
# PBIR → Source Text のチェック。カードが替わっても続く（Anki を閉じるまで）
_source_text = False
# PBIR → Font の項目
_FONT_KEYS = ("bold", "italic", "underline", "highlight", "superscript", "subscript")
# 選択範囲で決まるチェック。Font の項目と、Create → Cloze（選択範囲の文字がすべて cloze の箇所にあるか）。JS が ir2:font で知らせる
_CHECK_KEYS = _FONT_KEYS + ("cloze",)
_font = dict.fromkeys(_CHECK_KEYS, False)
# PBIR → Create の項目（どれも段落のカードの回答面で、段落から作る）
_CREATE_KEYS = ("cloze",) + tuple(core.QUIZ)
_actions: dict = {}
_menu = None  # Python 側で参照を持っておかないと Qt のオブジェクトが消える


def _reset_state(*_args) -> None:
    """カードの描画が替わった、または学習画面を離れたとき。JS からの通知で再び有効になる。下書きは quiz のノートにする
    （_commit_draft）。"""
    _commit_draft()
    _state.update(_STATE_DEFAULT)
    _font.update(dict.fromkeys(_CHECK_KEYS, False))
    _sync_menu()


def _on_state_change(new_state, _old_state) -> None:
    if new_state != "review":
        _reset_state()


def _available() -> bool:
    return mw.state == "review" and _state["ready"]


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

    描画し直しはカードの JS が行う（"ok" を受け取ると文書を読み直す。card.js の render）。
    Anki の mw.reviewer._redraw_current_card() は使わない。問題面では _showQuestion を通り、下の欄の時計を 0 に戻し
    （reviewer-bottom.ts の showQuestion が time = 0）、音声を流し直し、自動で答えへ進む待ち時間を始め直す（26.09.2 のソース）。
    card.load() はノートとカードのデータを読み直すだけで、回答時間の起点（timer_started）には触らない
    （pylib/anki/cards.py の _load_from_backend_card）。
    """
    _discard_undo()
    tooltip(msg)
    if mw.state == "review" and mw.reviewer.card:
        mw.reviewer.card.load()


# ---------------------------------------------------------------- カードの JS からの pycmd
#
# 形は ir2:<操作>:<DocId>:<段落の ID>:<中身>。中身は encodeURIComponent したもの（ir2:merge は向きと中身を : でつなぐ）。
# 受け口（_HANDLERS）の戻り値が JS の pycmd のコールバックに渡る。

def _on_save(docid: str, anchor: str, inner_html: str):
    n, msgs = core.save_edit(mw.col, docid, anchor, inner_html)
    tooltip("<br>".join([f"Saved ({n} notes regenerated)"] + msgs))
    # 保存したあとの段落の HTML。Source Text の表示に使う（使わない呼び出しでは捨てられる）
    return core.para_source(mw.col, docid, anchor)


def _on_source(docid: str, anchor: str, _payload: str):
    """PBIR → Source Text: 段落の HTML（文書に書かれているとおり）。"""
    return core.para_source(mw.col, docid, anchor)


def _on_font(_docid: str, _anchor: str, payload: str):
    """PBIR → Font と Create → Cloze のチェック。段落の選択範囲が変わったときと、Font と Cloze の操作のあとに届く。"""
    st = json.loads(payload)
    _font.update({k: bool(st.get(k)) for k in _CHECK_KEYS})
    _sync_menu()


def _on_note(_docid: str, _anchor: str, text: str):
    """JS からの短い通知。黙って失敗させないためのもの。"""
    tooltip(text)


def _on_state(_docid: str, _anchor: str, payload: str):
    """段落のカードの JS が準備できたとき、Create で問いと答えを書き始めたときなどに届く。
    JS は Source Text を続けるかどうかを受け取る。"""
    st = json.loads(payload)
    _state.update({k: type(v)(st.get(k) or v) for k, v in _STATE_DEFAULT.items()}, ready=True)
    _sync_menu()
    return {"source": _source_text}


def _on_cloze(docid: str, anchor: str, inner_html: str):
    """PBIR → Create → Cloze。選択範囲の data-cloze マーク 1 個とノート 1 件。ノートの Text は段落の中のその語の出現を
    すべて隠す。JS は受け取った id で DOM の PENDING を置き換える。"""
    cloze_id, word, n = core.make_cloze(mw.col, docid, anchor, inner_html, _deck_id())
    _discard_undo()
    tooltip(f"Cloze created ({cloze_id}: {core.plural(n, 'occurrence')} "
            f"of \"{html_lib.escape(word)}\")")
    return cloze_id


def _on_draft(docid: str, anchor: str, payload: str):
    """Create → Basic などで書いている欄の今の中身。表示中の段落のカードのノートとデッキを一緒に覚える。"""
    card = mw.reviewer.card
    _draft.clear()
    _draft.update(json.loads(payload), docid=docid, anchor=anchor, nid=card.nid, did=card.did)


def _on_extract(docid: str, anchor: str, inner_html: str):
    """PBIR → Extract。inner_html は選択の始まりと終わりの印の入った段落の中身。"""
    ids = core.extract_paragraph(mw.col, docid, anchor, inner_html, _deck_id())
    parts = [f"the text {where} the selection is now {i}" for where, i in zip(("before", "after"), ids) if i]
    _after_structure_change(f"Extract: {' and '.join(parts)} "
                            f"({'its card is' if len(parts) == 1 else 'their cards are'} at the end of the new cards)")
    return "ok"


def _on_merge(docid: str, anchor: str, payload: str):
    """PBIR → Merge Previous / Merge Next。payload は向き（prev か next）と段落の今の中身。先に今の中身を保存する。"""
    direction, _, inner_html = payload.partition(":")
    core.save_edit(mw.col, docid, anchor, inner_html)
    other, n_moved, n_del = core.merge_paragraph(mw.col, docid, anchor, direction)
    _after_structure_change(
        f"Merged {other} into {anchor}"
        + (f", moved {core.plural(n_moved, 'note')} to {anchor}" if n_moved else "")
        + (", deleted its paragraph note" if n_del else ""))
    return "ok"


_HANDLERS = {"save": _on_save, "source": _on_source, "font": _on_font, "note": _on_note, "state": _on_state,
             "cloze": _on_cloze, "draft": _on_draft, "extract": _on_extract, "merge": _on_merge}


def on_js_message(handled, message: str, context):
    parts = message.split(":", 4)   # ir2, 操作, DocId, 段落の ID, 中身
    if len(parts) != 5 or parts[0] != "ir2" or parts[1] not in _HANDLERS:
        return handled
    _, cmd, docid, anchor, payload = parts
    try:
        return (True, _HANDLERS[cmd](docid, anchor, unquote(payload)))
    except core.Refused as e:
        tooltip(f"{cmd[:1].upper() + cmd[1:]} failed: {e}")
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: operation failed: {e!r}")
    return (True, None)


def _commit_draft() -> None:
    """Create → Basic などで書いた下書き（ir2:draft）から、同じ段落に quiz のノートを作り、下書きを捨てる。

    カードを離れるとき（評価したとき、ほかのカードに替わるとき、同じカードを描画し直すとき、学習画面を終えるとき。
    on_answer_card と _reset_state）に呼ぶ。何も書かなかったときは下書きが無いので作らない。空のフィールドもそのまま保存する。
    文書は変えないので undo の履歴は空にしない。下書きを書いたあとに段落のノートが消されていれば（Anki の Delete Note など）
    作らない（その段落は生きていないので、作っても評価のあとの後始末で消える。core.remove_empty）。
    """
    d = dict(_draft)
    _draft.clear()
    if not d or mw.col is None:
        return
    try:
        mw.col.get_note(d["nid"])
    except NotFoundError:
        return
    kind = d["kind"]
    try:
        core.create_quiz(mw.col, d["docid"], d["anchor"], kind, d["fields"], d["did"])
    except Exception as e:  # noqa: BLE001
        showWarning(f"PBIR: could not create the {core.QUIZ[kind].label} card: {e!r}")
        return
    mw.update_undo_actions()
    tooltip(f"{core.QUIZ[kind].label} {'cards' if kind == 'enum' else 'card'} created")


# ---------------------------------------------------------------- 評価

def on_answer_card(_reviewer, card, _ease) -> None:
    """カードを評価したあと、次のカードを出す前（26.09.2 の reviewer.pyc の _after_answering は
    reviewer_did_answer_card を呼んでから nextCard を呼ぶ）。まず、Create で書いた下書きから quiz のノートを作る（_commit_draft。
    次のカードを出すときの _reset_state でも作るが、下の後始末が段落を消す前に作るため、ここで呼ぶ）。
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
        if not documents.doc_exists(docid):
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


# ---------------------------------------------------------------- PBIR メニュー
#
# メニューバーの PBIR メニュー。項目にキーショートカットは付けない。
# 付けると Qt が webview より先にキーを取り、編集中の文字入力を奪うため。

def _sync_menu() -> None:
    """項目の灰色表示とチェックを、表示中のカードの状態（_state、_font）に合わせる。"""
    if not _actions:
        return
    ok = _available()
    # 描画された段落に対する操作は、Create で問いと答えを書いている間と Source Text の間は使えない
    para = ok and not _state["creating"]
    rendered = para and not _state["source"]
    front, back = rendered and _state["side"] == "q", rendered and _state["side"] == "a"   # 問題面では段落を編集できる
    _actions["source_text"].setEnabled(para)
    # setChecked は triggered を出さない（メニューの操作はクリックのときだけ）
    _actions["source_text"].setChecked(_source_text)
    for key in _CHECK_KEYS:
        _actions[key].setChecked(ok and _font[key])
    # Create は回答面だけ（段落から作る）
    for key in _CREATE_KEYS:
        _actions[key].setEnabled(back)
    for key in _FONT_KEYS + ("extract",):
        _actions[key].setEnabled(front)
    _actions["merge_prev"].setEnabled(front and _state["prev"])
    _actions["merge_next"].setEnabled(front and _state["next"])
    # Open Document などの項目はノートと文書のファイルから決めるので、JS の準備を待たない（cloze と quiz のカードでも使える）
    note = documents.current_ir_note()
    _actions["open_source"].setEnabled(bool(documents.source_url(note)))
    _actions["open_original"].setEnabled(bool(documents.doc_file(note, orig=True)))
    _actions["open_current"].setEnabled(bool(documents.doc_file(note, orig=False)))
    _actions["edit_source"].setEnabled(bool(documents.doc_file(note, orig=False)))


def _run(cmd: str, arg=None) -> None:
    """カードの JS の window._ir2Cmd を呼ぶ（段落のカードの JS の準備ができているときだけ）。"""
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


def _check_cmd(cmd: str) -> None:
    """PBIR → Font の項目と Create → Cloze。Qt はクリックした時点でチェックを反転するので、いったん今の状態に戻す。
    操作で装飾か cloze が変わったら、JS が ir2:font で新しい状態を知らせる（断られたときは変わらない）。"""
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


def setup_menu() -> None:
    global _menu
    menu = QMenu("PBIR", mw)
    _add(menu, "source_text", "Source Text", _toggle_source_text, checkable=True)
    # 下位メニューは、中の項目がどれも使えないときも開けるようにしておく（Apple の HIG の Menus: 中に何があるかを見られるように）。
    # 灰色にするのは中の項目だけ
    create = menu.addMenu("Create")
    # Cloze は、選択範囲の文字がすべて cloze の箇所にあるとチェックが付き、選ぶとその cloze を外す（Font → Highlight と同じ）
    _add(create, "cloze", "Cloze", lambda _checked: _check_cmd("cloze"), checkable=True)
    for key, quiz in core.QUIZ.items():
        _add(create, key, quiz.label, lambda k=key: _run(k))
    font = menu.addMenu("Font")
    # 普通のエディタと同じく、選択範囲に付いている装飾にチェックが付く（_sync_menu）
    for key, label in zip(_FONT_KEYS, ("Bold", "Italic", "Underline", "Highlight", "Superscript", "Subscript")):
        _add(font, key, label, lambda _checked, k=key: _check_cmd(k), checkable=True)
    _add(menu, "extract", "Extract", lambda: _run("extract"))
    _add(menu, "merge_prev", "Merge Previous", lambda: _run("merge", "prev"))
    _add(menu, "merge_next", "Merge Next", lambda: _run("merge", "next"))
    opendoc = menu.addMenu("Open Document")
    _add(opendoc, "open_source", "Source", documents.open_source)
    _add(opendoc, "open_original", "Original", lambda: documents.open_document(orig=True))
    _add(opendoc, "open_current", "Current", lambda: documents.open_document(orig=False))
    moddoc = menu.addMenu("Modify Document")
    _add(moddoc, "edit_source", "Add/Edit Source", documents.edit_source)
    move = menu.addMenu("Move Document")
    # 開くたびにデッキの一覧で作り直す
    move.aboutToShow.connect(lambda: documents.fill_move_menu(move))
    _add(menu, "new_deck", "New Deck", documents.new_deck)
    _add(menu, "import", "Import ...", importer.on_import)
    _add(menu, "check_dupes", "Check Duplicates", importer.check_duplicates)
    # 開く直前にも合わせる。Open Document などの項目は文書のファイルの有無で決まり、カードの JS からの知らせを待たない
    for m in (menu, create, font, opendoc, moddoc):
        m.aboutToShow.connect(_sync_menu)
    mw.form.menubar.insertMenu(mw.form.menuHelp.menuAction(), menu)
    _menu = menu
    _sync_menu()


def setup() -> None:
    setup_menu()
    gui_hooks.webview_did_receive_js_message.append(on_js_message)
    gui_hooks.reviewer_did_show_question.append(_reset_state)
    gui_hooks.reviewer_did_show_answer.append(_reset_state)
    gui_hooks.reviewer_will_end.append(_reset_state)
    gui_hooks.reviewer_did_answer_card.append(on_answer_card)
    gui_hooks.reviewer_will_init_answer_buttons.append(on_answer_buttons)
    gui_hooks.state_did_change.append(_on_state_change)
