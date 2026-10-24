# -*- coding: utf-8 -*-
"""Paragraph-Based Incremental Reading: core logic.

aqt に依存しない層。GUI なしで pylib (anki) だけで動作確認できる。
記事の文章は文書のファイルに一つだけあり、ノートは文書の中の段落か cloze を指す。
カードは文書の直下の <div data-ir="pN"> 1 個で、その中に p やリストなどのブロックがそのまま入る。
"""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import html
import json
import os
import re
import time
from collections import Counter
from itertools import combinations

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from bs4.dammit import EncodingDetector, UnicodeDammit

from anki.collection import Collection
from anki.consts import MODEL_CLOZE
from anki.utils import ids2str

PARA_MODEL = "IR2 Paragraph"
CLOZE_MODEL = "IR2 Cloze"
# Basic、True or False、Multiple Choice、Enumeration: 段落に付く問いと答えのカード（PBIR → Create）。AnchorId は段落の ID（p12）
# そのもの。問いと答えは文書に無く、利用者が書くフィールドだけが持つ（Text は段落の写し）
BASIC_MODEL = "IR2 Basic"
# カードは 1 枚で、出すたびに True と False のどちらの文を出すかを選ぶ
TF_MODEL = "IR2 True or False"
# Choices は 1 行目が正しい選択肢、2 行目からが誤りの選択肢（choice_lines）。カードは 1 枚で、出すたびに誤りを 1 個選ぶ
MC_MODEL = "IR2 Multiple Choice"
# cloze 型。Items は順序のある項目を 1 行に 1 個、項目ごとに cloze 1 個（enum_items）。番号は後ろの項目から c1, c2, ...
# （Anki は同じノートのカードを番号の小さい順に出すので、後ろの項目から学ぶ）。作ったあとは編集しない（Recreate で作り直す）
ENUM_MODEL = "IR2 Enumeration"
# Basic などの種類（JS とのやりとりの名前、PBIR → Create の項目の名前）→ (ノートタイプ, 項目の名前, 利用者が書くフィールド)
DERIVED = {"basic": (BASIC_MODEL, "Basic", ("Question", "Answer")),
           "tf": (TF_MODEL, "True or False", ("True", "False")),
           "mc": (MC_MODEL, "Multiple Choice", ("Question", "Choices")),
           "enum": (ENUM_MODEL, "Enumeration", ("Question", "Items"))}
DERIVED_MODELS = tuple(m for m, _, _ in DERIVED.values())
# アドオンのノートタイプ全部
IR_MODELS = (PARA_MODEL, CLOZE_MODEL) + DERIVED_MODELS


def models_search(models=IR_MODELS) -> str:
    """ノートタイプが models のどれかのノートを探す検索の文字列。"""
    return "(" + " OR ".join(f'"note:{m}"' for m in models) + ")"


def _doc_notes(col: Collection, docid: str, models=IR_MODELS) -> list:
    """文書 docid のノートのうち、ノートタイプが models のどれかのもの。"""
    return [col.get_note(nid) for nid in col.find_notes(f'"DocId:{docid}" {models_search(models)}')]


# カードにするブロック（segment）。文書の直下にある p とリスト。リストは項目ごとでなく全体で 1 枚。
# 見出し、pre、blockquote、表、図、hr などは表示するがカードにしない（中の p もカードにしない）
CARD_TAGS = ("p", "ul", "ol", "dl")
LIST_TAGS = ("ul", "ol", "dl")
# 取り込みで外す入れ物（segment）。Readability の出力では本文がこれらの入れ子になっていて、class と style は
# Readability が外している（Readability.js の _cleanClasses と _cleanStyles）。main と header は HTML5 の入れ物
CONTAINER_TAGS = ("div", "section", "article", "main", "header")
# 取り込み時に落とす要素（スクリプトを動かしうる要素は sanitize が UNSAFE_TAGS で落とす）
DROP_TAGS = ["nav", "form", "button"]


# ---------------------------------------------------------------- doc store

def doc_path(col: Collection, docid: str, orig: bool = False) -> str:
    """文書のファイル。orig なら原本。"""
    suffix = ".orig.html" if orig else ".html"
    return os.path.join(col.media.dir(), f"_ir2_{docid}{suffix}")


def read_doc(col: Collection, docid: str, orig: bool = False) -> str:
    with open(doc_path(col, docid, orig), encoding="utf-8") as f:
        return f.read()


def write_doc(col: Collection, docid: str, html: str, orig: bool = False) -> None:
    """一時ファイルに書いてから文書のファイルと置き換える。

    同じファイルに上書きすると、media フォルダの更新時刻が変わらない（macOS で確かめた）。Anki の media の同期は、
    フォルダの更新時刻が前の同期のときと同じならフォルダを調べないので（26.09.2 の
    rslib/src/sync/media/database/client/changetracker.rs の register_changes）、直した文書がほかの端末へ届かない。
    置き換えはフォルダの中身の変更なので、フォルダの更新時刻が変わる。
    """
    path = doc_path(col, docid, orig)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(path + ".tmp", path)


def doc_soup(col: Collection, docid: str, orig: bool = False) -> BeautifulSoup:
    return BeautifulSoup(read_doc(col, docid, orig), "html.parser")


def _find_para(soup, anchor: str):
    el = soup.find(attrs={"data-ir": anchor})
    if el is None:
        raise KeyError(f"paragraph {anchor} was not found in the document")
    return el


def _doc_para(col: Collection, docid: str, anchor: str):
    """作業コピーの段落 anchor の要素。"""
    return _find_para(doc_soup(col, docid), anchor)


def _put_para(el, inner_html: str) -> None:
    """段落の要素 el の中身を inner_html にする（保存と Extract）。スクリプトを動かしうるものと、中の段落の印を外す
    （段落の中に段落の印があってはならない。Source Text で手で書ける）。cloze の印は残す。"""
    el.clear()
    el.append(BeautifulSoup(inner_html, "html.parser"))
    sanitize(el)
    for t in el.find_all(attrs={"data-ir": True}):
        del t["data-ir"]


def _add_note(col: Collection, model: str, docid: str, anchor: str, text: str, deck_id: int,
              fields: dict = None) -> int:
    """アドオンのノートを 1 件作る。戻り値は note id。"""
    note = col.new_note(col.models.by_name(model))
    for name, v in (fields or {}).items():
        note[name] = v
    note["DocId"] = docid
    note["AnchorId"] = anchor
    note["Text"] = text
    col.add_note(note, deck_id)
    return note.id


def new_docid() -> str:
    return format(int(time.time() * 1000), "x")


# ---------------------------------------------------------------- import

# このアドオンが文書に書く印。取り込む HTML に既にあれば外す。アドオンが書いた文書
# （原本 _ir2_<docid>.orig.html など）を取り込み直すと、見出しなど段落でない要素に古い
# data-ir が残り、振り直した段落の ID と重なるため（2026-09-25、プレビューで見つかった）。
IR_ATTRS = ("data-ir", "data-cloze", "data-ir-split", "contenteditable")


# ---------------------------------------------------------------- スクリプトの除去
#
# 取り込む HTML は他人が書いたもの。カードは文書を innerHTML で入れるので、<script> は動かないが、
# onerror などのイベント属性は動く（Chromium で確認）。そうすると記事の書き手のコードが学習画面で
# pycmd やネットワークを使える。Readability も出力を消毒しない（README の Security の節が、
# 信頼できない入力では出力を消毒せよと勧めている）。そこで、コードを動かしうるものを外す。
# 取り込み（normalize）、保存（save_edit, extract_paragraph）、
# View Original の表示用の複製（sanitize_html）で行う。原本そのものは変えない。

UNSAFE_TAGS = ["script", "style", "iframe", "frame", "frameset", "object", "embed", "applet",
               "base", "meta", "link", "noscript"]
_SCRIPT_URL = re.compile(r"(javascript|vbscript):", re.I)
_IGNORED_IN_URL = re.compile(r"[\x00-\x20]")  # ブラウザは "java script:" の空白や制御文字を無視する


def _attr_str(v) -> str:
    return " ".join(v) if isinstance(v, list) else (v or "")


def sanitize(root) -> int:
    """root（とその子孫）から、スクリプトを動かしうるものを外す。戻り値は外した数。

    - 要素: UNSAFE_TAGS（中身ごと）
    - 属性: on で始まるもの（onerror, onload, onclick など。SVG の onbegin も）と、
      値に javascript: か vbscript: を含むもの（href, xlink:href, SVG の animate の values など）
    """
    n = 0
    for tag in root.find_all(UNSAFE_TAGS):
        if not tag.decomposed:
            tag.decompose()
            n += 1
    tags = root.find_all(True)
    if isinstance(root, Tag) and not isinstance(root, BeautifulSoup):
        tags = [root] + tags
    for tag in tags:
        for a in list(tag.attrs):
            if a.lower().startswith("on") or _SCRIPT_URL.search(
                    _IGNORED_IN_URL.sub("", _attr_str(tag.attrs[a]))):
                del tag.attrs[a]
                n += 1
    return n


def sanitize_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    sanitize(soup)
    return str(soup)


