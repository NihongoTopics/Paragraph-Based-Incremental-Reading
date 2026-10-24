# -*- coding: utf-8 -*-
"""アドオン Paragraph-Based Incremental Reading の取り込みの規則の写し。pdf-to-ir-html がアドオンの無い環境でも動くように、スキルの中に持つ。

写し元: addon2/ir2/core.py。
取り込みの正規化とカードの判定（normalize, lift_headings, segment をまとめた prepare_article）、取り込みダイアログのプレビュー（preview_html とカードの
CSS の _css）、それらが使う定数を、関数と定数の単位でそのまま写した（ast で取り出した。書き換えていない）。
アドオンの取り込みの規則が変わったら、写し直す。
"""

import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


CARD_TAGS = ("p", "ul", "ol", "dl")


CONTAINER_TAGS = ("div", "section", "article", "main", "header")


DROP_TAGS = ["nav", "form", "button"]


IR_ATTRS = ("data-ir", "data-cloze", "data-ir-split", "contenteditable")


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


_MEDIA_TAGS = {"img", "svg", "video", "audio", "canvas", "iframe", "object", "embed", "math"}


def _has_content(el) -> bool:
    """文字か、画像などの要素を持つか。空白と <br> だけなら持たない。"""
    return bool(el.get_text().strip()) or el.find(lambda t: t.name in _MEDIA_TAGS) is not None


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
