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
from typing import NamedTuple

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from bs4.dammit import EncodingDetector, UnicodeDammit

from anki.collection import Collection
from anki.consts import MODEL_CLOZE
from anki.utils import ids2str


# ---------------------------------------------------------------- ノートタイプとノート

PARA_MODEL = "IR2 Paragraph"
CLOZE_MODEL = "IR2 Cloze"


class Quiz(NamedTuple):
    model: str           # ノートタイプ
    label: str           # PBIR → Create の項目の名前
    fields: tuple        # 利用者が書くフィールド


# quiz: 段落に付く問いと答えのカード（PBIR → Create → Basic など）。キーは種類（JS とのやりとりの名前）。
# AnchorId は段落の ID（p12）そのもの。問いと答えは文書に無く、利用者が書くフィールドだけが持つ（Text は段落の写し）。
# 作ったあとは学習画面では変えない（直すときは Anki の編集画面で）。
#   Basic: 問いと答え。
#   True or False: カードは 1 枚で、出すたびに True と False のどちらの文を出すかを選ぶ。
#   Multiple Choice: Choices は 1 行目が正しい選択肢、2 行目からが誤りの選択肢（choice_lines）。カードは 1 枚で、出すたびに
#     誤りを 1 個選ぶ。
#   Enumeration: cloze 型。Items は順序のある項目を 1 行に 1 個、項目ごとに cloze 1 個（enum_items）。番号は後ろの項目から
#     c1, c2, ...（Anki は同じノートのカードを番号の小さい順に出すので、後ろの項目から学ぶ）。
QUIZ = {"basic": Quiz("IR2 Basic", "Basic", ("Question", "Answer")),
        "tf": Quiz("IR2 True or False", "True or False", ("True", "False")),
        "mc": Quiz("IR2 Multiple Choice", "Multiple Choice", ("Question", "Choices")),
        "enum": Quiz("IR2 Enumeration", "Enumeration", ("Question", "Items"))}
QUIZ_MODELS = tuple(q.model for q in QUIZ.values())

# アドオンのノートタイプ全部
IR_MODELS = (PARA_MODEL, CLOZE_MODEL) + QUIZ_MODELS


def models_search(models=IR_MODELS) -> str:
    """ノートタイプが models のどれかのノートを探す検索の文字列。"""
    return "(" + " OR ".join(f'"note:{m}"' for m in models) + ")"


def _doc_search(docid: str, models=IR_MODELS) -> str:
    """文書 docid のノートのうち、ノートタイプが models のどれかのものを探す検索の文字列。"""
    return f'"DocId:{docid}" {models_search(models)}'


def _doc_notes(col: Collection, docid: str, models=IR_MODELS) -> list:
    """文書 docid のノートのうち、ノートタイプが models のどれかのもの。"""
    return [col.get_note(nid) for nid in col.find_notes(_doc_search(docid, models))]


def _doc_cards(col: Collection, docid: str) -> list:
    """文書 docid のアドオンのノート（段落、cloze、quiz）のカード。"""
    return [col.get_card(cid) for cid in col.find_cards(_doc_search(docid))]


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


# ---------------------------------------------------------------- 文書の要素

# カードにするブロック（segment）。文書の直下にある p とリスト。リストは項目ごとでなく全体で 1 枚。
# 見出し、pre、blockquote、表、図、hr などは表示するがカードにしない（中の p もカードにしない）
CARD_TAGS = ("p", "ul", "ol", "dl")
LIST_TAGS = ("ul", "ol", "dl")

# 取り込みで外す入れ物（segment）。Readability の出力では本文がこれらの入れ子になっていて、class と style は
# Readability が外している（Readability.js の _cleanClasses と _cleanStyles）。main と header は HTML5 の入れ物
CONTAINER_TAGS = ("div", "section", "article", "main", "header")

# 取り込み時に落とす要素（スクリプトを動かしうる要素は sanitize が UNSAFE_TAGS で落とす）
DROP_TAGS = ["nav", "form", "button"]

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
# 文字が無くても中身に数える要素（_has_content）。カードの JS も同じものを使う（_doc_js に渡す）
MEDIA_TAGS = {"img", "svg", "video", "audio", "canvas", "iframe", "object", "embed", "math"}
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]