def normalize(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(lambda t: any(a in t.attrs for a in IR_ATTRS)):
        for a in IR_ATTRS:
            tag.attrs.pop(a, None)
    for c in soup.find_all(string=lambda t: isinstance(t, Comment) and t.strip().startswith("ir:source")):
        c.extract()  # 取り込み時に先頭へ書き直す。古いものが残ると Source が食い違う
    sanitize(soup)
    body = soup.find("body")
    return BeautifulSoup(body.decode_contents(), "html.parser") if body else soup


def _is_block(node) -> bool:
    return isinstance(node, Tag) and node.name in BLOCK_TAGS


def _is_container(el) -> bool:
    """取り込みで外す入れ物か。CONTAINER_TAGS の要素で、直下にブロックを持つもの。

    直下に文字しか無い div（取り込みダイアログが題の下に置く著者と日付の行など）はブロックとして残す。
    """
    return (isinstance(el, Tag) and el.name in CONTAINER_TAGS
            and any(_is_block(c) for c in el.children))


def _wrap_runs(soup, parent) -> None:
    """parent の直下で、ブロックの間にある文字と inline の要素の並びを p に入れる（空白とコメントだけの並びは除く）。

    Readability が div の中の文字に対してするのと同じ（Readability.js の _grabArticle）。
    """
    run = []

    def flush():
        if any(not isinstance(n, Comment) and not _is_blank(n) for n in run):
            p = soup.new_tag("p")
            run[0].insert_before(p)
            for n in run:
                p.append(n.extract())
        run.clear()

    for c in list(parent.contents):
        if _is_block(c):
            flush()
        else:
            run.append(c)
    flush()


def segment(soup: BeautifulSoup) -> list:
    """入れ物を外して文書をブロックの並びにし、カードのブロックを <div data-ir="pN"> で包む。包んだ div のリストを返す。

    1. 根を決める。文書が入れ物 1 個だけなら、その中へ降りる（lang などの属性を持つ外側の要素を残すため）。
    2. 根の直下の入れ物（_is_container）を外し、その中身を根の直下に出す。中の入れ物も同じく外す。
       ブロックの間の文字は p に入れる（_wrap_runs）。
    3. 根の直下の CARD_TAGS（p とリスト）で、中身（文字か画像など）のあるものを、文書順に 1 個ずつ div で包み、ID を振る。
       ほかのブロック（見出し、pre、blockquote、表、図、hr など）はそのまま残り、カードにならない。
    """
    root = soup
    while True:
        kids = [c for c in root.contents if not isinstance(c, Comment) and not _is_blank(c)]
        if len(kids) == 1 and _is_container(kids[0]):
            root = kids[0]
        else:
            break
    while any(_is_container(c) for c in root.children):
        for c in list(root.children):
            if _is_container(c):
                _wrap_runs(soup, c)
                c.unwrap()
    _wrap_runs(soup, root)
    units = []
    for c in list(root.children):
        if isinstance(c, Tag) and c.name in CARD_TAGS and _has_content(c):
            box = soup.new_tag("div")
            box["data-ir"] = f"p{len(units) + 1}"
            c.wrap(box)
            units.append(box)
    return units


def _model_specs() -> dict:
    """アドオンのノートタイプ → (cloze 型か, フィールド, テンプレートの名前, 問題面, 回答面)。テンプレートはどれも 1 枚
    （Enumeration は cloze 型で、カードは項目の数だけ）。"""
    ids = ("DocId", "AnchorId", "Text")
    specs = {PARA_MODEL: (False, ids, "IR", _para_template("q"), _para_template("a")),
             CLOZE_MODEL: (True, ("Text", "DocId", "AnchorId"), "Cloze", _cloze_template("q"), _cloze_template("a"))}
    for kind, (model, label, fields) in DERIVED.items():
        specs[model] = (kind == "enum", fields + ids, label, _qa_template(kind, "q"), _qa_template(kind, "a"))
    return specs


def ensure_models(col: Collection) -> None:
    """無いアドオンのノートタイプを作る（取り込み）。"""
    for name, (cloze, fields, tname, front, back) in _model_specs().items():
        if col.models.by_name(name):
            continue
        m = col.models.new(name)
        if cloze:
            m["type"] = MODEL_CLOZE
        for f in fields:
            col.models.add_field(m, col.models.new_field(f))
        col.models.add_template(m, col.models.new_template(tname))
        _set_template(m, front, back)
        col.models.add(m)


def update_models(col: Collection) -> None:
    """あるアドオンのノートタイプのテンプレートと CSS を今の内容にする（起動時）。テンプレートはノートタイプを作ったときに
    書かれたきりなので、アドオンの更新をコレクションへ届けるにはこれが要る。"""
    for name, (_cloze, _fields, _tname, front, back) in _model_specs().items():
        m = col.models.by_name(name)
        if m:
            _set_template(m, front, back)
            col.models.update_dict(m)


def _set_template(m: dict, front: str, back: str) -> None:
    m["tmpls"][0]["qfmt"], m["tmpls"][0]["afmt"] = front, back
    m["css"] = _css()


def import_article(col: Collection, html: str, deck_name: str,
                   source_url: str = "") -> tuple:
    """記事を取り込む。戻り値は (docid, 作成したノート数)。

    1. 正規化 2. 分割と ID 付与 3. 埋め込み画像を media のファイルへ書き出す
    4. 原本を書く 5. 作業コピーを書く
    6. 段落ノートを文書順に生成（position は Anki の既定の採番に任せる）

    source_url は文書の Source（先頭のコメント。_with_source）になる。
    """
    ensure_models(col)
    soup, units = prepare_article(html)

    docid = new_docid()
    extract_images(col, soup, docid)
    doc_html = _with_source(str(soup), source_url)

    write_doc(col, docid, doc_html)
    write_doc(col, docid, doc_html, orig=True)
    deck_id = col.decks.id(deck_name)
    for el in units:  # 文書順。作成順が position を決める
        _add_note(col, PARA_MODEL, docid, el["data-ir"], el.decode_contents(), deck_id)
    return docid, len(units)


def prepare_article(html: str) -> tuple:
    """取り込む形の文書を作る。戻り値は (soup, 段落の要素のリスト)。段落が無ければ ValueError。

    html は、HTML なら取り込みダイアログが Readability で取り出した本文、平文と Markdown ならそれを起こした HTML。
    正規化、入れ物を外してカードのブロックを div で包む（segment）まで。
    取り込み（import_article）と取り込みダイアログのプレビュー（preview_html）の両方がこれを使うので、
    プレビューに出る文書と取り込まれる文書は同じになる。画像の書き出しはしない（取り込み時だけ）。
    """
    soup = normalize(html)
    lift_headings(soup)
    units = segment(soup)
    if not units:
        raise ValueError("no paragraphs found in the article")
    return soup, units


_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def lift_headings(soup) -> int:
    """p の中にある見出しを p の外に出す。戻り値は外に出した数。

    見出しはカードにしない。p の中に見出しがあると、その p のカードの中身になってしまう。
    ブラウザも p の中の見出しの開始タグで p を閉じる（BLOCK_TAGS）。
    Readability は見出しだけを持つ div を p に替える（DIV を P にする判定の要素に見出しが無い）ので、
    本文だけを取り出した文書によく出る。p の直下の子を順に見て、見出しを含む子はそのまま外に出し、
    その前後の中身は元の p と同じ属性の p に入れる。空白と <br> だけになる p は作らない。
    """
    n = 0
    for p in soup.find_all("p"):
        if p.parent is None or not p.find(_HEADINGS):
            continue
        parts, run = [], []
        for c in list(p.contents):
            c = c.extract()
            if isinstance(c, Tag) and (c.name in _HEADINGS or c.find(_HEADINGS)):
                parts += [("p", run), ("h", c)]
                run = []
                n += 1
            else:
                run.append(c)
        parts.append(("p", run))
        for kind, x in parts:
            if kind == "h":
                p.insert_before(x)
                continue
            box = soup.new_tag("p", attrs=dict(p.attrs))
            for c in x:
                box.append(c)
            if _has_content(box):
                p.insert_before(box)
        p.decompose()
    return n


def text_to_html(text: str) -> str:
    """平文を段落の HTML に起こす。空行で段落を区切る（改行は article_html が \n にそろえる）。"""
    return "".join(f"<p>{html.escape(b.strip(), quote=False)}</p>" for b in text.split("\n\n") if b.strip())


# 文字コードの名前（ラベル）を、ブラウザと同じ意味の Python の codec に直す。WHATWG の Encoding Standard
# （https://encoding.spec.whatwg.org/encodings.json、2026-09-25 に確認）で、これらのラベルは左の encoding を指す。
# Python の同名の codec とは中身が違うものがある。例: Standard の Shift_JIS は Windows の拡張（①など）を含み、
# ラベル windows-31j も同じ encoding を指す。Python の shift_jis はそれを読めず、cp932 なら読める
# （shift_jis と宣言して Windows の拡張の文字を使うページがある）。
_WHATWG_LABELS = {
    "utf-8": ["unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8", "utf-8", "utf8", "x-unicode20utf8"],
    "cp932": ["csshiftjis", "ms932", "ms_kanji", "shift-jis", "shift_jis", "sjis", "windows-31j", "x-sjis"],
    "euc_jp": ["cseucpkdfmtjapanese", "euc-jp", "x-euc-jp"],
    "iso2022_jp": ["csiso2022jp", "iso-2022-jp"],
    "cp1252": ["ansi_x3.4-1968", "ascii", "cp1252", "cp819", "csisolatin1", "ibm819", "iso-8859-1",
               "iso-ir-100", "iso8859-1", "iso88591", "iso_8859-1", "iso_8859-1:1987", "l1", "latin1",
               "us-ascii", "windows-1252", "x-cp1252"],
    "gb18030": ["chinese", "csgb2312", "csiso58gb231280", "gb2312", "gb_2312", "gb_2312-80", "gbk",
                "iso-ir-58", "x-gbk", "gb18030"],
    "cp949": ["cseuckr", "csksc56011987", "euc-kr", "iso-ir-149", "korean", "ks_c_5601-1987",
              "ks_c_5601-1989", "ksc5601", "ksc_5601", "windows-949"],
    "big5hkscs": ["big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5"],
}
_LABEL_TO_CODEC = {label: codec for codec, labels in _WHATWG_LABELS.items() for label in labels}


def browser_codec(label):
    """文字コードのラベルを Python の codec の名前にする。知らないラベルで Python にも無ければ None。"""
    if not label:
        return None
    key = label.strip().strip("'\"").lower()
    if key in _LABEL_TO_CODEC:
        return _LABEL_TO_CODEC[key]
    try:
        return codecs.lookup(key).name
    except LookupError:
        return None


def decode_text(data: bytes, is_html: bool, http_charset: str = None) -> str:
    """取り込むファイルやページのバイト列を文字列にする。URL とファイルの両方がこれを使う。

    文字コードの候補の順: BOM、HTTP の Content-Type の charset（URL のとき）、ページの <meta> の宣言
    （HTML のとき）、UTF-8、中身からの推定（Anki 同梱の charset_normalizer）、windows-1252。
    ラベルは browser_codec で直す。候補は厳密に読み、読めなければ次へ進む（bs4 の UnicodeDammit。
    BOM、user_encodings、宣言、推定、UTF-8、windows-1252 の順に試すことを実験で確認）。
    UTF-8 を推定より前に置くのは、推定が短い文や繰り返しの多い文で外れるため（BOM の無い UTF-8 の
    "本文。" * 20 が cp037 と推定された）。UTF-8 でない文字コードの文が UTF-8 として読めることはまず無い。
    宣言が無く UTF-8 でもない文（古い Shift_JIS や EUC-JP の平文など）は推定に頼るので、短いと外れうる。
    ここで決めた文字列が、プレビューにも、UTF-8 で保存する文書にも、カードにもそのまま使われる。
    """
    candidates = []
    if http_charset:
        candidates.append(browser_codec(http_charset))
    if is_html:
        candidates.append(browser_codec(EncodingDetector.find_declared_encoding(data, is_html=True)))
    candidates.append("utf-8")
    candidates = [c for i, c in enumerate(candidates) if c and c not in candidates[:i]]
    d = UnicodeDammit(data, user_encodings=candidates, is_html=is_html)
    if d.unicode_markup is None:  # どの候補でも読めない。読めない所だけ置き換える
        return data.decode(candidates[0] if candidates else "utf-8", errors="replace")
    return d.unicode_markup


PLAIN_TEXT_EXTS = (".txt", ".text")
MARKDOWN_EXTS = (".md", ".markdown")


def markdown_to_html(text: str) -> str:
    """Markdown を HTML に変換する（改行は article_html が \n にそろえる）。以後はほかの HTML と同じに扱う（prepare_article など）。

    Anki に同梱の Python-Markdown（26.09.2 では 3.10.3）を使い、同梱の extra 拡張（表、``` の囲みのコード、
    脚注、定義リストなど）を有効にする。見出しは h1〜h6 になりカードにならない。段落、リストの項目、
    引用、コードのブロックが段落（カード）になる。Markdown の中に書かれた HTML はそのまま通るが、
    取り込みでスクリプトを動かしうるものは外れる（sanitize）。
    """
    import markdown  # Anki の同梱。aqt を読み込む前でも使えるよう、ここで読む
    return markdown.markdown(text, extensions=["extra"], output_format="html")


def is_markdown_name(name: str) -> bool:
    """ファイル名か URL のパスが Markdown の拡張子で終わるか。"""
    return name.lower().endswith(MARKDOWN_EXTS)


def article_html(text: str, name: str) -> tuple:
    """取り込む文字列を HTML にする。name はファイル名か URL のパス。戻り値は (HTML, 平文か Markdown だったか)。

    Markdown の拡張子なら Markdown として変換し、平文の拡張子か、< を含まない文字列なら平文として段落に起こす。
    これらはアドオンが起こした HTML で、取り出す本文も周りも無いので、取り込みダイアログは Readability を通さない。
    それ以外は HTML のまま返す（取り込みダイアログが Readability で本文を取り出す）。URL とファイルの両方がこれを使う。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n")   # 平文と Markdown の改行をそろえる
    if is_markdown_name(name):
        return markdown_to_html(lines), True
    if name.lower().endswith(PLAIN_TEXT_EXTS) or "<" not in text:
        return text_to_html(lines), True
    return text, False


def read_article_file(path: str) -> tuple:
    """取り込むファイルを読む。戻り値は article_html と同じ。平文と Markdown では <meta> の文字コードを探さない。"""
    with open(path, "rb") as f:
        data = f.read()
    is_html = not path.lower().endswith(PLAIN_TEXT_EXTS + MARKDOWN_EXTS)
    return article_html(decode_text(data, is_html=is_html), path)


def plural(n: int, word: str) -> str:
    """利用者への知らせの数と語（1 paragraph, 2 paragraphs）。"""
    return f"{n} {word}{'' if n == 1 else 's'}"


_PREVIEW_CSS = """
body { margin: 0; padding: 1em 1em 1em 3.6em; background: #ffffff; }
body.night_mode { background: #2c2c2c; }
#ir-doc.ir-preview, #ir-doc.ir-preview * { color: var(--ir-fg); }
#ir-doc.ir-preview img, #ir-doc.ir-preview svg, #ir-doc.ir-preview canvas,
#ir-doc.ir-preview video { opacity: 1; }
#ir-doc.ir-preview [data-ir] { outline: 1px dashed #b8b8b8; outline-offset: 2px;
        position: relative; }
#ir-doc.ir-preview [data-ir]::before { content: attr(data-ir); position: absolute;
        right: 100%; margin-right: .7em; font: 11px sans-serif; color: #999999;
        white-space: nowrap; }
.ir-preview-head { font: 13px sans-serif; color: #666666; margin-bottom: 1em; }
.night_mode .ir-preview-head { color: #aaaaaa; }
"""


def preview_html(soup, night: bool = False) -> str:
    """取り込みダイアログのプレビューのページ。

    取り込まれる文書全体を、カードと同じ CSS で、薄くせずに出す。カードになる段落
    （data-ir を持つ要素）には点線の枠と ID を付ける。見出しや図は表示されるが枠は付かない。
    """
    n = len(soup.find_all(attrs={"data-ir": True}))
    head = (f'<div class="ir-preview-head">{plural(n, "paragraph")} '
            f'(one card each, in this order). Dashed boxes are paragraphs; '
            f'everything else is shown on the cards but gets no card.</div>')
    return ('<!doctype html><html><head><meta charset="utf-8"><style>' + _css() + _PREVIEW_CSS
            + '</style></head><body class="card' + (" night_mode" if night else "") + '">'
            + head + '<div id="ir-doc" class="ir-preview">' + str(soup) + '</div></body></html>')


DATA_URI_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.*)$", re.DOTALL)

MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "image/avif": "avif",
}


def _ext_for(mime: str) -> str:
    if mime in MIME_EXT:
        return MIME_EXT[mime]
    sub = mime.split("/")[-1].split("+")[0]
    return re.sub(r"[^a-z0-9]", "", sub.lower()) or "bin"


def extract_images(col: Collection, soup, docid: str) -> None:
    """<img src="data:...;base64,..."> を media の個別ファイルに書き出して参照に置き換える。

    文書本体から base64 が消えるので、カード表示のたびに読み込む量が減り、
    画像は webview 側でキャッシュされる。
    同じ画像が複数回出てきた場合は内容のハッシュで 1 ファイルにまとめる。
    ファイル名はアンダースコア始まりなので Check Media の未使用判定から外れる。
    復元できない src（base64 でない data URI、壊れた base64）はそのまま残す。
    """
    by_hash: dict[str, str] = {}
    for img in soup.find_all("img"):
        src = img.get("src", "")
        m = DATA_URI_RE.match(src.strip())
        if not m:
            continue
        mime, b64 = m.group(1), re.sub(r"\s+", "", m.group(2))
        try:
            blob = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        if not blob:
            continue
        digest = hashlib.sha1(blob).hexdigest()
        name = by_hash.get(digest)
        if name is None:
            name = f"_ir2_{docid}_{len(by_hash) + 1:04d}.{_ext_for(mime)}"
            with open(os.path.join(col.media.dir(), name), "wb") as f:
                f.write(blob)
            by_hash[digest] = name
        img["src"] = name


# ---------------------------------------------------------------- edit / regen

def _with_para(col: Collection, docid: str, anchor: str, inner_html: str) -> tuple:
    """文書を読み、段落 anchor の中身を、後始末した inner_html にする（書くのは呼び出し側）。保存（save_edit）と
    Extract（extract_paragraph）が使う。戻り値は (文書の soup, 段落の要素, 利用者への知らせのリスト)。

    後始末: 編集で増えた同じ id の cloze を 1 個に戻す（tidy_clozes）。Enter と Backspace などでブラウザが書き込んだ色を
    外す（clean_styles）。
    """
    html, msgs = tidy_clozes(inner_html)
    soup = doc_soup(col, docid)
    el = _find_para(soup, anchor)
    _put_para(el, clean_styles(html))
    return soup, el, msgs


def save_edit(col: Collection, docid: str, anchor: str, inner_html: str) -> tuple:
    """段落の中身を作業コピーへ書き戻し（_with_para）、その段落のノートの Text を作り直す。

    書き戻しは ID を持つ要素の innerHTML の置き換え。
    戻り値は (更新したノート数, 利用者への知らせのリスト)。
    """
    soup, _el, msgs = _with_para(col, docid, anchor, inner_html)
    write_doc(col, docid, str(soup))
    return regen_notes(col, docid, anchor), msgs


def para_source(col: Collection, docid: str, anchor: str) -> str:
    """段落の中身の HTML。文書に書かれているとおり（PBIR → Source Text で表示し、編集する）。"""
    return _doc_para(col, docid, anchor).decode_contents()


# ブロックの要素。p の開始タグを閉じる要素（Chromium で確認した、ブラウザが p の中に置かない要素）と表の中の要素。
# 取り込みでは入れ物の直下のこれらをブロックとして扱う（segment）。Choices と Items の行の区切り（choice_lines）、
# カードの JS の線の範囲と貼り付けも同じものを使う（_doc_js に渡す）。
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "center", "details", "dialog", "dir",
    "div", "dl", "fieldset", "figcaption", "figure", "footer", "header", "hgroup", "main",
    "menu", "nav", "ol", "p", "search", "section", "summary", "ul",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "listing", "form", "li", "dd", "dt",
    "plaintext", "table", "hr", "xmp",
    "thead", "tbody", "tfoot", "tr", "td", "th",
}


def _is_blank(node) -> bool:
    return isinstance(node, NavigableString) and not node.strip()


def regen_notes(col: Collection, docid: str, anchor: str) -> int:
    """指定段落に紐づくノート（段落ノート、その段落内の cloze ノート、その段落の Basic、True or False、Multiple Choice、Enumeration）の
    Text を作り直す。利用者が書くフィールド（Question と Answer など）は文書から作るものではないので触らない。

    Text は文書から一方向に作る。
    """
    el = doc_soup(col, docid).find(attrs={"data-ir": anchor})
    if el is None:
        return 0
    updated = 0
    for note in _doc_notes(col, docid):
        ntype = note.note_type()["name"]
        aid = note["AnchorId"]
        if (ntype == PARA_MODEL or ntype in DERIVED_MODELS) and aid == anchor:
            note["Text"] = el.decode_contents()
            col.update_note(note)
            updated += 1
        elif ntype == CLOZE_MODEL and aid.startswith(anchor + "."):
            note["Text"] = cloze_text(el, aid)
            col.update_note(note)
            updated += 1
    return updated


# 分かち書きをしない文字（かな、漢字）。これらは語の切れ目を持たないので、段落の中の
# 部分文字列として探す（\b を使うと「統語論は」の「統語論」が見つからない）
_UNSPACED = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ"
                       r"\U00020000-\U0003ffff]")


def _joins(a: str, b: str) -> bool:
    """文字 a と b が隣り合うと 1 語の途中になるか（どちらも分かち書きをする文字や数字）。"""
    return all(c.isalnum() and not _UNSPACED.match(c) for c in (a, b))


def cloze_word(para_el, cloze_id: str) -> str:
    """cloze のマークの文字（選んだ語）。空白はまとめる。マークが無ければ空文字。"""
    span = para_el.find(attrs={"data-cloze": cloze_id})
    return para_text(span) if span is not None else ""


def cloze_text(para_el, cloze_id: str) -> str:
    """段落要素から cloze ノートの Text を作る。

    マークの文字（選んだ語）の段落の中の出現を、すべて {{c1::...}} にする。cloze 1 個につきノート 1 件なので
    序数は常に c1 で、Anki は 1 枚のカードで全部を隠す。マークの付いた箇所は必ず含める。
    ほかの出現の探し方:
      - 大文字と小文字を区別しない。語の中の空白は、空白の種類と数を問わない。
      - 語の途中は数えない（run は running の中では数えない）。かなと漢字は語の切れ目が無いので、
        部分文字列として数える。
      - 1 個のテキストノードの中にあるものだけ（<i>ru</i>n のように要素で割れているものは数えない）。
      - 数式（<math>）の中は数えない。cloze の印が MathML の中に入ると数式が壊れる。
    他の cloze のマークは外して中身だけ残す。
    マークが編集で消えていた場合は cloze 記法なしのテキストになり、
    カードは空になって Anki の Empty Cards の扱いに乗る。
    """
    copy = BeautifulSoup(str(para_el), "html.parser").find(attrs={"data-ir": True})
    own = copy.find(attrs={"data-cloze": cloze_id})
    for span in copy.find_all(attrs={"data-cloze": True}):
        if span is not own:
            span.unwrap()
    if own is None:
        return copy.decode_contents()
    word = cloze_word(copy, cloze_id)
    copy.smooth()  # 外したマークの前後で分かれたテキストノードをつなぐ
    if word:
        pat = re.compile(r"\s+".join(re.escape(w) for w in word.split()), re.IGNORECASE)
        for s in list(copy.find_all(string=True)):
            if type(s) is not NavigableString or any(
                    p is own or p.name == "math" for p in s.parents):
                continue
            out, pos = [], 0
            for m in pat.finditer(s):
                a, b = m.start(), m.end()
                if (a > 0 and _joins(s[a - 1], s[a])) or (b < len(s) and _joins(s[b - 1], s[b])):
                    continue
                out += [s[pos:a], "{{c1::", s[a:b], "}}"]
                pos = b
            if out:
                s.replace_with(NavigableString("".join(out) + s[pos:]))
    own.insert_before(NavigableString("{{c1::"))
    own.insert_after(NavigableString("}}"))
    own.unwrap()
    return copy.decode_contents()


# 文書の Source（記事の出典の URL）。作業コピーの先頭のコメントで、取り込み（import_article）と
# Add/Edit Source（set_doc_source）が書く（_with_source）
_SOURCE_RE = re.compile(r"\s*<!-- ir:source (.*?) -->\n?")


def _with_source(body: str, url: str) -> str:
    """Source のコメントの無い文書 body の先頭に、url の Source のコメントを付ける。url の前後の空白と、コメントを
    閉じてしまう "-->" は外す。空なら付けない。"""
    url = _clean_url(url)
    return (f"<!-- ir:source {url} -->\n" if url else "") + body


def _clean_url(url: str) -> str:
    return url.strip().replace("-->", "")


def doc_source(col: Collection, docid: str) -> str:
    """文書の Source。無ければ（文書のファイルが無いときも）空文字。"""
    try:
        m = _SOURCE_RE.match(read_doc(col, docid))
    except OSError:
        return ""
    return m.group(1) if m else ""


# ---------------------------------------------------------------- duplicates
# 取り込みダイアログの重複の警告（URL とファイル）と、PBIR → Check Duplicates（取り込み済みの文書どうし）。
# 比べるのはアドオンのノートのある文書だけ（ノートが 1 件も残っていない文書は数えない）。

def _ir_docids(col: Collection) -> list:
    """アドオンのノートのある文書の DocId（古い順）。ノートを 1 件ずつ読まずに、DB のフィールドの列から取る。"""
    nids = col.find_notes(models_search())
    rows = col.db.all(f"select mid, flds from notes where id in {ids2str(nids)}") if nids else []
    pos = {}   # ノートタイプ → DocId のフィールドの位置
    for mid, _flds in rows:
        if mid not in pos:
            pos[mid] = col.models.field_map(col.models.get(mid))["DocId"][0]
    return sorted({flds.split("\x1f")[pos[mid]] for mid, flds in rows}, key=_docid_order)


def _docid_order(docid: str) -> int:
    """DocId は取り込んだ時刻のミリ秒の 16 進（new_docid）。古い順に並べるため。"""
    try:
        return int(docid, 16)
    except ValueError:
        return 0


def docs_with_source(col: Collection, url: str) -> list:
    """Source が url と同じ文書の DocId（古い順）。取り込みダイアログの URL 欄の重複の警告に使う。

    url は取り込みと同じく前後の空白と "-->" を外してから比べる。同じとするのは文字列が完全に同じものだけ。
    """
    url = _clean_url(url)
    return [d for d in _ir_docids(col) if doc_source(col, d) == url] if url else []


def para_text(el) -> str:
    """段落の要素の文字（空白は 1 個にまとめ、前後を外す）。ファイルの重複を比べる単位。"""
    return " ".join(el.get_text().split())


def para_texts(units) -> set:
    """段落の要素のリストから、文字のある段落の文字の集合（画像だけの段落などは比べない）。"""
    return {t for t in (para_text(el) for el in units) if t}


# 原本（_ir2_<docid>.orig.html）の段落の文字の集合。原本は取り込みのあと変えないので、
# 一度読んだら Anki を閉じるまで覚えておく。キーは (media のフォルダ, DocId)
_orig_cache: dict = {}


def orig_para_texts(col: Collection, docid: str) -> set:
    """文書の原本の段落（data-ir の要素）の文字の集合。原本が無ければ空。

    作業コピーでなく原本を見るのは、取り込んだときの形と比べるため（作業コピーは編集、Extract、結合で変わる）。
    """
    key = (col.media.dir(), docid)
    if key not in _orig_cache:
        soup = _orig_soup(col, docid)
        if soup is None:
            return set()
        _orig_cache[key] = para_texts(soup.find_all(attrs={"data-ir": True}))
    return _orig_cache[key]


def _orig_soup(col: Collection, docid: str):
    """文書の原本。読めなければ None。"""
    try:
        return doc_soup(col, docid, orig=True)
    except OSError:
        return None


def doc_snippet(col: Collection, docid: str, n: int = 60) -> str:
    """文書を見分けるための短い文字。原本の最初の見出しか段落の文字の先頭 n 文字。"""
    soup = _orig_soup(col, docid)
    for el in soup.find_all(_HEADINGS + ["p", "li", "blockquote", "pre"]) if soup else []:
        t = para_text(el)
        if t:
            return _clip(t, n)
    return ""


def shared_paragraphs(col: Collection, units) -> list:
    """取り込もうとしている文書（prepare_article の段落の要素のリスト）の段落のうち、取り込み済みの文書の原本にも
    ある段落の数。戻り値は (DocId, 共通の段落の数) のリストで、数の多い順（同じなら古い順）。1 個も無い文書は入れない。

    比べるのは段落の文字（para_text）が完全に同じかどうか。全部の段落が同じでなくても数える。
    """
    texts = para_texts(units)
    if not texts:
        return []
    out = []
    for docid in _ir_docids(col):
        k = len(texts & orig_para_texts(col, docid))
        if k:
            out.append((docid, k))
    out.sort(key=lambda x: (-x[1], _docid_order(x[0])))
    return out


def library_duplicates(col: Collection) -> dict:
    """PBIR → Check Duplicates。取り込み済みの文書どうしの重複。

    戻り値:
      "same_source": [(Source, [DocId, ...]), ...] … Source が完全に同じ 2 個以上の文書。
      "shared": [(DocId a, DocId b, 共通の段落の数), ...] … 原本の段落の文字が同じ段落を持つ文書の組（a が古い）。
      "paras": {DocId: 原本の段落の数（文字のあるもの）}
      "docs": 調べた文書の数
    比べ方は取り込みダイアログの警告と同じ（docs_with_source, shared_paragraphs）。
    """
    docids = _ir_docids(col)
    by_source: dict = {}
    for d in docids:
        src = doc_source(col, d)
        if src:
            by_source.setdefault(src, []).append(d)
    same_source = [(src, ds) for src, ds in by_source.items() if len(ds) > 1]
    same_source.sort(key=lambda x: _docid_order(x[1][0]))
    owners: dict = {}  # 段落の文字 → それを持つ文書
    paras = {}
    for d in docids:
        texts = orig_para_texts(col, d)
        paras[d] = len(texts)
        for t in texts:
            owners.setdefault(t, []).append(d)
    pairs = Counter(pair for ds in owners.values() for pair in combinations(ds, 2))
    shared = [(a, b, k) for (a, b), k in pairs.items()]
    shared.sort(key=lambda x: (-x[2], _docid_order(x[0]), _docid_order(x[1])))
    return {"same_source": same_source, "shared": shared, "paras": paras, "docs": len(docids)}


def set_doc_source(col: Collection, docid: str, url: str) -> bool:
    """PBIR → Modify Document → Add/Edit Source。作業コピーの Source を url にする（取り込みと同じく _with_source）。
    原本（_ir2_<docid>.orig.html）は変えない。戻り値は作業コピーを書き換えたか。
    """
    doc = read_doc(col, docid)
    m = _SOURCE_RE.match(doc)
    new_doc = _with_source(doc[m.end():] if m else doc, url)
    if new_doc != doc:
        write_doc(col, docid, new_doc)
    return new_doc != doc


def _doc_cards(col: Collection, docid: str) -> list:
    """文書 docid のアドオンのノート（Paragraph, Cloze, Basic、True or False、Multiple Choice、Enumeration）のカード。"""
    return [col.get_card(cid) for cid in col.find_cards(f'"DocId:{docid}" {models_search()}')]


def _home_deck(card) -> int:
    """カードの元のデッキ。フィルターデッキに入っていれば元のデッキ（odid）、そうでなければ今のデッキ。"""
    return card.odid or card.did


def doc_decks(col: Collection, docid: str) -> set:
    """PBIR → Move Document でチェックを付けるデッキ。文書のカードの元のデッキの集合（ふつうは 1 個）。"""
    return {_home_deck(c) for c in _doc_cards(col, docid)}


def move_document(col: Collection, docid: str, deck_id: int) -> int:
    """PBIR → Move Document。文書のカードのうち、元のデッキが deck_id でないものを deck_id へ移す。戻り値は移した数。

    Anki の Change Deck と同じ col.set_deck を使う。フィルターデッキに入っているカードは、そこから出て deck_id に入る
    （26.09.2 の pylib で確かめた。Change Deck と同じ）。元のデッキが既に deck_id のカードは触らない（フィルターデッキから
    出さない）。文書は変えない。
    """
    cids = [c.id for c in _doc_cards(col, docid) if _home_deck(c) != deck_id]
    if cids:
        col.set_deck(cids, deck_id)
    return len(cids)


# PBIR → New Deck で作るデッキ。下位のデッキの名前と、デッキに付けるプリセット
PBIR_DECK = "PBIR"
PBIR_PRESET = "PBIR"
# 下位のデッキ（この順に作る）と、その上限（デッキのオプションの This deck。デッキに保存される newLimit と reviewLimit）。
# 上のデッキには付けない
PBIR_LIMITS = {
    "Tier 1": (128, 128), "Tier 2": (64, 128), "Tier 3": (32, 128), "Tier 4": (16, 128), "Tier 5": (8, 128),
    "Tier 6": (4, 128), "Tier 7": (2, 128), "Tier 8": (1, 128), "Trash": (0, 0),
}
# 同じ名前のプリセットが無いときに作るプリセットの値のうち、Anki の新しいプリセット（add_config）と違うもの
PBIR_PRESET_VALUES = {
    "maxTaken": 600, "timer": 1,
    "new": {"bury": True, "delays": [10080.0], "initialFactor": 1500, "ints": [47, 337, 0], "perDay": 256},
    "rev": {"bury": True, "ease4": 2.0, "hardFactor": 0.99, "ivlFct": 0.8, "perDay": 512},
    "lapse": {"delays": [10080.0, 15840.0, 18720.0], "leechAction": 0, "leechFails": 3, "minInt": 17},
    "newSortOrder": 1, "buryInterdayLearning": True,
}


def pbir_deck_name(col: Collection) -> str:
    """PBIR → New Deck で作るデッキの名前。PBIR が既にあれば PBIR 2, PBIR 3, ... のうち、まだ無い最初のもの。
    Anki のデッキの名前は大文字と小文字を区別しない（by_name）。"""
    name, n = PBIR_DECK, 1
    while col.decks.by_name(name):
        n += 1
        name = f"{PBIR_DECK} {n}"
    return name


def pbir_preset(col: Collection) -> dict:
    """名前が PBIR のプリセット。無ければ PBIR_PRESET_VALUES で作る。あれば値は変えない（同じ名前が複数あれば id の小さいもの）。"""
    found = sorted((c for c in col.decks.all_config() if c["name"] == PBIR_PRESET), key=lambda c: c["id"])
    if found:
        return found[0]
    conf = col.decks.add_config(PBIR_PRESET)
    for key, val in PBIR_PRESET_VALUES.items():
        if isinstance(val, dict):
            conf[key].update(val)
        else:
            conf[key] = val
    col.decks.update_config(conf)
    return conf


def create_pbir_deck(col: Collection) -> tuple:
    """PBIR → New Deck。空のデッキ（pbir_deck_name）と、その下に Tier 1 から Tier 8 と Trash を作り、どれにも
    プリセット PBIR（pbir_preset）を付け、下位のデッキには PBIR_LIMITS の上限を付ける。デッキを作る部分は Anki の undo で 1 回の操作（"New PBIR Deck"）にまとめる。
    プリセットを作るとき（add_config, update_config）は Anki が undo の履歴を空にし、undo でも消えない（26.09.2 の pylib で
    確かめた）ので、その前に作る。戻り値は (デッキの名前, Anki の OpChanges)。__init__.py は CollectionOp で呼ぶ。"""
    conf_id = pbir_preset(col)["id"]
    name = pbir_deck_name(col)
    pos = col.add_custom_undo_entry("New PBIR Deck")
    for sub in (None, *PBIR_LIMITS):
        deck = col.decks.get(col.decks.add_normal_deck_with_name(f"{name}::{sub}" if sub else name).id)
        deck["conf"] = conf_id
        if sub:
            deck["newLimit"], deck["reviewLimit"] = PBIR_LIMITS[sub]
        col.decks.save(deck)
    return name, col.merge_undo_entries(pos)


def next_cloze_id(col: Collection, docid: str, anchor: str) -> str:
    """その段落で未使用の cloze ID を返す。文書側のマークと既存ノートの両方を見る。"""
    used = set(_cloze_ids(doc_soup(col, docid)))
    used.update(note["AnchorId"] for note in _doc_notes(col, docid, (CLOZE_MODEL,)))
    n = 1
    while f"{anchor}.c{n}" in used:
        n += 1
    return f"{anchor}.c{n}"


def make_cloze(col: Collection, docid: str, anchor: str, inner_html: str, deck_id: int) -> tuple:
    """PBIR → Create → Cloze。inner_html は段落の中身で、選んだ所に data-cloze="PENDING" の印が 1 個ある。
    印に未使用の ID を振り（next_cloze_id）、段落を保存し（save_edit）、cloze ノートを 1 件作る（create_cloze）。
    選んだ語が空（空白や画像だけの選択）なら、何も変えずに断る。戻り値は (cloze の ID, 選んだ語, 隠す出現の数)。
    """
    cloze_id = next_cloze_id(col, docid, anchor)
    marker = f'data-cloze="{PENDING}"'
    if inner_html.count(marker) != 1:
        raise ValueError(f"expected one new cloze mark, found {inner_html.count(marker)}")
    inner_html = inner_html.replace(marker, f'data-cloze="{cloze_id}"')
    word = cloze_word(BeautifulSoup(inner_html, "html.parser"), cloze_id)
    if not word:
        raise Refused("select a word (the selection has no text)")
    save_edit(col, docid, anchor, inner_html)
    nid = create_cloze(col, docid, anchor, cloze_id, deck_id)
    return cloze_id, word, col.get_note(nid)["Text"].count("{{c1::")


def create_cloze(col: Collection, docid: str, anchor: str, cloze_id: str,
                 deck_id: int) -> int:
    """文書に書かれた data-cloze マークから cloze ノートを 1 件作る。戻り値は note id。

    マークは先に save_edit で文書へ入っている前提。
    """
    el = _doc_para(col, docid, anchor)
    if el.find(attrs={"data-cloze": cloze_id}) is None:
        raise KeyError(f"cloze {cloze_id} not found in the document")
    return _add_note(col, CLOZE_MODEL, docid, cloze_id, cloze_text(el, cloze_id), deck_id)


def _qa_html(html: str) -> str:
    """問いか答えの HTML（学習画面で編集した要素の innerHTML）を、フィールドに入れる形にする。

    段落の保存と同じく、スクリプトを動かしうるもの（sanitize）と、編集でブラウザが書き込んだ色（clean_styles）を外す。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    sanitize(soup)
    return clean_styles(str(soup)).strip()


def choice_lines(html: str) -> list:
    """Multiple Choice の Choices と Enumeration の Items の行（HTML のリスト）。Choices は 1 行目が正しい選択肢、
    2 行目からが誤りの選択肢。Items は上から順の項目。

    行の区切りは、いちばん外側の <br> と、ブロックの要素（編集で Enter を押すと Chromium が入れる div など。BLOCK_TAGS）の
    境目。中身の無い行（空白と <br> だけ）は数えない。コメントは落とす。_doc_js の choiceLines と同じ決め方。
    """
    lines, cur = [], []

    def end():
        h = "".join(cur).strip()
        cur.clear()
        if _has_content(BeautifulSoup(h, "html.parser")):
            lines.append(h)

    def walk(node):
        for c in list(node.children):
            if isinstance(c, Comment):
                continue
            if isinstance(c, Tag) and c.name == "br":
                end()
            elif isinstance(c, Tag) and c.name in BLOCK_TAGS:
                end()
                walk(c)
                end()
            else:
                cur.append(c.decode() if isinstance(c, Tag) else c.output_ready())

    walk(BeautifulSoup(html, "html.parser"))
    end()
    return lines


def enum_items(lines: list) -> str:
    """Enumeration の項目（HTML の行のリスト。上から順）を Items の形にする。項目ごとに cloze 1 個で、番号は後ろの項目から
    c1, c2, ...（3 項目なら {{c3::1 番目}}<br>{{c2::2 番目}}<br>{{c1::3 番目}}）。"""
    n = len(lines)
    return "<br>".join("{{c%d::%s}}" % (n - i, line) for i, line in enumerate(lines))


def prepare_derived(kind: str, fields: dict) -> dict:
    """Basic、True or False、Multiple Choice、Enumeration のノートに入れるフィールドの値を作る。フィールドの名前が合わなければ ValueError。

    fields は {フィールド名: 学習画面で編集した HTML} で、利用者が書くフィールドが全部要る。空のフィールドも、行の足りないフィールドも
    そのまま保存する（足りない行は空として扱う）。Choices と Items は行に分け（choice_lines）、Choices は行を <br> でつなぐ
    （表示の JS は行に分けて使い、無い行は空として出す）。Items は enum_items の形にする。項目が 1 個も無ければ空の項目 1 個にする
    （cloze 型のノートは cloze が無いとカードができないので。空の cloze も Anki は cloze として数える）。
    """
    model, _label, content = DERIVED[kind]
    if set(fields) != set(content):
        raise ValueError(f"fields {sorted(fields)} do not match {model}")
    vals = {name: _qa_html(html) for name, html in fields.items()}
    if "Choices" in vals:
        vals["Choices"] = "<br>".join(choice_lines(vals["Choices"]))
    if "Items" in vals:
        vals["Items"] = enum_items(choice_lines(vals["Items"]) or [""])
    return vals


def _add_derived(col: Collection, docid: str, anchor: str, model: str, vals: dict, deck_id: int) -> int:
    return _add_note(col, model, docid, anchor, para_source(col, docid, anchor), deck_id, fields=vals)


def _derived_note(col: Collection, docid: str, anchor: str, model: str, nid):
    note = col.get_note(nid)
    if (note.note_type()["name"] != model or note["DocId"] != docid
            or note["AnchorId"] != anchor):
        raise KeyError(f"note {nid} is not the {model} note of {docid} {anchor}")
    return note


def save_derived(col: Collection, docid: str, anchor: str, kind: str, nid, fields: dict,
                 deck_id: int) -> int:
    """段落 anchor の Basic などのノート（kind は DERIVED のキー）に、利用者が書くフィールドを保存する。戻り値は note id。

    fields は {フィールド名: 学習画面で編集した HTML}（prepare_derived）。nid が None なら新しく作る（PBIR → Create → Basic など）。
    nid があればそのノートのフィールドを書き換える（PBIR → Create → Edit）。Enumeration は書き換えない（recreate_derived で作り直す）。
    文書は変えない。
    """
    model = DERIVED[kind][0]
    if kind == "enum" and nid is not None:
        raise ValueError("Enumeration notes are not edited (use Recreate Enumeration)")
    vals = prepare_derived(kind, fields)
    if nid is None:
        return _add_derived(col, docid, anchor, model, vals, deck_id)
    note = _derived_note(col, docid, anchor, model, nid)
    for name, v in vals.items():
        note[name] = v
    col.update_note(note)
    return note.id


def recreate_derived(col: Collection, docid: str, anchor: str, kind: str, old_nid, fields: dict,
                     deck_id: int) -> None:
    """Basic などのノート old_nid を捨て、同じ段落に fields で新しいノートを作る（PBIR → Create → Recreate Enumeration）。

    作ってから古いノートを消し（カードと学習履歴も消える）、Anki の undo では
    1 回の操作（"Recreate ..."）にまとめる。新しいノートのカードは新規キューの末尾に付く。文書は変えない。
    """
    model, label, _content = DERIVED[kind]
    vals = prepare_derived(kind, fields)
    _derived_note(col, docid, anchor, model, old_nid)
    pos = col.add_custom_undo_entry(f"Recreate {label}")
    _add_derived(col, docid, anchor, model, vals, deck_id)
    col.remove_notes([old_nid])
    col.merge_undo_entries(pos)


# ---------------------------------------------------------------- cloze の印の後始末

# JS は選んだ所を data-cloze="PENDING" の span で包んで送る（make_cloze が id を振る）。JS は返された id で置き換える
PENDING = "PENDING"


def _cloze_ids(el) -> list:
    return [s["data-cloze"] for s in el.find_all(attrs={"data-cloze": True})]


def _cloze_num(aid: str) -> int:
    m = re.search(r"\.c(\d+)$", aid)
    return int(m.group(1)) if m else 0


def _clip(text: str, n: int) -> str:
    """利用者への知らせに出す文字の先頭 n 文字（長ければ ... を付ける）。"""
    return text if len(text) <= n else text[:n] + "..."


def _snippet(span, n: int = 30) -> str:
    return f'"{_clip(para_text(span), n)}"'


def _merge_adjacent(el, ids: set) -> None:
    """同じ id の span が直に隣り合っていたら 1 個にまとめる。

    段落の中で Enter と Backspace を押すと、ブラウザは cloze の span を二つに割り、
    戻したあとも割れたまま（片方に style が付く）になる。
    """
    for span in el.find_all(attrs={"data-cloze": True}):
        if span.parent is None:   # 前の span にまとめた
            continue
        while (span["data-cloze"] in ids and isinstance(span.next_sibling, Tag)
               and span.next_sibling.get("data-cloze") == span["data-cloze"]):
            nxt = span.next_sibling.extract()
            for c in list(nxt.contents):
                span.append(c.extract())


def _drop_copies(el, ids: set) -> list:
    """同じ id の span が 2 個以上あれば、文書順で最初の 1 個だけを cloze として残す。

    残りはマークを外して中身だけにする。戻り値は外した span の中身の抜粋のリスト。
    """
    seen, dropped = set(), []
    for span in el.find_all(attrs={"data-cloze": True}):
        i = span["data-cloze"]
        if i not in ids:
            continue
        if i in seen:
            dropped.append(_snippet(span))
            span.unwrap()
        else:
            seen.add(i)
    return dropped


def tidy_clozes(inner_html: str) -> tuple:
    """保存しようとする段落の HTML で、編集によって増えた同じ id の span を 1 個に戻す。

    増える経路: cloze の中で Enter を押す（span が二つに割れ、別のブロックに分かれる。Chromium で確認）と、
    Source Text で span を書き写す。（貼り付けでは増えない。カードの JS が貼り付ける HTML から data-cloze を外す。）
    隣り合う span はまとめ、それでも残るものは文書順で最初の 1 個だけを残す。
    戻り値は (HTML, 利用者への知らせのリスト)。
    """
    frag = BeautifulSoup(inner_html, "html.parser")
    new_ids = _cloze_ids(frag)
    targets = {i for i in new_ids if new_ids.count(i) > 1}
    if not targets:
        return inner_html, []
    _merge_adjacent(frag, targets)
    dropped = _drop_copies(frag, targets)
    msgs = [f"Cloze mark removed from copied or split text {d}" for d in dropped]
    return str(frag), msgs


# ---------------------------------------------------------------- 書き込まれた色の後始末
#
# Chromium は段落の編集（Enter のあとの Backspace による結合、貼り付け）のとき、
# 動かした部分の計算済みの色を style 属性として書き込む（Playwright の Chromium で確認:
# Enter と Backspace で style="color: rgb(17, 17, 17)"、薄くしてある段落からの貼り付けで
# style="color: rgb(155, 155, 155)"）。この色はアドオンの CSS の薄化・強調の色そのもので、
# 属性の style は CSS より強いので、その部分は薄くならない（または薄いまま戻らない）。
# アドオンの CSS の色と一致する宣言だけを外す。記事がもともと持っている色には触らない。

_COLOR_PROPS = ("color", "-webkit-text-fill-color", "caret-color", "text-decoration-color")
_BG_DEFAULT_TOKENS = {"none", "0%", "/", "auto", "repeat", "scroll", "padding-box",
                      "border-box", "initial"}
_COLOR_RE = re.compile(r"rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}\b|\btransparent\b")


def _rgb(value: str):
    """色の値を (r, g, b, a) にする。読めなければ None。"""
    v = value.strip().lower()
    if v == "transparent":
        return (0, 0, 0, 0.0)
    m = re.fullmatch(r"#([0-9a-f]{3}|[0-9a-f]{6})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    if m:
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), a)
    return None


def _palette() -> set:
    """アドオンの CSS の --ir-* の色（昼と夜の両方）。_css() から読むので CSS と食い違わない。"""
    return {_rgb(h) for h in re.findall(r"--ir-[a-z]+:\s*(#[0-9a-fA-F]{6})", _css())}


def _injected(prop: str, value: str, palette: set) -> bool:
    if prop in _COLOR_PROPS:
        return _rgb(value) in palette
    if prop == "background":
        colors = _COLOR_RE.findall(value)
        rest = _COLOR_RE.sub(" ", value).split()
        if len(colors) != 1 or any(t.lower() not in _BG_DEFAULT_TOKENS for t in rest):
            return False
        value, prop = colors[0], "background-color"
    if prop == "background-color":
        c = _rgb(value)   # 透明な背景も外す
        return c in palette or (c is not None and c[3] == 0)
    return False


def strip_injected_styles(root) -> int:
    """root の子孫から、アドオンの色と一致する style の宣言を外す。戻り値は変えた要素の数。

    宣言が無くなった style 属性は消し、属性が何も無くなった span は中身だけにする
    （Chromium が色を書くために作った入れ物なので）。
    """
    palette = _palette()
    n = 0
    for el in list(root.find_all(style=True)):
        decls = [d for d in el["style"].split(";") if d.strip()]
        keep = []
        for d in decls:
            prop, sep, value = d.partition(":")
            if sep and _injected(prop.strip().lower(), value, palette):
                continue
            keep.append(d.strip())
        if len(keep) == len(decls):
            continue
        n += 1
        if keep:
            el["style"] = "; ".join(keep) + ";"
        else:
            del el["style"]
            if el.name == "span" and not el.attrs:
                el.unwrap()
    return n


def clean_styles(inner_html: str) -> str:
    """保存しようとする段落の HTML から、編集で書き込まれた色を外して返す。"""
    frag = BeautifulSoup(inner_html, "html.parser")
    if not strip_injected_styles(frag):
        return inner_html
    return str(frag)


# ---------------------------------------------------------------- Extract と結合
#
# PBIR → Extract, Merge Previous, Merge Next。どれも文書とノートの両方を変える。
# 文書は media のファイルで Anki の undo の対象外なので、undo で片方だけが戻らないように、
# 操作のあと undo の履歴を空にする（discard_undo。cloze の作成も同じ）。
# 断る場合は Refused を投げる。どの場合も、文書にもノートにも書く前に判定する。

SPLIT_MARK = "data-ir-split"   # JS が選択の始まり（値は start）と終わり（end）に入れる印の属性（extract_paragraph）
_MEDIA_TAGS = {"img", "svg", "video", "audio", "canvas", "iframe", "object", "embed", "math"}
_NOT_COPIED = ("data-ir", "id", "contenteditable")  # 分けるときに新しい側の要素に写さない属性


class Refused(ValueError):
    """利用者の操作を断る理由。GUI は警告ではなく短い通知で出す。"""


def discard_undo(col: Collection) -> None:
    """Anki の undo と redo の履歴を空にする。

    Anki に履歴を消す API は無い。col.db 経由で select 以外の SQL を送ると、バックエンドが
    undo の履歴と学習キューを捨てる（26.09.2 の rslib/src/backend/dbproxy.rs の
    update_state_after_modification と rslib/src/undo/mod.rs の discard_undo_and_study_queues）。
    何も変えない update でそれを起こす。
    学習キューは、ノートを足したり消したりした時点で既に捨てられている（ops.rs の
    requires_study_queue_rebuild）。
    """
    col.db.execute("update col set mod = mod")


def _has_content(el) -> bool:
    """文字か、画像などの要素を持つか。空白と <br> だけなら持たない。"""
    return bool(el.get_text().strip()) or el.find(lambda t: t.name in _MEDIA_TAGS) is not None


def remove_empty(col: Collection, docid: str) -> tuple:
    """カードを評価したあと、次のカードを出す前に呼ぶ（__init__.py の reviewer_did_answer_card）。

    文書 docid から次のものを消す。
      - 中身の無い段落（_has_content。取り込みで段落にしない要素と同じ決め方）の要素。
      - 生きていない段落のアドオンのノート（Paragraph, Cloze, Basic、True or False、Multiple Choice、Enumeration）。
        段落が生きているのは、文書に中身のある要素があり、段落のノートがあるとき。中身の無い段落のノートと、
        利用者が段落のノートを消したあとに残ったその段落の cloze などのノートがこれに当たる。
      - cloze の無い IR2 Cloze のノート。Anki の決め方（Note.cloze_numbers_in_fields に 1 が無い）で、
        これは Anki が「No cloze 1 found」を出し、Empty Cards が挙げるものと同じ（26.09.2 の pylib で確かめた）。
      - IR2 Cloze のノートが指していない cloze のマーク（利用者がノートを消したあとと、上で消したあとに残ったもの）。
        マークを外して中身は残し、その段落のノートの Text を作り直す。
    原本は変えない。戻り値は (消した段落 ID のリスト, 消したノートの数, 外したマークの数)。
    """
    soup = doc_soup(col, docid)
    empty = [el for el in soup.find_all(attrs={"data-ir": True}) if not _has_content(el)]
    paras = {el["data-ir"] for el in empty}
    for el in empty:
        el.decompose()
    notes = _doc_notes(col, docid)
    live = ({el["data-ir"] for el in soup.find_all(attrs={"data-ir": True})}
            & {n["AnchorId"] for n in notes if n.note_type()["name"] == PARA_MODEL})
    remove = [n.id for n in notes if n["AnchorId"].split(".")[0] not in live or (
        n.note_type()["name"] == CLOZE_MODEL and 1 not in n.cloze_numbers_in_fields())]
    kept = {n["AnchorId"] for n in notes if n.note_type()["name"] == CLOZE_MODEL and n.id not in remove}
    if remove:
        col.remove_notes(remove)
    orphans = [s for s in soup.find_all(attrs={"data-cloze": True}) if s["data-cloze"] not in kept]
    touched = {s.find_parent(attrs={"data-ir": True})["data-ir"] for s in orphans}
    for s in orphans:
        s.unwrap()
    if empty or orphans:
        write_doc(col, docid, str(soup))
    for anchor in touched:
        regen_notes(col, docid, anchor)
    return sorted(paras, key=_para_num), len(remove), len(orphans)


def _shallow_copy(soup, tag):
    attrs = {k: v for k, v in tag.attrs.items() if k not in _NOT_COPIED}
    return soup.new_tag(tag.name, attrs=attrs)


def _ancestors_below(node, el) -> list:
    """node から el の手前までの祖先（内側から）。"""
    out, p = [], node.parent
    while p is not el:
        if p is None:
            raise KeyError("the split position is outside the paragraph")
        out.append(p)
        p = p.parent
    return out


def _content_parts(el, marks: list, media: bool = True) -> list:
    """el の中身を marks（文書順）で区切った部分ごとに、中身（空白でない文字。media なら画像などの要素も）があるか。"""
    parts, i = [False] * (len(marks) + 1), 0
    for node in el.descendants:
        if i < len(marks) and node is marks[i]:
            i += 1
        elif (isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip()) or (
                media and isinstance(node, Tag) and node.name in _MEDIA_TAGS):
            parts[i] = True
    return parts


def _lift_mark(mark, box, media: bool = True) -> bool:
    """mark の前か後ろに box の中身が無ければ（_content_parts）、mark を box の前か後ろへ出す。出したら True。"""
    before, after = _content_parts(box, [mark], media)
    if not before:
        box.insert_before(mark.extract())
    elif not after:
        box.insert_after(mark.extract())
    else:
        return False
    return True


def _move_mark_out_of_cloze(mark, el) -> None:
    """選択の端の印が cloze の端にあれば（文字だけを見る）外へ出す。途中にあれば断る。

    cloze の span を二つに割ると同じ id が二つになり、どちらが本体か決まらない（5 章）。
    """
    while True:
        cl = next((a for a in _ancestors_below(mark, el) if a.has_attr("data-cloze")), None)
        if cl is None:
            return
        if not _lift_mark(mark, cl, media=False):
            raise Refused("the selection starts or ends inside a cloze")


def _split_at(soup, el, mark):
    """el を mark の位置で二つに分け、mark より後ろを入れた新しい要素を返す（文書にはまだ入れない）。

    mark と el の間にある要素（i, mark など）は、同じ要素を両側に作って分ける。
    分けた結果、中身が無くなった側の要素は消す。
    """
    touched = []
    node, right = mark, None
    while node.parent is not el:
        parent = node.parent
        clone = _shallow_copy(soup, parent)
        if right is not None:
            clone.append(right)
        for sib in list(node.next_siblings):
            clone.append(sib.extract())
        touched += [parent, clone]
        right, node = clone, parent
    new_el = _shallow_copy(soup, el)
    if right is not None:
        new_el.append(right)
    for sib in list(node.next_siblings):
        new_el.append(sib.extract())
    mark.decompose()
    for t in touched:  # 内側から順に見るので、外側が空になった場合も消える
        if not t.contents:
            t.extract()
    return new_el


def _para_num(aid: str) -> int:
    m = re.fullmatch(r"p(\d+)", aid.split(".")[0])
    return int(m.group(1)) if m else 0


def _new_para_id(col: Collection, docid: str, soup) -> str:
    """文書とノートのどちらでも使われていない段落 ID。既存の最大の番号 + 1。"""
    nums = [_para_num(e["data-ir"]) for e in soup.find_all(attrs={"data-ir": True})]
    nums += [_para_num(note["AnchorId"]) for note in _doc_notes(col, docid)]
    return f"p{max(nums, default=0) + 1}"


def _cloze_notes(col: Collection, docid: str, para: str) -> dict:
    """段落 para の cloze ノート。{AnchorId: note}"""
    return {note["AnchorId"]: note for note in _doc_notes(col, docid, (CLOZE_MODEL,))
            if note["AnchorId"].split(".")[0] == para}


def _reanchor(col: Collection, notes: dict, moved: dict) -> int:
    """notes（{AnchorId: note}）のうち moved（{古い ID: 新しい ID}）にあるものを新しい ID に向ける。戻り値は向けた数。"""
    n = 0
    for old, new in moved.items():
        if old in notes:
            notes[old]["AnchorId"] = new
            col.update_note(notes[old])
            n += 1
    return n


def _renumber_clozes(el, anchor: str, n: int) -> dict:
    """el の中の cloze のマークを、段落 anchor の cloze として n + 1 から文書順に振り直す（Extract と結合で移った cloze）。
    戻り値は {古い ID: 新しい ID}。"""
    moved = {}
    for n, span in enumerate(el.find_all(attrs={"data-cloze": True}), n + 1):
        moved[span["data-cloze"]] = span["data-cloze"] = f"{anchor}.c{n}"
    return moved


def _list_start(ol) -> int:
    """ol の最後の項目の次の番号（li の value と ol の start を見る。reversed は扱わない）。"""
    n = int(ol["start"]) - 1 if str(ol.get("start", "")).lstrip("-").isdigit() else 0
    for li in ol.find_all("li", recursive=False):
        n = int(li["value"]) if str(li.get("value", "")).lstrip("-").isdigit() else n + 1
    return n + 1


def _cut(col: Collection, docid: str, soup, el, mark, direction: str):
    """段落の div el を印 mark の位置で分け、mark の前（prev）か後ろ（next）を新しい ID の段落の div にして el の隣に入れる。
    戻り値は (その div, {古い cloze の ID: 新しい ID})。

    印は、それを囲む要素の先頭にあればその要素の前へ、末尾にあれば後ろへ出す（div の直下に着くまで）。
      - div の直下に着いたら（ブロックの境目）、ブロックは変えず、div をそこで二つに分ける。
      - リストの項目の間に着いたら、リストをそこで二つに分け（ol の後ろ側には番号が続くように start を付ける）、div も分ける。
      - それ以外の要素の途中なら、そのブロック（p など）を二つに分け（_split_at）、div も分ける。
        ただしリストの項目の途中なら断る。
    分ける位置の前後の空白と <br> は取り除かない。新しい div の中の cloze のマークは、新しい段落の cloze として振り直す。
    """
    while mark.parent is not el and _lift_mark(mark, mark.parent):
        pass
    if mark.parent is el:
        start = mark.next_sibling
        mark.decompose()
    else:
        block = next(a for a in [mark.parent] + _ancestors_below(mark.parent, el) if a.parent is el)
        if block.name in LIST_TAGS and mark.parent is not block:
            raise Refused("in a list, select whole items")
        new_block = _split_at(soup, block, mark)
        if block.name == "ol" and not block.has_attr("reversed"):
            new_block["start"] = str(_list_start(block))
        block.insert_after(new_block)
        start = new_block
    if direction == "next":
        part = [start] + list(start.next_siblings)
    else:
        part = list(reversed(list(start.previous_siblings)))
    new_id = _new_para_id(col, docid, soup)
    new_el = soup.new_tag("div")
    new_el["data-ir"] = new_id
    for t in part:
        new_el.append(t.extract())
    moved = _renumber_clozes(new_el, new_id, 0)
    if direction == "next":
        el.insert_after(new_el)
        el.insert_after(NavigableString("\n"))
    else:
        el.insert_before(new_el)
        el.insert_before(NavigableString("\n"))
    return new_el, moved


def extract_paragraph(col: Collection, docid: str, anchor: str, inner_html: str, deck_id: int) -> tuple:
    """PBIR → Extract。段落 anchor のうち選んだ部分だけを残し、選んでいない前と後ろを新しい段落にする。
    戻り値は (前の部分の段落の ID, 後ろの部分の段落の ID)。中身の無い側は分けず、その ID は None。

    inner_html は段落の div の中身で、選択の始まりと終わりに印の span（SPLIT_MARK が start と end）が入っている。
    まずそれを保存と同じに後始末して段落に入れる（_with_para）。前の部分は始まりの印で、後ろの部分は終わりの印で分ける（_cut）。
    新しい段落にはそれぞれ段落ノートを 1 件作る（文書の順。カードは新規キューの末尾に付く）。選んだ部分は元の段落とそのノートのまま。
    新しい段落の側にある cloze はそこへ移り、id を振り直す（ノートとカードはそのまま）。
    元の段落の Basic、True or False、Multiple Choice、Enumeration のノートは元の段落に残る。
    選択の端が cloze の途中か数式（<math>）の中なら断る。選んだ部分に中身が無いとき、前と後ろのどちらにも中身が無いときも断る。
    """
    soup, el, _msgs = _with_para(col, docid, anchor, inner_html)   # save_edit と同じ
    marks = {m[SPLIT_MARK]: m for m in el.find_all(attrs={SPLIT_MARK: True})}
    if len(el.find_all(attrs={SPLIT_MARK: True})) != 2 or set(marks) != {"start", "end"}:
        raise Refused("the selection was not found")
    ends = [marks["start"], marks["end"]]
    for mark in ends:
        if any(a.name == "math" for a in _ancestors_below(mark, el)):
            # 数式の途中で分けると <math> が二つに割れ、どちらの数式も壊れる（2026-09-26 に確認）
            raise Refused("the selection starts or ends inside a formula")
        _move_mark_out_of_cloze(mark, el)
    before, selected, after = _content_parts(el, ends)
    if not selected:
        raise Refused("the selection has no text")
    if not before and not after:
        raise Refused("select only part of the paragraph")
    cut = [_cut(col, docid, soup, el, ends[0], "prev") if before else None,
           _cut(col, docid, soup, el, ends[1], "next") if after else None]
    for mark in ends:
        if not mark.decomposed:
            mark.decompose()
    notes = _cloze_notes(col, docid, anchor)
    write_doc(col, docid, str(soup))
    for new_el, moved in filter(None, cut):
        _add_note(col, PARA_MODEL, docid, new_el["data-ir"], new_el.decode_contents(), deck_id)
        _reanchor(col, notes, moved)
        regen_notes(col, docid, new_el["data-ir"])
    regen_notes(col, docid, anchor)
    return tuple(c and c[0]["data-ir"] for c in cut)


def _adjacent_para(el, direction: str):
    """カードの div el の直前（prev）か直後（next）の兄弟がカードの div ならそれを返す。そうでなければ None。

    間にあってよいのは空白とコメントだけ。見出しや図など、カードでないブロックが間にあれば断る。
    """
    sib = el.previous_sibling if direction == "prev" else el.next_sibling
    while sib is not None and (isinstance(sib, Comment) or _is_blank(sib)):
        sib = sib.previous_sibling if direction == "prev" else sib.next_sibling
    return sib if isinstance(sib, Tag) and sib.has_attr("data-ir") else None


def merge_paragraph(col: Collection, docid: str, anchor: str, direction: str) -> tuple:
    """直前（prev）か直後（next）のカードを、表示中のカード anchor に取り込む。

    取り込んだカードの div の中身（ブロック）を、表示中のカードの div の先頭（prev）か末尾（next）へそのまま移し、
    取り込んだカードの div を消す。ブロックのタグと中身は変えない。中身の無い側（文字を全部消したカード）は中身を捨てる。
    取り込んだカードの段落ノートは削除する（学習履歴も消える）。その cloze は anchor のカードへ移し、id を振り直す
    （ノートとカードはそのまま）。Basic、True or False、Multiple Choice、Enumeration のノートも anchor のカードを指すようにする
    （ノートとカードはそのまま）。文書にマークの無い cloze のノートは移さない（cloze が無いので、評価のあとに消える。remove_empty）。
    戻り値は (取り込んだカードの ID, anchor のカードへ移したノートの数, 削除した段落ノートの数)。
    """
    soup = doc_soup(col, docid)
    el = _find_para(soup, anchor)
    other = _adjacent_para(el, direction)
    if other is None:
        where = "before" if direction == "prev" else "after"
        raise Refused(f"there is no paragraph right {where} this one with nothing in between")
    other_id = other["data-ir"]

    # 番号は、anchor の cloze のマークとノートのどちらでも使われていないもの（next_cloze_id と同じ）
    used = set(_cloze_ids(el)) | set(_cloze_notes(col, docid, anchor))
    moved = _renumber_clozes(other, anchor, max(map(_cloze_num, used), default=0))

    for x in (el, other):
        if not _has_content(x):
            x.clear()
    kids = [c.extract() for c in list(other.contents)]
    if direction == "prev":
        for c in reversed(kids):
            el.insert(0, c)
    else:
        for c in kids:
            el.append(c)
    other.decompose()
    write_doc(col, docid, str(soup))

    n_moved = _reanchor(col, _cloze_notes(col, docid, other_id), moved)
    para_nids = [note.id for note in _doc_notes(col, docid, (PARA_MODEL,)) if note["AnchorId"] == other_id]
    if para_nids:
        col.remove_notes(para_nids)
    for note in _doc_notes(col, docid, DERIVED_MODELS):
        if note["AnchorId"] == other_id:
            note["AnchorId"] = anchor
            col.update_note(note)
            n_moved += 1
    regen_notes(col, docid, anchor)
    return other_id, n_moved, len(para_nids)


# ---------------------------------------------------------------- templates

def _css() -> str:
    # ダイミングは data-ir の有無に依存させない。文書全体を薄い色にし、
    # 対象（.ir-focus）とその子孫だけを通常色に戻す。色は継承するので、
    # id の付かない要素、入れ子、裸のテキストノードもまとめて薄くなる。
    # 優先度: "#ir-doc *" は (1,0,0)、"#ir-doc .ir-focus *" は (1,1,0) で後者が勝つ。
    # 夜間モードは変数だけ差し替えるので優先度が衝突しない。
    return """
/* 両端揃え。記事が要素に書いた text-align はこれより強いので、そのまま出る */
.card { font-family: sans-serif; font-size: 17px; text-align: justify;
        max-width: 46em; margin: 0 auto; }
/* 見出しは両端揃えにしない。start は左から右の文では左 */
#ir-doc :is(h1, h2, h3, h4, h5, h6) { text-align: start; }
#ir-doc { --ir-fg: #111111; --ir-dim: #9b9b9b; --ir-mark: #ffe08a;
        --ir-cloze: #cfe6ff; }
.night_mode #ir-doc { --ir-fg: #e9e9e9; --ir-dim: #6a6a6a; --ir-mark: #7a6420;
        --ir-cloze: #1f4e70; }

#ir-doc, #ir-doc * { color: var(--ir-dim); transition: color .15s; }
/* 記事が要素に直接書いた色（style="color:white" など）は上の規則より強いので、対象とその隣の
   外では !important で薄くする。対象の中では記事の色をそのまま出す。取り込みのプレビューは薄くしない */
#ir-doc:not(.ir-preview) :not(.ir-focus):not(.ir-focus *):not(.ir-near):not(.ir-near *) {
        color: var(--ir-dim) !important; }
#ir-doc .ir-focus, #ir-doc .ir-focus * { color: var(--ir-fg); }
/* 対象の真隣にある、カード生成の対象外の要素は薄くしない */
#ir-doc .ir-near, #ir-doc .ir-near * { color: var(--ir-fg); }

#ir-doc img, #ir-doc svg, #ir-doc canvas, #ir-doc video { opacity: .3; }
#ir-doc .ir-focus img, #ir-doc .ir-focus svg, #ir-doc .ir-focus canvas,
#ir-doc .ir-focus video,
#ir-doc .ir-near img, #ir-doc .ir-near svg, #ir-doc .ir-near canvas,
#ir-doc .ir-near video { opacity: 1; }

/* .cloze は Anki が描画した cloze（cloze カードと Enumeration）。data-cloze は問題面の span にしか付かないので、
   回答面にも色が付くように class で選ぶ */
#ir-doc mark, #ir-doc [data-cloze], #ir-doc .cloze { background: transparent; border-radius: 2px; }
#ir-doc .ir-focus mark { background: var(--ir-mark); }
#ir-doc .ir-focus [data-cloze], #ir-doc .ir-focus .cloze { background: var(--ir-cloze); }
/* 段落のカードの問題面では cloze のマークに色を付けない（JS が .ir-hide-cloze を付ける）。回答面では付ける */
#ir-doc.ir-hide-cloze .ir-focus [data-cloze] { background: transparent; }
/* cloze の語のほかの出現。マークと同じ色。JS が CSS Custom Highlight API で付け、文書には書かない */
#ir-doc ::highlight(ir-cloze) { background-color: var(--ir-cloze); }

.ir-warn { margin: 0 0 1em; padding: .5em .8em; border-radius: 4px;
        background: #fde2e1; color: #7a1c16; font-size: 14px; }
.night_mode .ir-warn { background: #5a2320; color: #f5d0cd; }

/* pre（整形済みの文）は、改行と空白をそのまま残して、長い行は画面の幅で折り返す（2026-10-13）。
   ブラウザの既定（white-space: pre）では折り返さず、カードの右へはみ出していた。
   両端揃えにすると折り返した行の空白が伸びるので、左揃えにする */
#ir-doc pre { white-space: pre-wrap; overflow-wrap: anywhere; text-align: left; }
/* PBIR → Source Text: 段落の HTML のソースを文字として出す。
   HTML のソースなので両端揃えにしない */
#ir-doc .ir-source { white-space: pre-wrap; font-family: Menlo, Consolas, monospace;
        font-size: 13px; line-height: 1.5; overflow-wrap: anywhere; text-align: left; }

/* IR2 Basic の問いと答え。段落と見分けられるように、少し大きな字で、行ごとに中央に寄せる。
   段落の要素の中に入るので、pre の段落でも空白の扱いは普通に戻す */
.ir-qa { font-size: 1.15em; text-align: center; white-space: normal; margin: .2em 0 .8em; }
.ir-qa hr { border: 0; border-top: 1px solid currentColor; opacity: .3; margin: .5em 0; }
/* 編集中の問いと答え。空でもキャレットを置けるように高さを持たせ、空のときは何の欄かを薄く出す */
.ir-qa [contenteditable] { min-height: 1.4em; }
.ir-qa [contenteditable]:empty::before { content: attr(data-placeholder); opacity: .4; }
/* True or False の回答面（と作るとき）の偽の文。字の大きさと中央寄せは真の文と同じで、色と画像は対象の外の段落と
   同じ薄さ（#ir-doc *、#ir-doc img など）。対象の中の規則（#ir-doc .ir-focus * など）より強くするため .ir-qa を重ねる */
#ir-doc .ir-qa .ir-qa-dim, #ir-doc .ir-qa .ir-qa-dim * { color: var(--ir-dim); }
#ir-doc .ir-qa .ir-qa-dim img, #ir-doc .ir-qa .ir-qa-dim svg, #ir-doc .ir-qa .ir-qa-dim canvas,
#ir-doc .ir-qa .ir-qa-dim video { opacity: .3; }
"""


def _doc_js() -> str:
    """カードのテンプレートに入れる JS（表示、編集、PBIR メニューの操作）。

    Python 側のメニューは window._ir2Cmd を呼ぶ。JS は状態（カードの種類と面、問いと答えを編集中か、Source Text か、
    前後の段落と結合できるか）を ir:state で Python に知らせる。ブロックの要素、中身に数える要素、Create の種類は Python の
    BLOCK_TAGS、_MEDIA_TAGS、DERIVED から渡す。貼り付けで落とす要素は UNSAFE_TAGS（sanitize）と title。
    """
    # 注意: この JS はカードテンプレートにそのまま入るので、Anki のテンプレート記法として
    # 解釈される。コメントの中でも二重の波括弧を書いてはいけない。書くとフィールドの中身に
    # 置き換わり、中身に改行があると // コメントから外れた行が JS の構文エラーになって、
    # スクリプト全体が動かない（2026-09-23 に起きた）。
    js = _DOC_JS
    for name, val in (("BLOCK_TAGS", sorted(BLOCK_TAGS)), ("MEDIA_TAGS", sorted(_MEDIA_TAGS)),
                      ("UNSAFE_TAGS", UNSAFE_TAGS), ("DERIVED_KINDS", list(DERIVED))):
        js = js.replace(f"__{name}__", json.dumps(val))
    return js


_DOC_JS = r"""
(function () {
  var root = document.getElementById('ir-root');
  if (!root || root.dataset.irInit) return;
  root.dataset.irInit = '1';
  var docid = root.dataset.doc, anchor = root.dataset.anchor, side = root.dataset.side;   // side は q（問題面）か a（回答面）
  var para = anchor.split('.')[0];
  // cloze カードかどうかは属性の有無で決める。Anki は Text に該当の cloze が無いと
  // cloze フィルタの結果を空で描画する。中身の真偽で決めると、そのとき段落カードとして扱い、
  // 文書のマークに色が付いただけの表示になり、編集までできてしまう。
  var isCloze = 'clozeHtml' in root.dataset;
  var clozeQ = isCloze ? root.dataset.clozeHtml : null;
  // Basic、True or False、Multiple Choice、Enumeration のカードかどうかも属性の有無で決める。テンプレートが data-qa に
  // {kind, fields} を書く（_qa_template）
  var isDerived = 'qa' in root.dataset;
  var spec = isDerived ? JSON.parse(root.dataset.qa) : null;
  // 段落のカードの問題面だけが編集でき、回答面は編集できない
  var editable = !isCloze && !isDerived && side === 'q';
  // 表示中の問いと答え（openQA の戻り値）。Basic、True or False、Multiple Choice、Enumeration のカードと、段落のカードで Create → Basic などを開いている間
  var derived = null;
  // ブロックの要素と中身に数える要素（core.py の BLOCK_TAGS と _MEDIA_TAGS）、Create の種類（core.py の DERIVED）
  var BLOCK_TAGS = __BLOCK_TAGS__, MEDIA = __MEDIA_TAGS__.join(', '), DERIVED_KINDS = __DERIVED_KINDS__;
  var BLOCK_SELECTOR = BLOCK_TAGS.join(', ');
  function isBlock(n) { return n.nodeType === 1 && BLOCK_TAGS.indexOf(n.nodeName.toLowerCase()) >= 0; }
  // 文字か、画像などの要素を持つか（core._has_content と同じ）
  function hasContent(n) { return /\S/.test(n.textContent) || !!(n.querySelector && n.querySelector(MEDIA)); }
  function unwrap(n) {
    while (n.firstChild) n.parentNode.insertBefore(n.firstChild, n);
    n.parentNode.removeChild(n);
  }

  // 文書を読み、記事を描画して編集の準備をする。カードを出したときと、Extract と Merge のあと
  // （sendStructural）に呼ぶ。Extract と Merge のあとに Anki の描画し直し（reviewer の
  // _redraw_current_card）を使わないのは、それが下の欄の時計を 0 に戻し、音声を流し直し、
  // 自動で答えへ進む待ち時間を始め直すため（2026-10-09）。
  render();
  function render() {
    return loadDoc().then(function (html) {
      // 読み込み中に次の描画（回答画面など）で差し替えられていたら何もしない。
      // 古い段落に window._ir2Cmd が向くのを防ぐ。
      if (!document.body.contains(root)) return;
      var doc = document.createElement('div');
      doc.id = 'ir-doc';
      doc.innerHTML = html;
      var target = doc.querySelector('[data-ir="' + para + '"]');
      if (!target) {                     // 文書と索引の食い違い。フォールバックのまま
        fallbackReason('paragraph ' + para + ' was not found in the document');
        return;
      }
      // 薄くする側を列挙しない。文書全体が既定で薄く、対象だけを戻す（CSS 参照）。
      target.classList.add('ir-focus');
      // 段落のカードの問題面では cloze のマークに色を付けない（CSS 参照）。ほかの出現の色も付けない（setupEdit）
      if (editable) doc.classList.add('ir-hide-cloze');
      // 対象の前後に続く、カードでないブロック（見出し、図など）は薄化を打ち消す。
      // 連続している限り辿り、次のカードの div に当たったところで止める。
      // 見出しや figure が対象の段落と一緒に読めるようにするため。
      ['previousElementSibling', 'nextElementSibling'].forEach(function (dir) {
        var el = target[dir];
        while (el && !el.hasAttribute('data-ir')) {
          el.classList.add('ir-near');
          el = el[dir];
        }
      });
      var warn = null;
      if (isCloze && clozeQ) {
        // cloze カード: 段落の中身を Anki が描画した cloze 表示に差し替える
        target.innerHTML = clozeQ;
        hideFallback();
      } else if (isCloze) {
        // Text に cloze が無い。段落はそのまま見せ、そのことを知らせる
        warn = warnBox('This cloze card has no cloze in its Text field. ' +
          'It is removed after you answer it, or remove it with Tools \u2192 Empty Cards.');
      } else if (isDerived) {
        // Basic、True or False、Multiple Choice、Enumeration のカード: 問題面は段落の中身を隠して問いだけを出す。回答面は問いと答えを段落の中身の前に出す
        if (side === 'q') target.innerHTML = '';
        derived = openQA(target, spec.kind, side, spec.fields);
        hideFallback();
      }
      root.innerHTML = '';
      if (warn) root.appendChild(warn);
      root.appendChild(doc);
      target.scrollIntoView({block: 'center'});
      setupEdit(target);
    }).catch(function (err) {
      // フォールバック表示のまま。黙って落ちると原因が分からないので、理由を画面に出す
      fallbackReason(String(err && err.message || err));
    });
  }

  // 文書を出せずにフィールドだけの表示（フォールバック）になったとき、その理由を上に出す
  // テンプレートの器（Anki が描画した cloze か、問いと答えのフィールド）。文書を出せたら隠す
  function fallbackBox() { return document.getElementById('ir-cloze-q') || document.getElementById('ir-qa-fb'); }
  function hideFallback() { var fb = fallbackBox(); if (fb) fb.style.display = 'none'; }
  function fallbackReason(text) {
    if (!document.body.contains(root)) return;
    var at = fallbackBox() || root;
    at.parentNode.insertBefore(warnBox('PBIR: the article could not be shown (' + text + ').'), at);
  }

  function warnBox(text) {
    var w = document.createElement('div');
    w.className = 'ir-warn';
    w.textContent = text;
    return w;
  }

  // 保存の直後の描画（回答面や同じ記事の次のカード）が編集の前の文書を受け取らないように、キャッシュを使わない。
  // Anki の media server は URL のパスだけでファイルを決める（26.09.2 の aqt/mediasrv.py の flask の route）ので、
  // 付けたクエリ文字列は読むファイルを変えない
  function loadDoc() {
    var url = '_ir2_' + docid + '.html';
    return fetch(url + '?t=' + Date.now(), {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + url);
      return r.text();
    });
  }

  // Source Text のときは、表示している文字（段落の HTML のソース）をそのまま送る。
  // Python は保存したあとの段落の HTML を返す（after に渡す）
  function save(target, after) {
    if (!sourceMode) marksFromStyles(target);
    pycmd('ir2:save:' + docid + ':' + para + ':' +
          encodeURIComponent(sourceMode ? target.textContent : target.innerHTML), after || null);
  }

  function setupEdit(target) {
    // reviewer は同じ webview をカード間で使い回すので、前のカードのリスナとコマンドを外す
    if (window._ir2SelHandler) document.removeEventListener('selectionchange', window._ir2SelHandler);
    window._ir2SelHandler = null;
    window._ir2Cmd = null;
    if (window.CSS && CSS.highlights) CSS.highlights.delete('ir-cloze');
    if (isCloze) {
      return;  // cloze カードの表示は Anki の cloze 描画を含むため編集させない
    }
    if (isDerived) {
      // Basic、True or False、Multiple Choice、Enumeration のカード: 段落の操作（cloze、線、Extract など）は無い。
      // 回答面で、Basic、True or False、Multiple Choice は PBIR → Create → Edit で問いと答えを編集し、
      // Enumeration は PBIR → Create → Recreate Enumeration で作り直す
      window._ir2Cmd = function (cmd, arg) {
        if (side !== 'a') return;
        if (cmd === 'edit' && spec.kind !== 'enum') { if (arg) startQA(target); else endQA(target); }
        else if (cmd === 'enum' && spec.kind === 'enum' && derived.side === 'a') recreateDerived(target);
      };
      pushState(target);
      return;
    }
    if (side === 'a') watchClozes(target);   // cloze の語の出現の色は回答面だけ
    // PBIR → Font のチェックを選択に合わせる
    window._ir2SelHandler = function () {
      if (document.body.contains(root)) pushFont(target);   // 次のカードに替わったあとは何もしない
    };
    document.addEventListener('selectionchange', window._ir2SelHandler);
    window._ir2Cmd = function (cmd, arg) {
      if (derived) return;   // Create → Basic などを開いている間。段落の操作はしない（作るのはカードを離れるとき）
      if (cmd === 'source') { setSource(target, !!arg); return; }
      // 以下は描画された段落に対する操作。PBIR → Create は回答面だけ、ほかは問題面だけ
      var create = cmd === 'cloze' || DERIVED_KINDS.indexOf(cmd) >= 0;
      var why = sourceMode ? ' is not available while Source Text is on'
              : create === editable ? ' is available only on the ' + (create ? 'back' : 'front') + ' side' : null;
      if (why) { notify(cmd.charAt(0).toUpperCase() + cmd.slice(1) + why); return; }
      trimSel(target);
      if (cmd === 'cloze') makeCloze(target);
      else if (create) openCreate(target, cmd, {});
      else if (cmd === 'highlight') toggleHighlight(target);
      else if (cmd === 'extract') extract(target);
      else if (cmd === 'merge') merge(target, arg);
      else if (FORMATS.indexOf(cmd) >= 0) format(target, cmd);
    };
    if (editable) {
      // キャレットを段落に置く（キーは文字の入力になる。枠は Chromium の既定のフォーカスの輪で、フォーカスのある間だけ出る）
      target.setAttribute('contenteditable', 'true');
      target.focus({preventScroll: true});
      // 段落からフォーカスが外れるたびに保存する（ページの中の別の所へ移ったときも、メニュー操作や別ウィンドウへの切り替えで
      // ページごとフォーカスを失ったときも）。編集は続く
      target.addEventListener('blur', function () {
        if (!structBusy && !sourceBusy) save(target);
      });
      // Anki 本体の js/webview.js（qt/aqt/data/web/js/webview.ts）は、ページ戻りを防ぐため
      // document の keydown で Backspace (keyCode 8) を preventDefault する。例外は
      // event.target が INPUT / TEXTAREA / DIV のときだけ。アドオンの editing host は div なので
      // 例外に入るが、host で伝播を止めて、document のリスナまで届かせない。
      target.addEventListener('keydown', function (ev) {
        if (target.isContentEditable && ev.keyCode === 8) ev.stopPropagation();
      });
      target.addEventListener('paste', onPaste);
    }
    // Source Text はカードをまたいで続く（Python が覚えている）。入っていればこのカードでも切り替える
    pushState(target, function (r) { if (r && r.source) setSource(target, true, true); });
  }

  // 貼り付け（段落と、問いと答えの欄）。Chromium はコピー元の計算済みの見た目（色、フォントなど）を style 属性として
  // クリップボードの HTML に書き込み、貼り付け先と違う分を残す。薄くしてある段落から
  // コピーすると薄い色が残り、その部分は表示中の段落でも薄いままになる。
  // 貼り付けの HTML から style と、この文書の印（data-ir, data-cloze）を外してから入れる。
  // insertHTML は Anki の外の編集と同じく取り消し（Cmd+Z）の対象になる。
  function onPaste(ev) {
    if (!ev.target.isContentEditable || !ev.clipboardData || sourceMode) return;  // Source Text は文字のまま
    var html = ev.clipboardData.getData('text/html');
    if (!html) return;  // 平文だけのときはブラウザに任せる
    ev.preventDefault();
    document.execCommand('insertHTML', false, cleanPasted(html));
  }

  var PASTE_DROP_ATTRS = ['style', 'class', 'id', 'data-ir',
                          'data-cloze', 'contenteditable'];
  function cleanPasted(html) {
    var tpl = document.createElement('template');
    tpl.innerHTML = html;
    var junk = tpl.content.querySelectorAll(__UNSAFE_TAGS__.concat('title').join(', '));
    for (var i = 0; i < junk.length; i++) junk[i].parentNode.removeChild(junk[i]);
    var els = tpl.content.querySelectorAll('*');
    for (var j = 0; j < els.length; j++) {
      PASTE_DROP_ATTRS.forEach(function (a) { els[j].removeAttribute(a); });
      // スクリプトを動かしうる属性（Python の sanitize と同じ規則。保存時にも外す）
      for (var k = els[j].attributes.length - 1; k >= 0; k--) {
        var at = els[j].attributes[k];
        if (/^on/i.test(at.name) ||
            /(javascript|vbscript):/i.test(at.value.replace(/[\x00-\x20]/g, '')))
          els[j].removeAttribute(at.name);
      }
    }
    // 貼り付ける先はふつうカードの中の p で、p の中にブロックは入れられない。そのまま渡すと Chromium が
    // span に置き換えて色を書き込むので、ブロックは外して間に改行を入れる
    var blocks = tpl.content.querySelectorAll(BLOCK_SELECTOR);
    for (var b = blocks.length - 1; b >= 0; b--) {
      var bl = blocks[b];
      if (bl.nextSibling && /\S/.test(bl.textContent))
        bl.parentNode.insertBefore(document.createElement('br'), bl.nextSibling);
      unwrap(bl);
    }
    // 属性の無くなった span は意味を持たないので中身だけにする
    var spans = tpl.content.querySelectorAll('span');
    for (var k = 0; k < spans.length; k++) if (!spans[k].attributes.length) unwrap(spans[k]);
    return tpl.innerHTML;
  }

  var FORMATS = ['bold', 'italic', 'subscript', 'superscript'];

  // Python 側のメニュー（チェックと灰色表示）のための状態通知
  function pushState(target, after) {
    // card はカードの種類と面（段落のカードは para:q と para:a、Basic などのカードは basic:a や enum:q など）。
    // derived は、問いと答えを出していること（Basic、True or False、Multiple Choice、Enumeration のカードか、Create → Basic などを開いている間）。
    // editing は、問いと答えの編集中のこと（Create → Edit のチェックと、Create → Basic などと Recreate Enumeration で開いている間）
    pycmd('ir2:state:' + docid + ':' + para + ':' + JSON.stringify({
      card: (isDerived ? spec.kind : 'para') + ':' + side,
      editing: !!(derived && derived.editing),
      derived: !!derived,
      source: sourceMode,
      prev: !!adjacentPara(target, 'prev'),
      next: !!adjacentPara(target, 'next')
    }), after || null);
    pushFont(target);
  }

  // PBIR → Font のチェックのための状態通知。普通のエディタと同じく、選択範囲（キャレットだけのときはその位置）に
  // 付いている装飾にチェックを付ける。判定は付け外しと同じ基準にする。
  //   Bold など: queryCommandState。Chromium（main の style_commands.cc）では、状態の StateStyle と
  //   付け外しの ExecuteToggleStyle が同じ判定（Mac は選択の始まり、ほかは選択全体に付いているか）を使う。
  //   キャレットだけで Bold を押したあとの「次に打つ文字は太字」も含む。
  //   Highlight: toggleHighlight と同じく、選択範囲の文字が全部 <mark> の中か。キャレットだけなら <mark> の中か。
  //   Create → Cloze（回答面だけ）: makeCloze が cloze を外すときと同じく、選択範囲の文字が全部 cloze の箇所にあるか。
  //   回答面の段落は編集できずキャレットが見えないので、キャレットだけのときは付けない。
  // 段落の中に選択が無いとき（段落の外のキャレットや選択、Source Text の間、問いと答えを出している間）は、どれにもチェックを付けない。
  // 前に送ったものと同じときは送らない（selectionchange のたびに呼ぶため）。
  var lastFont = null;
  function pushFont(target) {
    var st = {highlight: false, bold: false, italic: false, subscript: false, superscript: false, cloze: false};
    var sel = window.getSelection();
    var range = !sourceMode && !derived && sel.rangeCount && cardRange(sel.getRangeAt(0), target);
    if (range) {
      st.highlight = sel.isCollapsed ? inMark(range.startContainer, target) : fullyMarked(range, target);
      for (var i = 0; i < FORMATS.length; i++)
        st[FORMATS[i]] = document.queryCommandState(FORMATS[i]);
      st.cloze = side === 'a' && !sel.isCollapsed && !!clozesInSelection(range, target);
    }
    var s = JSON.stringify(st);
    if (s === lastFont) return;
    lastFont = s;
    pycmd('ir2:font:' + docid + ':' + para + ':' + s);
  }

  // PBIR → Source Text。段落の HTML のソースを文字として表示する（文書に書かれているとおりの HTML を
  // Python から受け取る）。問題面では、そのまま文字として編集できる（contenteditable="plaintext-only"。
  // 打った文字も貼り付けも文字のまま入る）。切り替えるときは、問題面では今の中身を保存してから、
  // 保存された HTML（Python が整えたもの）で表示し直す。saved はカードを出したとき（中身は文書のまま）
  var sourceMode = false, sourceBusy = false;
  function setSource(target, on, saved) {
    if (on === sourceMode || sourceBusy) { pushState(target); return; }
    var done = function (stored) {
      sourceBusy = false;
      if (typeof stored !== 'string') { notify('Source Text failed'); pushState(target); return; }
      sourceMode = on;
      target.classList.toggle('ir-source', on);
      if (on) target.textContent = stored;
      else target.innerHTML = stored;
      if (editable) {
        target.setAttribute('contenteditable', on ? 'plaintext-only' : 'true');
        if (!saved) target.focus();
      }
      pushState(target);
    };
    sourceBusy = true;
    if (editable && !saved) save(target, done);
    else pycmd('ir2:source:' + docid + ':' + para + ':', done);
  }

  // 直前か直後の兄弟がカードの div ならそれ。結合できなければ null（core._adjacent_para と同じ。間は空白とコメントだけ。
  // メニューの灰色表示に使う。最終の判定は Python）
  function adjacentPara(target, dir) {
    var step = dir === 'prev' ? 'previousSibling' : 'nextSibling', n = target[step];
    while (n && (n.nodeType === 8 || (n.nodeType === 3 && !/\S/.test(n.data)))) n = n[step];
    return n && n.nodeType === 1 && n.hasAttribute('data-ir') ? n : null;
  }

  // PBIR → Extract。問題面だけ。選択の始まりと終わりに印の span を入れて中身を読み、印は外す。
  // Python が、選んでいない前と後ろを新しい段落にする（core.extract_paragraph）。
  function extract(target) {
    var bad = selectionProblem(target);
    if (bad) { notify('Extract failed: ' + bad); return; }
    marksFromStyles(target);
    var range = window.getSelection().getRangeAt(0);
    // 終わりから入れる。終わりに入れても始まりの位置は動かない
    var marks = ['end', 'start'].map(function (pos) {
      var m = document.createElement('span'), at = range.cloneRange();
      m.setAttribute('data-ir-split', pos);
      at.collapse(pos === 'start');
      at.insertNode(m);
      return m;
    });
    var html = target.innerHTML;
    marks.forEach(function (m) { m.parentNode.removeChild(m); });
    target.normalize();
    sendStructural(target, 'extract', '', html);
  }

  // PBIR → Merge Previous / Merge Next。今の中身も送り、先に保存させる。
  function merge(target, dir) {
    if (!adjacentPara(target, dir)) {
      notify('Merge failed: there is no paragraph ' + (dir === 'prev' ? 'right before' : 'right after') +
             ' this one with nothing in between');
      return;
    }
    marksFromStyles(target);
    sendStructural(target, 'merge', dir, target.innerHTML);
  }

  // 送る前に編集できなくする。そうしないと、描画し直すときの blur で Extract・結合の前の中身が
  // 保存され、結果を上書きする。成功したら文書を読み直して描画し直す（render）。断られたら編集できる状態に戻す。
  var structBusy = false;
  function sendStructural(target, cmd, arg, html) {
    structBusy = true;
    target.removeAttribute('contenteditable');
    pycmd('ir2:' + cmd + ':' + docid + ':' + para + ':' + (arg ? arg + ':' : '') +
          encodeURIComponent(html), function (res) {
      structBusy = false;
      if (res === 'ok') { render(); return; }
      if (editable) { target.setAttribute('contenteditable', 'true'); target.focus(); }
      pushState(target);
    });
  }

  // 段落の操作（_ir2Cmd）は今の選択に対して働く。選択が段落の外にはみ出していて、その部分に文字が無ければ、段落の中だけの
  // 選択にする（cardRange）。メニューを開いても選択とフォーカスはページに残る（2026-10-22、Anki 26.09.2 で確かめた）
  function trimSel(target) {
    var sel = window.getSelection(), r = sel.rangeCount && cardRange(sel.getRangeAt(0), target);
    if (r) { sel.removeAllRanges(); sel.addRange(r); }
  }

  // 選択のうち段落の外にはみ出した部分に文字が無ければ（空白だけで、画像なども無ければ）、段落の中だけの範囲を返す。
  // 段落の最後の行を 3 回クリックしたときや、最後の行の先まで引いて選んだとき、選択の終わりは次の段落の先頭に来る。
  // はみ出した部分に文字があれば null。段落の中にある選択はそのまま返す。
  function cardRange(range, target) {
    if (target.contains(range.commonAncestorContainer)) return range;
    function empty(a) { return !hasContent(a.cloneContents()); }
    var r = range.cloneRange(), outer = document.createRange(), a;
    outer.selectNodeContents(target);
    if (r.compareBoundaryPoints(Range.START_TO_START, outer) < 0) {
      a = document.createRange(); a.setStart(r.startContainer, r.startOffset); a.setEnd(target, 0);
      if (!empty(a)) return null;
      r.setStart(target, 0);
    }
    if (r.compareBoundaryPoints(Range.END_TO_END, outer) > 0) {
      a = document.createRange(); a.setStart(target, target.childNodes.length); a.setEnd(r.endContainer, r.endOffset);
      if (!empty(a)) return null;
      r.setEnd(target, target.childNodes.length);
    }
    return r.collapsed ? null : r;
  }

  // cloze の span が分割・複製されたり中身が動いたりしていないかを見るための指紋
  function clozeSig(target) {
    var cs = target.querySelectorAll('[data-cloze]'), a = [];
    for (var i = 0; i < cs.length; i++)
      a.push(cs[i].getAttribute('data-cloze') + '\u0000' + cs[i].textContent);
    return a.sort().join('\u0001');
  }

  // 数式（<math>）の中に要素が入ったり、数式が割れたりしていないかを見るための指紋
  function mathSig(target) {
    var ms = target.querySelectorAll('math'), a = [];
    for (var i = 0; i < ms.length; i++) a.push(ms[i].outerHTML);
    return a.join('\u0001');
  }

  // Bold, Italic, Subscript, Superscript。付け外しの判定はブラウザの execCommand に任せる
  // （普通のエディタと同じく、全部に付いていれば外し、そうでなければ付ける）。
  // 装飾を外すときにブラウザが祖先の要素を分割することがあり、cloze の span が
  // 二つに割れると id が重複する。そうなったら元に戻して断る。
  // 数式（<math>）もブラウザの装飾で壊れる。数式を丸ごと選んでも、ブラウザは <b> などを数式の中に入れる
  // （Bold で分数の 2 つの項が <b> に包まれた。2026-09-26、QtWebEngine 6.11.2 で確認）。
  // そこで数式の形が変わったら、cloze と同じく元に戻して断る。
  function format(target, cmd) {
    var before = target.innerHTML, sig = clozeSig(target), msig = mathSig(target);
    document.execCommand(cmd, false, null);
    var why = clozeSig(target) !== sig ? 'it would split an existing cloze'
            : mathSig(target) !== msig ? 'it would put formatting inside a formula (select only text outside formulas)'
            : null;
    if (why) {
      target.innerHTML = before;
      notify('Format failed: ' + why);
    }
    pushFont(target);
  }

  // 編集中に Chromium が <mark> を、線の色の背景を書いた span に書き換えることがある（線の行の先頭で Backspace を
  // 押して前の行とつないだときなど。2026-10-19、アプリ内の Chromium で確認）。画面では線に見えるが <mark> ではないので
  // Highlight の判定に入らず、保存の後始末（core.clean_styles）がアドオンの色として背景を外すので、文書では線が消える。
  // そこで保存と、線、cloze、Extract、結合の前に、背景が線の色の span を <mark> に戻す。span のほかの style は残す。
  // 選択（キャレット）の位置は保つ。
  function rgbKey(v) {
    v = (v || '').trim().toLowerCase();
    var m = /^#([0-9a-f]{6})$/.exec(v);
    if (m) return [0, 2, 4].map(function (i) { return parseInt(m[1].substr(i, 2), 16); }).join(',');
    m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(v);
    if (m && (m[4] === undefined || parseFloat(m[4]) === 1)) return m[1] + ',' + m[2] + ',' + m[3];
    return null;
  }
  function marksFromStyles(target) {
    var doc = target.closest('#ir-doc');
    var color = doc && rgbKey(getComputedStyle(doc).getPropertyValue('--ir-mark'));
    if (!color) return;
    var spans = Array.prototype.filter.call(target.querySelectorAll('span[style]'), function (s) {
      return rgbKey(s.style.backgroundColor) === color;
    });
    if (!spans.length) return;
    var sel = window.getSelection(), at = null;
    if (sel.rangeCount) {
      var r = sel.getRangeAt(0);
      at = [r.startContainer, r.startOffset, r.endContainer, r.endOffset];
    }
    spans.forEach(function (s) {
      var m = document.createElement('mark');
      s.parentNode.insertBefore(m, s);
      m.appendChild(s);
      s.style.removeProperty('background-color');
      if (!s.style.length) s.removeAttribute('style');
      if (!s.attributes.length) {   // 中身を mark へ移す。選択の端が span にあれば mark に付け替える（位置は同じ）
        unwrap(s);
        if (at && at[0] === s) at[0] = m;
        if (at && at[2] === s) at[2] = m;
      }
    });
    // 線の中の線は外す（線を引くときと同じ）
    var inner = target.querySelectorAll('mark mark');
    for (var i = 0; i < inner.length; i++) {
      var n = inner[i], p = n.parentNode, k = Array.prototype.indexOf.call(p.childNodes, n), len = n.childNodes.length;
      // 選択の端の位置を、中身を外へ出したあとの位置に直す
      for (var e = 0; at && e < 4; e += 2) {
        if (at[e] === p && at[e + 1] > k) at[e + 1] += len - 1;
        else if (at[e] === n) { at[e] = p; at[e + 1] += k; }
      }
      unwrap(n);
    }
    if (at && document.contains(at[0]) && document.contains(at[2])) {
      var nr = document.createRange();
      nr.setStart(at[0], at[1]); nr.setEnd(at[2], at[3]);
      sel.removeAllRanges(); sel.addRange(nr);
    }
  }

  // 選択の境界が <mark> の途中にあると、extractContents が中身の無い <mark> を残す
  function removeEmptyMarks(target) {
    var ms = target.querySelectorAll('mark');
    for (var i = 0; i < ms.length; i++)
      if (!ms[i].textContent && !ms[i].querySelector(MEDIA + ', br')) ms[i].parentNode.removeChild(ms[i]);
  }

  // node が <mark> か、その中にあるか（段落の中だけを見る）
  function inMark(node, target) {
    for (var n = node; n && n !== target; n = n.parentNode)
      if (n.nodeName === 'MARK') return true;
    return false;
  }

  // 選択範囲の中の文字（空白だけの部分は除く）がすべて <mark> の中にあるか
  function fullyMarked(range, target) {
    var w = document.createTreeWalker(target, NodeFilter.SHOW_TEXT), n, any = false;
    while ((n = w.nextNode())) {
      if (!range.intersectsNode(n)) continue;
      var s = (n === range.startContainer) ? range.startOffset : 0;
      var e = (n === range.endContainer) ? range.endOffset : n.length;
      if (!/\S/.test(n.data.slice(s, e))) continue;
      any = true;
      if (!inMark(n, target)) return false;
    }
    return any;
  }

  // 選択範囲から <mark> を外す。選択の外にはみ出している部分は <mark> のまま残す。
  // 境界に目印の要素を差し込んでから作業し、最後に目印の間を選択し直す。
  function unmarkSelection(range, target) {
    var S = document.createElement('span'), E = document.createElement('span');
    var er = range.cloneRange(); er.collapse(false); er.insertNode(E);
    var sr = range.cloneRange(); sr.collapse(true); sr.insertNode(S);
    var r = document.createRange();
    r.setStartAfter(S); r.setEndBefore(E);
    var marks = Array.prototype.filter.call(target.querySelectorAll('mark'),
      function (m) { return r.intersectsNode(m); });
    // m の中の、選択の外の部分（範囲 r）を新しい <mark> に移して、at の前に置く
    function keepOutside(m, r, at) {
      var k = document.createElement('mark');
      k.appendChild(r.extractContents());
      if (k.textContent) m.parentNode.insertBefore(k, at);
    }
    marks.forEach(function (m) {
      if (!m.parentNode) return;
      var r;
      if (m.contains(S)) {
        r = document.createRange(); r.setStart(m, 0); r.setEndBefore(S);
        keepOutside(m, r, m);
      }
      if (m.contains(E)) {
        r = document.createRange(); r.setStartAfter(E); r.setEnd(m, m.childNodes.length);
        keepOutside(m, r, m.nextSibling);
      }
      unwrap(m);
    });
    var nr = document.createRange();
    nr.setStartAfter(S); nr.setEndBefore(E);
    S.parentNode.removeChild(S); E.parentNode.removeChild(E);
    removeEmptyMarks(target);
    target.normalize();
    var sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(nr);
  }

  // PBIR → Font → Highlight。選択範囲が全部 <mark> なら外し、そうでなければ付ける。
  // 選択は残す（続けて押せば元に戻る）。
  function toggleHighlight(target) {
    var sel = window.getSelection();
    // 線を外すとき（unmarkSelection）も、端に目印の要素を入れるので、数式の中の端は断る
    var bad = selectionProblem(target) || mathConflict(sel.getRangeAt(0));
    if (bad) { notify('Highlight failed: ' + bad); return; }
    marksFromStyles(target);
    var range = sel.getRangeAt(0), before = target.innerHTML, sig = clozeSig(target), r = 'ok';
    if (fullyMarked(range, target)) {
      try { unmarkSelection(range, target); }
      catch (err) { r = 'could not remove the highlight (' + err.name + ')'; }
    } else {
      r = wrapSelection(target, 'mark');
    }
    if (r === 'ok' && clozeSig(target) !== sig) {
      target.innerHTML = before;
      r = 'it would split an existing cloze';
    }
    if (r === 'ok') save(target);
    else notify('Highlight failed: ' + r);
    pushFont(target);
  }

  // 選択範囲と要素の位置関係を 4 つに分ける
  //   disjoint  … 重ならない
  //   contains  … 選択が要素を丸ごと含む
  //   inside    … 選択が要素の中に収まっている
  //   crossing  … 境界をまたぐ。包むと要素が分割される
  function relateToRange(range, node) {
    if (!range.intersectsNode(node)) return 'disjoint';
    var outer = document.createRange();
    outer.selectNode(node);
    if (range.compareBoundaryPoints(Range.START_TO_START, outer) <= 0 &&
        range.compareBoundaryPoints(Range.END_TO_END, outer) >= 0) return 'contains';
    var inner = document.createRange();
    inner.selectNodeContents(node);
    if (range.compareBoundaryPoints(Range.START_TO_START, inner) >= 0 &&
        range.compareBoundaryPoints(Range.END_TO_END, inner) <= 0) return 'inside';
    return 'crossing';
  }

  // op: 'mark' か 'cloze'。断る理由を返す。問題なければ null。
  //   どちらも crossing は断る。cloze の span を分割すると同じ id の span が
  //   二つできて、どちらが本体か決まらなくなるため。
  //   cloze はさらに contains と inside も断る。入れ子にすると
  //   「cloze 1 個につきノート 1 件、序数は常に c1」という前提が崩れるため。
  function clozeConflict(range, target, op) {
    var clozes = target.querySelectorAll('[data-cloze]');
    for (var i = 0; i < clozes.length; i++) {
      var rel = relateToRange(range, clozes[i]);
      if (rel === 'disjoint') continue;
      if (rel === 'crossing') return 'selection crosses the edge of an existing cloze';
      if (op === 'cloze') return 'clozes cannot be nested';
    }
    return null;
  }

  // 選択の端が数式（<math>）の中にあれば断る理由を返す。問題なければ null。
  // 端が数式の中にあると、線や cloze の要素（HTML）が MathML の中に入るか、数式が二つに割れて、
  // 数式が壊れる（2026-09-26、QtWebEngine 6.11.2 で確認）。両端が数式の外なら、選択にかかる数式は
  // 丸ごと含まれるので壊れない。
  function mathConflict(range) {
    var ends = [range.startContainer, range.endContainer];
    for (var i = 0; i < ends.length; i++) {
      var el = ends[i].nodeType === 1 ? ends[i] : ends[i].parentNode;
      if (el && el.closest && el.closest('math'))
        return 'the selection starts or ends inside a formula (select the whole formula, or only text outside it)';
    }
    return null;
  }

  function notify(text) {
    pycmd('ir2:note:' + docid + ':' + para + ':' + encodeURIComponent(text));
  }

  function hasBlock(node) {
    return node.nodeType === 1 && (isBlock(node) || !!node.querySelector(BLOCK_SELECTOR));
  }
  // 選択範囲を、ブロック（p、li など）の中の、ブロックを含まない部分ごとの範囲に分ける（文書の順）。
  // 印の要素がブロックを含まないように、線はこの範囲ごとに別の <mark> で包む。
  // 選択に文字の無い範囲（空白だけで、画像なども無いもの）は入れない。そのため、選択の終わりが次のブロックの先頭にある
  // とき（行を 3 回クリックしたときなど）は、そのブロックは選択に入らない。
  function blockParts(range, target) {
    var parts = [];
    function add(first, last) {
      var r = document.createRange();
      r.setStartBefore(first); r.setEndAfter(last);
      if (range.compareBoundaryPoints(Range.START_TO_START, r) > 0) r.setStart(range.startContainer, range.startOffset);
      if (range.compareBoundaryPoints(Range.END_TO_END, r) < 0) r.setEnd(range.endContainer, range.endOffset);
      if (hasContent(r.cloneContents())) parts.push(r);
    }
    (function walk(node) {
      var first = null, last = null;
      for (var n = node.firstChild; n; n = n.nextSibling) {
        if (!range.intersectsNode(n)) continue;
        if (hasBlock(n)) {
          if (first) add(first, last);
          first = last = null;
          walk(n);
        } else {
          first = first || n;
          last = n;
        }
      }
      if (first) add(first, last);
    })(target);
    return parts;
  }
  // 線と cloze で選択を使えなければ、その理由。使えれば null
  function selectionProblem(target) {
    var sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return 'nothing selected';
    if (!target.contains(sel.getRangeAt(0).commonAncestorContainer)) return 'selection is outside the current paragraph';
    return null;
  }
  // 選択範囲を線（op が mark。<mark>）か cloze の印（cloze。data-cloze が PENDING の span）で包む。戻り値は 'ok' か失敗の理由。
  // surroundContents は要素の境界をまたぐ範囲に InvalidStateError を投げるので使わない。extractContents + insertNode なら
  // またいだ要素を分割して包める。線は、中の線を外し、包んだ範囲を選んだままにする（続けて押せば元に戻る）
  function wrapSelection(target, op) {
    var bad = selectionProblem(target);
    if (bad) return bad;
    var sel = window.getSelection(), range = sel.getRangeAt(0);
    var parts = blockParts(range, target);
    if (!parts.length) return 'nothing selected';
    // cloze の印は 1 個なので、二つ以上のブロック（p、li など）にまたがる選択は断る。線はブロックごとに包む
    if (parts.length > 1 && op === 'cloze')
      return 'selection spans more than one paragraph or list item';
    var conflict = mathConflict(range) || clozeConflict(range, target, op);
    if (conflict) return conflict;
    var before = target.innerHTML, els = [];
    try {
      // 後ろの範囲から包む。後ろの範囲を包んでも、前の範囲の境界は動かない
      for (var i = parts.length - 1; i >= 0; i--) {
        var el = document.createElement(op === 'mark' ? 'mark' : 'span');
        if (op === 'cloze') el.setAttribute('data-cloze', 'PENDING');
        el.appendChild(parts[i].extractContents());
        parts[i].insertNode(el);
        els.unshift(el);
      }
    } catch (err) {
      target.innerHTML = before;
      return 'could not wrap the selection (' + err.name + ')';
    }
    if (op === 'mark') {
      els.forEach(function (e) { Array.prototype.forEach.call(e.querySelectorAll('mark'), unwrap); });
      removeEmptyMarks(target);
    }
    target.normalize();
    sel.removeAllRanges();
    if (op === 'mark') {
      var nr = document.createRange();
      nr.setStart(els[0], 0);
      nr.setEnd(els[els.length - 1], els[els.length - 1].childNodes.length);
      sel.addRange(nr);
    }
    return 'ok';
  }

  // cloze のマークは選んだ箇所に 1 個だけで、CSS はマークにだけ色を付ける。段落の中のほかの出現にも
  // 同じ色を付ける。文書には書かない（CSS Custom Highlight API。DOM を変えないので、保存、編集、
  // cloze の指紋に影響しない）。段落が変わるたび（編集、線、cloze の id の受け取り、Source Text）
  // に数え直す。数え方は Python の cloze_text と同じ:
  // 大文字と小文字を区別しない、語の中の空白は種類と数を問わない、語の途中は数えない（かなと漢字は数える）、
  // ほかの data-cloze のマークの境目はまたいでよい、それ以外の要素で割れたものと数式の中は数えない。
  var UNSPACED = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\u{20000}-\u{3ffff}]/u;
  function joins(a, b) {
    return [a, b].every(function (c) { return /^[\p{L}\p{N}]$/u.test(c) && !UNSPACED.test(c); });
  }
  function charBefore(s, i) {   // i の直前の 1 文字（サロゲートペアは 2 単位で 1 文字）
    var c = s.charCodeAt(i - 1);
    return (c >= 0xdc00 && c <= 0xdfff && i >= 2) ? s.slice(i - 2, i) : s.charAt(i - 1);
  }
  function charAt(s, i) { return String.fromCodePoint(s.codePointAt(i)); }
  // 続けて読むテキストノードの並び。Python はほかのマークを外してから隣り合う文字列をつなぐので、
  // ほかの data-cloze の span の境目では切らない。そのマーク自身、数式、ほかの要素、コメントでは切る。
  function textRuns(root, own) {
    var runs = [], cur = [];
    function flush() { if (cur.length) runs.push(cur); cur = []; }
    (function walk(node) {
      for (var n = node.firstChild; n; n = n.nextSibling) {
        if (n.nodeType === 3) cur.push(n);
        else if (n.nodeType !== 1 || n === own || n.nodeName.toLowerCase() === 'math' ||
                 n.classList.contains('ir-qa')) flush();   // 問いと答え（Create → Basic など）は段落の文ではない
        else if (n.hasAttribute('data-cloze')) walk(n);
        else { flush(); walk(n); flush(); }
      }
    })(root);
    flush();
    return runs;
  }
  function rangeIn(run, a, b) {
    var r = document.createRange(), pos = 0, started = false;
    for (var i = 0; i < run.length; i++) {
      var t = run[i], end = pos + t.data.length;
      if (!started && a < end) { r.setStart(t, a - pos); started = true; }
      if (started && b <= end) { r.setEnd(t, b - pos); break; }
      pos = end;
    }
    return r;
  }
  // cloze のマークごとの、語のほかの出現の範囲。[{mark, word, ranges}]。word は比べるための語（空白をまとめ、小文字にしたもの）
  function clozeHits(target) {
    var out = [], marks = target.querySelectorAll('[data-cloze]');
    for (var i = 0; i < marks.length; i++) {
      var own = marks[i], ranges = [];
      var words = own.textContent.split(/\s+/).filter(Boolean);
      if (!words.length) continue;
      var re = new RegExp(words.map(function (w) {
        return w.replace(/[.*+?^$()|[\]\\\/{}]/g, '\\$&');
      }).join('\\s+'), 'giu');
      textRuns(target, own).forEach(function (run) {
        var s = run.map(function (t) { return t.data; }).join(''), m;
        re.lastIndex = 0;
        while ((m = re.exec(s))) {
          var a = m.index, b = a + m[0].length;
          if ((a > 0 && joins(charBefore(s, a), charAt(s, a))) ||
              (b < s.length && joins(charBefore(s, b), charAt(s, b)))) continue;
          ranges.push(rangeIn(run, a, b));
        }
      });
      out.push({mark: own, word: words.join(' ').toLowerCase(), ranges: ranges});
    }
    return out;
  }
  // 選択範囲の中の文字（空白だけの部分は除く）がすべて cloze の箇所（マークか、その語のほかの出現）にあれば、
  // それらの箇所の cloze のマークと、同じ語のほかの cloze のマーク。そうでなければ null。判定は fullyMarked と同じ形
  function clozesInSelection(range, target) {
    var hits = clozeHits(target), found = [], any = false;
    var w = document.createTreeWalker(target, NodeFilter.SHOW_TEXT), n;
    function at(n, i) {
      var inAny = false;
      hits.forEach(function (h) {
        if (h.mark.contains(n) || h.ranges.some(function (r) { return r.isPointInRange(n, i) && r.isPointInRange(n, i + 1); })) {
          inAny = true;
          if (found.indexOf(h) < 0) found.push(h);
        }
      });
      return inAny;
    }
    while ((n = w.nextNode())) {
      if (!range.intersectsNode(n)) continue;
      var s = (n === range.startContainer) ? range.startOffset : 0;
      var e = (n === range.endContainer) ? range.endOffset : n.length;
      for (var i = s; i < e; i++) {
        if (!/\S/.test(n.data.charAt(i))) continue;
        any = true;
        if (!at(n, i)) return null;
      }
    }
    if (!any) return null;
    var words = found.map(function (h) { return h.word; });
    return hits.filter(function (h) { return words.indexOf(h.word) >= 0; }).map(function (h) { return h.mark; });
  }
  function showClozes(target) {
    if (!window.CSS || !CSS.highlights || typeof Highlight === 'undefined') return;
    if (!document.body.contains(target)) return;   // 次のカードに替わっている
    var h = new Highlight();
    clozeHits(target).forEach(function (hit) { hit.ranges.forEach(function (r) { h.add(r); }); });
    CSS.highlights.set('ir-cloze', h);
  }
  function watchClozes(target) {
    var queued = false;
    new MutationObserver(function () {
      if (queued) return;
      queued = true;
      setTimeout(function () { queued = false; showClozes(target); }, 0);
    }).observe(target, {childList: true, characterData: true, subtree: true,
                        attributes: true, attributeFilter: ['data-cloze']});
    showClozes(target);
  }

  // PBIR → Create → Cloze。選択範囲の文字がすべて cloze の箇所なら、その cloze を外す（removeClozes）。
  // そうでなければ、選択範囲を data-cloze の span で包む（入れ子とまたぎは断る）
  function makeCloze(target) {
    var sel = window.getSelection();
    var marks = !selectionProblem(target) && clozesInSelection(sel.getRangeAt(0), target);
    if (marks) { removeClozes(target, marks); return; }
    marksFromStyles(target);
    var r = wrapSelection(target, 'cloze');
    if (r !== 'ok') { notify('Cloze failed: ' + r); return; }
    // Python が採番した id を受け取って DOM 側の PENDING も置き換える。
    // 置き換えないと、この後の保存（線を引くなど）で PENDING が文書に戻る。
    pycmd('ir2:cloze:' + docid + ':' + para + ':' +
          encodeURIComponent(target.innerHTML), function (id) {
      var p = target.querySelector('[data-cloze="PENDING"]');
      if (!p) return;
      if (id) { p.setAttribute('data-cloze', id); return; }
      unwrap(p);  // 失敗時は外す
      target.normalize();
    });
  }

  // cloze のマークを外して（中身の文字は残す）段落を保存する。外した cloze のノートは、Text を作り直すと cloze が無くなり、
  // 評価のあとの後始末で消える（core.remove_empty）
  function removeClozes(target, marks) {
    var ids = marks.map(function (m) { return m.getAttribute('data-cloze'); });
    marks.forEach(unwrap);
    target.normalize();
    save(target, function () {
      notify('Cloze removed (' + ids.join(', ') + '). ' + (ids.length === 1 ? 'Its card is' : 'Their cards are') +
             ' deleted after you answer this card');
    });
    pushFont(target);
  }

  // ---- Basic、True or False、Multiple Choice、Enumeration の問いと答え

  // 上から順の行を返す。行は {head: 文字}（決まった文字）、{hr: true}（線）、
  // {field: フィールド名, dim, placeholder}（フィールド。編集中は書ける）、{html, dim}（フィールドから作った表示だけの行）。
  // qa.side は q（問題面）、a（回答面）、new（Create で作るとき）。
  // True or False は、問題面に True と False のどちらかの文（出すたびに選ぶ。qaPick）を出し、回答面にその文の答え（True か
  // False）と両方の文を出す。作るときは回答面と同じ並びで、答えの代わりに問いの文字を出す。
  // Multiple Choice は、編集する間（回答面と作るとき）は選択肢を全部 1 個の欄（Choices）で出し、ほかは問いと選択肢 2 個
  // （mcPick）を出す。
  function qaRows(qa, editing) {
    if (qa.kind === 'tf') {
      if (qa.side === 'new') return [{head: 'True or False?'}, {field: 'True'}, {field: 'False', dim: true}];
      var t = qaPick(qa).t;
      if (qa.side === 'q') return [{head: 'True or False?'}, {field: t ? 'True' : 'False'}];
      return [{head: t ? 'True' : 'False'}, {field: 'True'}, {field: 'False', dim: true}];
    }
    if (qa.kind === 'mc') {
      if (editing)
        return [{field: 'Question'},
                {field: 'Choices', placeholder: 'Right choice on the first line, wrong choices on the following lines'}];
      var p = mcPick(qa), rows = [{field: 'Question'}];
      // 無い選択肢（Choices の行が足りないとき）は空として出す
      if (qa.side === 'q') (p.swap ? [p.wrong, p.right] : [p.right, p.wrong]).forEach(function (h) { rows.push({html: h}); });
      else rows.push({html: p.right}, {html: p.wrong, dim: true});
      return rows;
    }
    if (qa.kind === 'enum') {
      if (qa.side === 'new')
        return [{field: 'Question'}, {field: 'Items', placeholder: 'Items in order, one per line'}];
      // Items は Anki が描画した cloze（対象の項目は問題面で空欄、回答面で答え。ほかの項目はそのまま）。問題面では対象より後の項目を外す
      return [{field: 'Question'}, {html: qa.side === 'q' ? enumUpToTarget(qa.values.Items || '') : (qa.values.Items || '')}];
    }
    if (qa.side === 'q') return [{field: 'Question'}];
    return [{field: 'Question'}, {hr: true}, {field: 'Answer'}];
  }

  // Enumeration の問題面: Anki が描画した Items の cloze から、対象の項目（class="cloze"）より後を外す。
  // 行は <br> で区切られ、項目ごとに 1 個の span（Anki の cloze の描画。26.09.2 の pylib で確かめた）
  function enumUpToTarget(html) {
    var d = fromHtml(html), top = d.querySelector('.cloze');
    if (!top) return html;
    while (top.parentNode !== d) top = top.parentNode;
    while (top.nextSibling) d.removeChild(top.nextSibling);
    return d.innerHTML;
  }

  // 回答面の Enumeration の項目（Anki が描画した cloze の span の中身。上から順）
  function enumItemsOf(html) {
    return Array.prototype.map.call(fromHtml(html).querySelectorAll('[data-ordinal]'), function (e) { return e.innerHTML; });
  }

  // html を中身にした div（文書には入れない）
  function fromHtml(html) {
    var d = document.createElement('div');
    d.innerHTML = html;
    return d;
  }

  // Multiple Choice の Choices を行に分ける。1 行目が正しい選択肢、2 行目からが誤りの選択肢。
  // 行の区切りは、いちばん外側の <br> とブロックの要素の境目。中身の無い行は数えない（core.py の choice_lines と同じ）
  function choiceLines(html) {
    var box = fromHtml(html), lines = [], cur = [];
    function end() {
      var d = fromHtml(cur.join(''));
      cur = [];
      if (hasContent(d)) lines.push(d.innerHTML.trim());
    }
    (function walk(node) {
      for (var n = node.firstChild; n; n = n.nextSibling) {
        if (n.nodeName === 'BR') end();
        else if (isBlock(n)) { end(); walk(n); end(); }
        else if (n.nodeType === 1) cur.push(n.outerHTML);
        else if (n.nodeType === 3) { var t = document.createElement('span'); t.textContent = n.data; cur.push(t.innerHTML); }
      }
    })(box);
    end();
    return lines;
  }

  // 出すたびに乱数で選ぶもの: True or False の t（True の文を出すか）、Multiple Choice の w（何番目の誤りの選択肢か）と
  // swap（並び）。回答面は問題面と同じものを出す必要があるが、Anki は問題面と回答面を別に描画するので、問題面で選んだものを
  // window._ir2Pick に置き、回答面で読む。学習画面のページは問題面と回答面で同じもの（Anki が中身だけを差し替える。
  // setupEdit の注記）。印は文書、段落、種類で、回答面で印が合わなければ（プレビューで回答面だけを出したときなど）回答面で選ぶ。
  // 印にフィールドの中身を入れないのは、問題面で文や問いを編集しても回答面が同じものを出すようにするため（問題面はいつも
  // 選び直して置くので、別のカードの選んだものを読むのは、回答面だけを出したときに限られる）。
  // 同じ面で並べ直すとき（編集のあと）は選んだものを使い続ける。
  function qaPick(qa) {
    if (!qa.pick) {
      var key = docid + '\u0001' + para + '\u0001' + qa.kind, st = window._ir2Pick;
      qa.pick = (qa.side === 'a' && st && st.key === key) ? st
              : {key: key, t: Math.random() < 0.5, w: Math.random(), swap: Math.random() < 0.5};
      if (qa.side === 'q') window._ir2Pick = qa.pick;
    }
    return qa.pick;
  }

  // Multiple Choice で出す 2 個: 正しい選択肢と、誤りの選択肢から乱数で選んだ 1 個（qaPick の w。0 以上 1 未満の数で、
  // 誤りの選択肢の数を掛けて何番目かにする。編集で誤りの選択肢の数が変わっても範囲を外れない）。問題面では並びも乱数で決める。
  function mcPick(qa) {
    var lines = choiceLines(qa.values.Choices || ''), wrongs = lines.slice(1), p = qaPick(qa);
    return {right: lines[0] || '', wrong: wrongs[Math.floor(p.w * wrongs.length)] || '', swap: p.swap};
  }

  // 対象の段落の要素の先頭に .ir-qa を入れ、qaRows の行を並べる。fields は {フィールド名: HTML}。
  // 戻り値は {box, kind, side, values, editing}。values はフィールドの今の HTML。
  // 編集中に中身が変わるたびに、今の中身を下書きとして Python に送る。Python は、カードを離れるときと、
  // PBIR → Create → Edit のチェックを外したときに、下書きをノートに保存する（__init__.py の _commit_draft）
  function openQA(target, kind, side, fields) {
    var box = document.createElement('div');
    box.className = 'ir-qa';
    var qa = {box: box, kind: kind, side: side, values: {}, editing: false};
    for (var k in fields) qa.values[k] = fields[k];
    layoutQA(qa);
    target.insertBefore(box, target.firstChild);
    box.addEventListener('input', function () {
      if (qa.editing) pycmd('ir2:draft:' + docid + ':' + para + ':' +
                            encodeURIComponent(JSON.stringify({kind: kind, fields: collectQA(qa)})));
    });
    box.addEventListener('paste', onPaste);   // 段落と同じ
    return qa;
  }

  // 行を並べ直す（編集の開始と終了で並びが変わる種類があるので）
  function layoutQA(qa) {
    while (qa.box.firstChild) qa.box.removeChild(qa.box.firstChild);
    qaRows(qa, qa.editing).forEach(function (row) {
      var el = document.createElement(row.hr ? 'hr' : 'div');
      if (row.head) el.textContent = row.head;
      if (row.html !== undefined) el.innerHTML = row.html;
      if (row.field) {
        el.innerHTML = qa.values[row.field] || '';
        el.setAttribute('data-field', row.field);
        el.setAttribute('data-placeholder', row.placeholder || row.field);
        if (qa.editing) el.setAttribute('contenteditable', 'true');
      }
      if (row.dim) el.className = 'ir-qa-dim';
      qa.box.appendChild(el);
    });
  }

  // 出ているフィールドの要素（上から順）
  function qaFields(qa) {
    return Array.prototype.filter.call(qa.box.children, function (el) { return el.hasAttribute('data-field'); });
  }

  // 出ているフィールドの今の中身を values に写して返す
  function collectQA(qa) {
    var fields = {};
    qaFields(qa).forEach(function (el) { fields[el.getAttribute('data-field')] = qa.values[el.getAttribute('data-field')] = el.innerHTML; });
    return fields;
  }

  // 編集を始める（PBIR → Create → Edit にチェックを入れたとき、Create → Basic などと Recreate Enumeration で開いたとき）。
  // フォーカスは最初の空のフィールド（無ければ最初のフィールド）へ
  function startQA(target) {
    if (derived.editing) return;
    derived.editing = true;
    layoutQA(derived);
    var els = qaFields(derived);
    (els.filter(function (el) { return !hasContent(el); })[0] || els[0]).focus();
    pushState(target);
  }

  // 編集を終える（Create → Edit のチェックを外し、Python が下書きを保存したあと）。今の中身で表示の並びに戻す
  function endQA(target) {
    if (!derived.editing) return;
    collectQA(derived);
    derived.editing = false;
    layoutQA(derived);
    pushState(target);
  }

  // PBIR → Create → Recreate Enumeration（Enumeration のカードの回答面）。作るときと同じ欄を、今の問いと項目を入れて出し、
  // 編集を始める（元のカードの表示は消す）。作り直すのはカードを離れるとき（Python が今のノートを捨てて同じ段落に新しいノートを作る）
  function recreateDerived(target) {
    var old = derived;
    old.box.parentNode.removeChild(old.box);
    openCreate(target, old.kind,
               {Question: old.values.Question || '', Items: enumItemsOf(old.values.Items || '').join('<br>')});
  }

  // PBIR → Create → Basic / True or False / Multiple Choice / Enumeration（kind）と Recreate Enumeration。できるカードの回答面の、
  // 編集のときの並びを fields で出し、編集を始める。ノートを作るのはカードを離れるとき
  function openCreate(target, kind, fields) {
    derived = openQA(target, kind, 'new', fields);
    startQA(target);
  }
})();
"""


# どのテンプレートにもある、_doc_js が文書を描画する要素の開始タグ。side は面（q か a）
def _root(side: str) -> str:
    return f'<div id="ir-root" data-doc="{{{{DocId}}}}" data-anchor="{{{{AnchorId}}}}" data-side="{side}">'


def _with_js(html: str) -> str:
    return html + "<script>" + _doc_js() + "</script>"


def _para_template(side: str) -> str:
    """段落のカードのテンプレート。問題面と回答面は面の印（_root）だけが違う。"""
    return _with_js(_root(side) + "{{Text}}</div>\n")


def _cloze_template(side: str) -> str:
    """cloze のカードのテンプレート。問題面と回答面は面の印だけが違う。Anki の cloze 描画をまず器で行い、JS がそれを段落の位置へ移す。
    文書を読めない環境ではフォールバックとして cloze 描画をそのまま見せる。"""
    return _with_js(
        '<div id="ir-cloze-q">{{cloze:Text}}</div>\n'
        + _root(side) + "</div>\n"
        "<script>\n"
        "var q = document.getElementById('ir-cloze-q');\n"
        "var r = document.getElementById('ir-root');\n"
        "r.dataset.clozeHtml = q.innerHTML;\n"
        "</script>\n"
    )


# Basic、True or False、Multiple Choice、Enumeration のフォールバックの器の中身（問題面, 回答面）。回答面には段落の写し（Text）も入れる。
# 問題面で JS が選ぶもの（True or False の文、Multiple Choice の選択肢）と、答えが分かるもの（Enumeration の後の項目）は
# 器では隠す（hidden）。
_QA_FALLBACK = {
    "basic": ('<div class="ir-qa"><div data-field="Question">{{Question}}</div></div>',
              '<div class="ir-qa"><div data-field="Question">{{Question}}</div><hr>'
              '<div data-field="Answer">{{Answer}}</div></div>{{Text}}'),
    "tf": ('<div class="ir-qa"><div>True or False?</div>'
           '<div data-field="True" hidden>{{True}}</div><div data-field="False" hidden>{{False}}</div></div>',
           '<div class="ir-qa"><div data-field="True">{{True}}</div>'
           '<div data-field="False" class="ir-qa-dim">{{False}}</div></div>{{Text}}'),
    "mc": ('<div class="ir-qa"><div data-field="Question">{{Question}}</div>'
           '<div data-field="Choices" hidden>{{Choices}}</div></div>',
           '<div class="ir-qa"><div data-field="Question">{{Question}}</div>'
           '<div data-field="Choices">{{Choices}}</div></div>{{Text}}'),
    "enum": ('<div class="ir-qa"><div data-field="Question">{{Question}}</div>'
             '<div data-field="Items" hidden>{{cloze:Items}}</div></div>',
             '<div class="ir-qa"><div data-field="Question">{{Question}}</div>'
             '<div data-field="Items">{{cloze:Items}}</div></div>{{Text}}'),
}


def _qa_template(kind: str, side: str) -> str:
    """Basic、True or False、Multiple Choice、Enumeration のテンプレート。side は q（問題面）か a（回答面）。

    器（_QA_FALLBACK）のフィールドは data-field にフィールド名を書いた要素に入れる。スクリプトがその中身を集め、種類と一緒に
    ir-root の data-qa に JSON で写す。_doc_js がそれを段落の位置に並べ（qaRows）、文書を読めたら器を隠す。
    文書を読めない環境では器がそのまま見える（cloze と同じやり方）。
    """
    fallback = _QA_FALLBACK[kind][0 if side == "q" else 1]
    return _with_js(
        f'<div id="ir-qa-fb">{fallback}</div>\n'
        + _root(side) + "</div>\n"
        "<script>\n"
        "var r = document.getElementById('ir-root'), f = {};\n"
        "document.querySelectorAll('#ir-qa-fb [data-field]').forEach(function (e) "
        "{ f[e.dataset.field] = e.innerHTML; });\n"
        f"r.dataset.qa = JSON.stringify({{kind: '{kind}', fields: f}});\n"
        "</script>\n"
    )