# このアドオンが文書に書く印。取り込む HTML に既にあれば外す。アドオンが書いた文書
# （原本 _ir2_<docid>.orig.html など）を取り込み直すと、見出しなど段落でない要素に古い
# data-ir が残り、振り直した段落の ID と重なるため（2026-09-25、プレビューで見つかった）。
IR_ATTRS = ("data-ir", "data-cloze", "data-ir-split", "contenteditable")


def _is_blank(node) -> bool:
    return isinstance(node, NavigableString) and not node.strip()


def _is_block(node) -> bool:
    return isinstance(node, Tag) and node.name in BLOCK_TAGS


def _has_content(el) -> bool:
    """文字か、画像などの要素を持つか。空白と <br> だけなら持たない。"""
    return bool(el.get_text().strip()) or el.find(lambda t: t.name in MEDIA_TAGS) is not None


def para_text(el) -> str:
    """段落の要素の文字（空白は 1 個にまとめ、前後を外す）。ファイルの重複を比べる単位。"""
    return " ".join(el.get_text().split())


def _para_num(aid: str) -> int:
    m = re.fullmatch(r"p(\d+)", aid.split(".")[0])
    return int(m.group(1)) if m else 0


def plural(n: int, word: str) -> str:
    """利用者への知らせの数と語（1 paragraph, 2 paragraphs）。"""
    return f"{n} {word}{'' if n == 1 else 's'}"


def _clip(text: str, n: int) -> str:
    """利用者への知らせに出す文字の先頭 n 文字（長ければ ... を付ける）。"""
    return text if len(text) <= n else text[:n] + "..."


# ---------------------------------------------------------------- 文書のファイル

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


def new_docid() -> str:
    return format(int(time.time() * 1000), "x")


# 文書の Source（記事の出典の URL）。作業コピーの先頭のコメントで、取り込み（import_article）と
# Add/Edit Source（set_doc_source）が書く（_with_source）
_SOURCE_RE = re.compile(r"\s*<!-- ir:source (.*?) -->\n?")


def _with_source(body: str, url: str) -> str:
    """Source のコメントの無い文書 body の先頭に、url の Source のコメントを付ける。url の前後の空白と、コメントを
    閉じてしまう "-->" は外す。空なら付けない。"""
    url = _clean_url(url)
    return (f"<!-- ir:source {url} -->\n" if url else "") + body


def _clean_url(url: str) -> str:
    return url.replace("-->", "").strip()


def doc_source(col: Collection, docid: str) -> str:
    """文書の Source。無ければ（文書のファイルが無いときも）空文字。"""
    try:
        m = _SOURCE_RE.match(read_doc(col, docid))
    except OSError:
        return ""
    return m.group(1) if m else ""


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


# ---------------------------------------------------------------- スクリプトの除去

# 取り込む HTML は他人が書いたもの。カードは文書を innerHTML で入れるので、<script> は動かないが、
# onerror などのイベント属性は動く（Chromium で確認）。そうすると記事の書き手のコードが学習画面で
# pycmd やネットワークを使える。Readability も出力を消毒しない（README の Security の節が、
# 信頼できない入力では出力を消毒せよと勧めている）。そこで、コードを動かしうるものを外す。
# 取り込み（normalize）、保存（save_edit, extract_paragraph）、
# Open Document の表示用の複製（sanitize_html）で行う。原本そのものは変えない。

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


# ---------------------------------------------------------------- 取り込み

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


# ---------------------------------------------------------------- 取り込むファイルとページの読み方

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


def text_to_html(text: str) -> str:
    """平文を段落の HTML に起こす。空行で段落を区切る（改行は article_html が \n にそろえる）。"""
    return "".join(f"<p>{html.escape(b.strip(), quote=False)}</p>" for b in text.split("\n\n") if b.strip())


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


# ---------------------------------------------------------------- ノートタイプとテンプレート

class _ModelSpec(NamedTuple):
    cloze: bool          # cloze 型か
    fields: tuple
    template: str        # テンプレート（カードの種類）の名前
    front: str
    back: str


def _model_specs() -> dict:
    """アドオンのノートタイプの名前 → _ModelSpec。テンプレートはどれも 1 枚（Enumeration は cloze 型で、カードは項目の数だけ）。"""
    ids = ("DocId", "AnchorId", "Text")
    specs = {PARA_MODEL: _ModelSpec(False, ids, "IR", _para_template("q"), _para_template("a")),
             CLOZE_MODEL: _ModelSpec(True, ("Text", "DocId", "AnchorId"), "Cloze",
                                     _cloze_template("q"), _cloze_template("a"))}
    for kind, q in QUIZ.items():
        specs[q.model] = _ModelSpec(kind == "enum", q.fields + ids, q.label,
                                    _qa_template(kind, "q"), _qa_template(kind, "a"))
    return specs


def ensure_models(col: Collection) -> None:
    """無いアドオンのノートタイプを作る（取り込み）。"""
    for name, spec in _model_specs().items():
        if col.models.by_name(name):
            continue
        m = col.models.new(name)
        if spec.cloze:
            m["type"] = MODEL_CLOZE
        for f in spec.fields:
            col.models.add_field(m, col.models.new_field(f))
        col.models.add_template(m, col.models.new_template(spec.template))
        _set_template(m, spec)
        col.models.add(m)


def update_models(col: Collection) -> None:
    """あるアドオンのノートタイプのテンプレートと CSS を今の内容にする（起動時）。テンプレートはノートタイプを作ったときに
    書かれたきりなので、アドオンの更新をコレクションへ届けるにはこれが要る。"""
    for name, spec in _model_specs().items():
        m = col.models.by_name(name)
        if m:
            _set_template(m, spec)
            col.models.update_dict(m)


def _set_template(m: dict, spec: _ModelSpec) -> None:
    m["tmpls"][0]["qfmt"], m["tmpls"][0]["afmt"] = spec.front, spec.back
    m["css"] = _css()


_HERE = os.path.dirname(os.path.abspath(__file__))
_assets: dict = {}   # card.css と card.js の中身（Anki を閉じるまで覚えておく）


def _asset(name: str) -> str:
    if name not in _assets:
        with open(os.path.join(_HERE, name), encoding="utf-8") as f:
            _assets[name] = f.read()
    return _assets[name]


def _css() -> str:
    """カードの CSS（card.css）。ノートタイプの CSS と、取り込みのプレビューに使う。"""
    return _asset("card.css")


def _doc_js() -> str:
    """カードのテンプレートに入れる JS（card.js）。印（__ で挟んだ大文字の名前）を core.py の値に置き換える。

    渡すのは、ブロックの要素（BLOCK_TAGS）、中身に数える要素（MEDIA_TAGS）、貼り付けで落とす要素（UNSAFE_TAGS。sanitize）、
    quiz の種類（QUIZ）。JS はテンプレートにそのまま入るので、Anki のテンプレート記法の開き括弧 2 個を含んではいけない
    （card.js の先頭の注意）。含んでいれば ValueError。
    """
    js = _asset("card.js")
    for name, val in (("BLOCK_TAGS", sorted(BLOCK_TAGS)), ("MEDIA_TAGS", sorted(MEDIA_TAGS)),
                      ("UNSAFE_TAGS", UNSAFE_TAGS), ("QUIZ_KINDS", list(QUIZ))):
        js = js.replace(f"__{name}__", json.dumps(val))
    if "{{" in js:
        raise ValueError("card.js must not contain a double opening brace (Anki would read it as a field)")
    return js


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


# quiz のフォールバックの器の中身（問題面, 回答面）。回答面には段落の写し（Text）も入れる。
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
    """quiz のテンプレート。side は q（問題面）か a（回答面）。

    器（_QA_FALLBACK）のフィールドは data-field にフィールド名を書いた要素に入れる。スクリプトがその中身を集め、種類と一緒に
    ir-root の data-qa に JSON で写す。card.js がそれを段落の位置に並べ（qaRows）、文書を読めたら器を隠す。
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


# ---------------------------------------------------------------- 段落の保存

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


def _put_para(el, inner_html: str) -> None:
    """段落の要素 el の中身を inner_html にする（保存と Extract）。スクリプトを動かしうるものと、中の段落の印を外す
    （段落の中に段落の印があってはならない。Source Text で手で書ける）。cloze の印は残す。"""
    el.clear()
    el.append(BeautifulSoup(inner_html, "html.parser"))
    sanitize(el)
    for t in el.find_all(attrs={"data-ir": True}):
        del t["data-ir"]


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


def regen_notes(col: Collection, docid: str, anchor: str) -> int:
    """指定段落に紐づくノート（段落ノート、その段落内の cloze ノート、その段落の quiz のノート）の
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
        if (ntype == PARA_MODEL or ntype in QUIZ_MODELS) and aid == anchor:
            note["Text"] = el.decode_contents()
            col.update_note(note)
            updated += 1
        elif ntype == CLOZE_MODEL and aid.startswith(anchor + "."):
            note["Text"] = cloze_text(el, aid)
            col.update_note(note)
            updated += 1
    return updated


# ---------------------------------------------------------------- cloze の印の後始末

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


# ---------------------------------------------------------------- cloze

# JS は選んだ所を data-cloze="PENDING" の span で包んで送る（make_cloze が id を振る）。JS は返された id で置き換える
PENDING = "PENDING"


def _cloze_ids(el) -> list:
    return [s["data-cloze"] for s in el.find_all(attrs={"data-cloze": True})]


def _cloze_num(aid: str) -> int:
    m = re.search(r"\.c(\d+)$", aid)
    return int(m.group(1)) if m else 0


def _cloze_notes(col: Collection, docid: str, para: str) -> dict:
    """段落 para の cloze ノート。{AnchorId: note}"""
    return {note["AnchorId"]: note for note in _doc_notes(col, docid, (CLOZE_MODEL,))
            if note["AnchorId"].split(".")[0] == para}


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


def cloze_word(para_el, cloze_id: str) -> str:
    """cloze のマークの文字（選んだ語）。空白はまとめる。マークが無ければ空文字。"""
    span = para_el.find(attrs={"data-cloze": cloze_id})
    return para_text(span) if span is not None else ""


# 分かち書きをしない文字（かな、漢字）。これらは語の切れ目を持たないので、段落の中の
# 部分文字列として探す（\b を使うと「統語論は」の「統語論」が見つからない）
_UNSPACED = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ"
                       r"\U00020000-\U0003ffff]")


def _joins(a: str, b: str) -> bool:
    """文字 a と b が隣り合うと 1 語の途中になるか（どちらも分かち書きをする文字や数字）。"""
    return all(c.isalnum() and not _UNSPACED.match(c) for c in (a, b))


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


# ---------------------------------------------------------------- quiz

def _qa_html(html: str) -> str:
    """問いか答えの HTML（学習画面で書いた欄の innerHTML）を、フィールドに入れる形にする。

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


def quiz_fields(kind: str, fields: dict) -> dict:
    """quiz のノート（kind は QUIZ のキー）に入れるフィールドの値を作る。フィールドの名前が合わなければ ValueError。

    fields は {フィールド名: 学習画面で書いた HTML} で、利用者が書くフィールドが全部要る。空のフィールドも、行の足りないフィールドも
    そのまま保存する（足りない行は空として扱う）。Choices と Items は行に分け（choice_lines）、Choices は行を <br> でつなぐ
    （表示の JS は行に分けて使い、無い行は空として出す）。Items は enum_items の形にする。項目が 1 個も無ければ空の項目 1 個にする
    （cloze 型のノートは cloze が無いとカードができないので。空の cloze も Anki は cloze として数える）。
    """
    quiz = QUIZ[kind]
    if set(fields) != set(quiz.fields):
        raise ValueError(f"fields {sorted(fields)} do not match {quiz.model}")
    vals = {name: _qa_html(html) for name, html in fields.items()}
    if "Choices" in vals:
        vals["Choices"] = "<br>".join(choice_lines(vals["Choices"]))
    if "Items" in vals:
        vals["Items"] = enum_items(choice_lines(vals["Items"]) or [""])
    return vals


def create_quiz(col: Collection, docid: str, anchor: str, kind: str, fields: dict, deck_id: int) -> int:
    """PBIR → Create → Basic など。段落 anchor に quiz のノート（kind は QUIZ のキー）を 1 件作る。戻り値は note id。

    fields は {フィールド名: 学習画面で書いた HTML}（quiz_fields）。Text は段落の写し。文書は変えない。
    """
    return _add_note(col, QUIZ[kind].model, docid, anchor, para_source(col, docid, anchor), deck_id,
                     fields=quiz_fields(kind, fields))


# ---------------------------------------------------------------- Extract と結合

# PBIR → Extract, Merge Previous, Merge Next。どれも文書とノートの両方を変える。
# 文書は media のファイルで Anki の undo の対象外なので、undo で片方だけが戻らないように、
# 操作のあと undo の履歴を空にする（discard_undo。cloze の作成も同じ）。
# 断る場合は Refused を投げる。どの場合も、文書にもノートにも書く前に判定する。

SPLIT_MARK = "data-ir-split"   # JS が選択の始まり（値は start）と終わり（end）に入れる印の属性（extract_paragraph）
_NOT_COPIED = ("data-ir", "id", "contenteditable")  # 分けるときに新しい側の要素に写さない属性


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
                media and isinstance(node, Tag) and node.name in MEDIA_TAGS):
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


def _new_para_id(col: Collection, docid: str, soup) -> str:
    """文書とノートのどちらでも使われていない段落 ID。既存の最大の番号 + 1。"""
    nums = [_para_num(e["data-ir"]) for e in soup.find_all(attrs={"data-ir": True})]
    nums += [_para_num(note["AnchorId"]) for note in _doc_notes(col, docid)]
    return f"p{max(nums, default=0) + 1}"


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
    元の段落の quiz のノートは元の段落に残る。
    選択の端が cloze の途中か数式（<math>）の中なら断る。選んだ部分に中身が無いとき、前と後ろのどちらにも中身が無いときも断る。
    """
    soup, el, _msgs = _with_para(col, docid, anchor, inner_html)   # save_edit と同じ
    found = el.find_all(attrs={SPLIT_MARK: True})
    marks = {m[SPLIT_MARK]: m for m in found}
    if len(found) != 2 or set(marks) != {"start", "end"}:
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
    取り込んだカードの段落ノートは削除する（学習履歴も消える）。その cloze は anchor のカードへ移して id を振り直し、quiz のノートも
    anchor のカードを指すようにする（どちらもノートとカードはそのまま）。文書にマークの無い cloze のノートは移さない（cloze が
    無いので、評価のあとに消える。remove_empty）。
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
    for note in _doc_notes(col, docid, QUIZ_MODELS):
        if note["AnchorId"] == other_id:
            note["AnchorId"] = anchor
            col.update_note(note)
            n_moved += 1
    regen_notes(col, docid, anchor)
    return other_id, n_moved, len(para_nids)


# ---------------------------------------------------------------- 評価のあとの後始末

def remove_empty(col: Collection, docid: str) -> tuple:
    """カードを評価したあと、次のカードを出す前に呼ぶ（reviewer.py の on_answer_card。reviewer_did_answer_card）。

    文書 docid から次のものを消す。
      - 中身の無い段落（_has_content。取り込みで段落にしない要素と同じ決め方）の要素。
      - 生きていない段落のアドオンのノート（段落、cloze、quiz）。
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


# ---------------------------------------------------------------- 重複

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


# ---------------------------------------------------------------- デッキ

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
    確かめた）ので、その前に作る。戻り値は (デッキの名前, Anki の OpChanges)。documents.py の new_deck が CollectionOp で呼ぶ。"""
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
