#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ページの断片（WORK/frag/pNNNN.html）をつないで、アドオンが取り込む HTML を 1 本作る。

使い方: assemble.py WORK OUT.html --lang en [--pages SPEC] [--allow-math-errors]

  --lang   文書の主な言語（BCP 47）。全体を包む <article lang="..."> に付く。

出力は <article> 1 個だけ（<html>, <head>, <title>, <body> は書かない）。取り込みは body の中身だけを使い
（core.normalize）、文字コードは宣言が無くても UTF-8 として読む（core.decode_text）。取り込みで残らないもの
（style、class、Readability の表示用の属性）は出力に書かない。check.py が確かめる。

すること:
- 脚注 <aside class="pdf-footnote"> を、そのページで合印（脚注の番号か記号と同じ文字の <sup>）のある、文書の直下の要素
  （段落、リスト、表、図など）のすぐ後へ移す。1 つの要素に脚注が幾つもあれば、ページに書いた順に並べる。合印と同じ文字の
  <sup> がページの二つ以上の要素にあれば（m<sup>2</sup> など）、脚注がそのどれかのすぐ後（ほかの脚注を挟んでよい）に
  書いてあればそこに置き、そうでなければ移さずに警告する。合印が見つからないときも、移さずに警告する。
- ページの順につなぐ。data-pdf-cont の付いた要素を、前のページの同じ種類の要素につなぐ（space / none / dehyphen）。
  ul, ol, table, blockquote の続きは、中の項目や行を前のものに移し、最初の項目に data-pdf-cont があればそれもつなぐ。
- data-pdf-page の span <span data-pdf-page="N" [data-page-label="…"]></span> を、各ページの最初の要素の中に入れる
  （続きの段落ではつなぎ目に）。段落の間には入れない（入れると Merge Previous/Next が使えなくなる）。
- <tex> を MathML にする（tex2mml.py）。TeX の誤りがあれば一覧を出して止まる（--allow-math-errors で続ける）。
- <img src="figs/..."> を data URI にする。src と alt 以外の属性は外す。
- アドオンの取り込みに合わせて形を替える（どれもアドオンの取り込みの規則による。references/markup.md）:
  - 脚注 <aside class="pdf-footnote"> を外し、中の <p> を本文の段落と並べる（aside の中はカードにならないため）。
  - <ul type="none"> と <ol type="none"> の type="none" を、直下の各 <li> に移す（ブラウザが番号と記号を付けない。
    ol の type="none" は効かず、li の type="none" は効く）。
- 下書きの印（data-box, <!--hy?--> などのコメント）とソフトハイフンを外す。
- 取り込みで問題になる形（段落の中のブロック、figure の外の img など）を調べ、誤りとして止まる。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import base64
import re
from pathlib import Path

C.need("bs4", "beautifulsoup4")
from bs4 import BeautifulSoup, Comment, NavigableString, Tag  # noqa: E402

import ir_rules as rules  # noqa: E402  アドオンの取り込みの規則の写し
import tex2mml  # noqa: E402

CARD_TAGS = ("p", "ul", "ol", "dl")          # 文書の直下でカードになるブロック（アドオンの core.segment）
BLOCK_KIDS = ("p", "li", "blockquote", "pre")  # 中にこれがある blockquote は、続きをつなぐとき入れ物として扱う
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
FLOATS = ("aside", "figure", "table")      # 前のページの段落の後にあってよいもの（続きをつなぐとき飛ばす）
CONTAINERS = ("ol", "ul", "table", "tbody", "thead", "dl")
TEXT_HOLDERS = ("p", "li", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "caption", "td", "th",
                "dt", "dd", "div", "blockquote", "aside")
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp"}
DRAFT_COMMENT = re.compile(r"^\s*(hy\?|math\?|lang\?|cont\?|furniture:|draft of page|the page is rotated|page-label:)")
PAGE_LABEL = re.compile(r"<!--\s*page-label:\s*(.*?)\s*-->")


class Problems:
    def __init__(self):
        self.errors, self.warnings = [], []

    def page_of(self, el) -> str:
        while el is not None and not (isinstance(el, Tag) and el.has_attr("data-pg")):
            el = el.parent
        return f"p. {el['data-pg']}" if el is not None else "p. ?"

    def error(self, el, msg):
        self.errors.append(f"{self.page_of(el)}: {msg}")

    def warn(self, el, msg):
        self.warnings.append(f"{self.page_of(el)}: {msg}")


def snippet(el, n=60) -> str:
    return " ".join(el.get_text(" ").split())[:n]


def elements(node):
    return [c for c in node.children if isinstance(c, Tag)]


def marker(soup, n: int, label):
    span = soup.new_tag("span")
    span["data-pdf-page"] = str(n)
    if label:
        span["data-page-label"] = label
    return span


# ---------------------------------------------------------------- data-pdf-page の span

def first_holder(el):
    """data-pdf-page の spanを入れる要素。el の中で、文字を持つ最初の要素（段落、項目、キャプション、升目など）。"""
    if el.name == "figure":
        cap = el.find("figcaption")
        return cap if cap is not None else el
    if el.name == "table":
        return el.find(["caption", "th", "td"]) or el
    for child in el.find_all(TEXT_HOLDERS):
        if not child.find(TEXT_HOLDERS):   # いちばん内側の、文字を持つ要素
            return child
    return el


# ---------------------------------------------------------------- 脚注を合印のある段落の後へ

FOOTNOTE_MARK = re.compile(r"\s*(\d+|[*†‡§¶‖#]+)")   # <sup> で書いていない脚注の先頭の番号か記号


def is_footnote(el) -> bool:
    return isinstance(el, Tag) and el.name == "aside" and "pdf-footnote" in el.get("class", [])


def footnote_mark(note) -> str:
    """脚注の合印の文字。脚注が <sup> で始まればその文字、そうでなければ先頭の数字か記号。無ければ空文字。"""
    sup = note.find("sup")
    if sup is not None and sup.get_text().strip() and note.get_text().lstrip().startswith(sup.get_text().strip()):
        return sup.get_text().strip()
    m = FOOTNOTE_MARK.match(note.get_text())
    return m.group(1) if m else ""


def place_footnotes(frag, P: Problems) -> None:
    """ページの断片 frag の脚注を、合印のある直下の要素のすぐ後へ移す（モジュールの説明の「すること」の最初の項目）。
    1 つの要素の後の脚注は、その要素の中の合印の順に並べる。"""
    sups = frag.find_all("sup")
    placed = {}   # 直下の要素 → [(合印の <sup> の位置, 脚注)]
    for note in [el for el in frag.contents if is_footnote(el)]:
        mark = footnote_mark(note)
        refs = {}   # 直下の要素 → その中で最初の合印の <sup> の位置
        for i, sup in enumerate(sups):
            if mark and sup.get_text().strip() == mark and not any(is_footnote(a) for a in sup.parents):
                refs.setdefault(next(a for a in [sup] + list(sup.parents) if a.parent is frag), i)
        written_after = next((el for el in note.find_previous_siblings() if not is_footnote(el)), None)
        block = next(iter(refs)) if len(refs) == 1 else written_after if written_after in refs else None
        if block is None:
            P.warn(note, f"footnote {mark!r}: " + (f"<sup>{mark}</sup> is in {len(refs)} blocks of the page; write "
                                                   f"the footnote right after the one it belongs to"
                                                   if refs else f"no <sup>{mark}</sup> in the text of the page")
                   + f"; it stays where it is: {snippet(note)!r}")
            continue
        placed.setdefault(block, []).append((refs[block], note))
    for block, notes in placed.items():
        for _i, note in sorted(notes, key=lambda x: x[0], reverse=True):   # 後ろの脚注から、要素のすぐ後へ
            block.insert_after(note.extract())


# ---------------------------------------------------------------- 続きをつなぐ

def last_text(el):
    texts = [t for t in el.find_all(string=True) if not isinstance(t, Comment)]
    return texts[-1] if texts else None


def join_inline(target, el, mode: str, mark, P: Problems) -> None:
    """el（続きの部分）の中身を target の末尾につなぐ。mark（data-pdf-page の span）をつなぎ目に入れる。"""
    t = last_text(target)
    if t is not None:
        s = str(t).rstrip()
        if mode == "dehyphen":
            if s.endswith(("-", "‐", "­")):
                s = s[:-1]
            else:
                P.warn(el, f"data-pdf-cont=\"dehyphen\" but the previous part does not end with a hyphen: "
                           f"...{s[-30:]!r}")
        elif mode == "space":
            s += " "
        t.replace_with(NavigableString(s))
    target.append(mark)
    for c in list(el.contents):
        target.append(c.extract())


def continue_into(target, el, mark, P: Problems) -> None:
    mode = el.get("data-pdf-cont") or "space"
    if mode not in ("space", "none", "dehyphen"):
        P.error(el, f'data-pdf-cont must be space, none or dehyphen, not "{mode}"')
        mode = "space"
    if el.name in CONTAINERS or (el.name == "blockquote" and el.find(BLOCK_KIDS)):
        # 入れ物の続き: 最初の子が続きなら（tbody は常に）前の同じ種類の子につなぎ、残りの子を移す
        kids = elements(el)
        first = kids[0] if kids else None
        if first is not None:
            same = [k for k in elements(target) if k.name == first.name]
            if same and (first.has_attr("data-pdf-cont") or first.name == "tbody"):
                continue_into(same[-1], first, mark, P)
            else:
                first_holder(first).insert(0, mark)
        for c in list(el.contents):
            target.append(c.extract())
    else:
        join_inline(target, el, mode, mark, P)
    el.decompose()


def find_target(article, name: str):
    """前のページまでの本文で、続きをつなぐ相手（同じ種類の最後の要素）。脚注、図、表は飛ばす。"""
    for el in reversed(elements(article)):
        if el.name == name:
            return el
        if el.name in FLOATS and name not in FLOATS:
            continue
        if el.name == "span" and el.has_attr("data-pdf-page"):
            continue
        return None
    return None


# ---------------------------------------------------------------- 読み込み

def load_fragment(path: Path, n: int, P: Problems):
    raw = path.read_text(encoding="utf-8")
    m = PAGE_LABEL.search(raw)
    label = m.group(1) if m else None
    soup = BeautifulSoup(raw, "html.parser")
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        if DRAFT_COMMENT.match(str(c)):
            c.extract()
    for bad in ("html", "head", "body"):
        for t in soup.find_all(bad):
            P.warn(None, f"{path.name}: <{bad}> in a fragment was unwrapped")
            t.unwrap()
    for t in soup.find_all(True):
        t.attrs.pop("data-box", None)
        if t.parent is soup or t.name in ("tex", "img", "p", "li"):
            t["data-pg"] = str(n)
    return soup, label


def assemble(work: Path, pages: list, P: Problems):
    doc = BeautifulSoup("", "html.parser")
    article = doc.new_tag("article")
    doc.append(article)
    for n in pages:
        frag, label = load_fragment(C.work_path(work, "frag", C.pname(n) + ".html"), n, P)
        place_footnotes(frag, P)
        mark = marker(doc, n, label)
        els = elements(frag)
        if not els:
            P.warn(None, f"p. {n}: the fragment has no elements (blank page?)")
            continue
        first = els[0]
        if first.has_attr("data-pdf-cont"):
            target = find_target(article, first.name)
            if target is None:
                P.error(first, f"<{first.name} data-pdf-cont> but the previous page does not end with a "
                               f"<{first.name}>: {snippet(first)!r}")
                first.attrs.pop("data-pdf-cont")
                first_holder(first).insert(0, mark)
            else:
                continue_into(target, first, mark, P)
        else:
            first_holder(first).insert(0, mark)
        for c in list(frag.contents):
            article.append(c.extract())
        for t in article.find_all(attrs={"data-pdf-cont": True}):
            P.error(t, f"data-pdf-cont is only allowed on the first element of a page (or on the first item of a "
                       f"continued list/table): <{t.name}> {snippet(t)!r}")
            del t["data-pdf-cont"]
    return doc, article


# ---------------------------------------------------------------- 変換

def convert_math(work: Path, article, P: Problems, allow: bool) -> int:
    texs = article.find_all("tex")
    if not texs:
        return 0
    res = tex2mml.convert([(t.get_text(), t.has_attr("display")) for t in texs], work)
    for t, r in zip(texs, res):
        if r["error"]:
            (P.warn if allow else P.error)(t, f"TeX error ({r['error']}): {t.get_text()!r}")
        math = BeautifulSoup(r["mathml"], "html.parser").find("math")
        t.replace_with(math)
    return len(texs)


def embed_images(work: Path, article, P: Problems) -> tuple:
    count = total = 0
    for img in article.find_all("img"):
        src = img.get("src", "")
        alt = img.get("alt")
        if not src.startswith("data:"):
            path = C.work_path(work, src)
            mime = MIME.get(path.suffix.lower())
            if not path.is_file():
                P.error(img, f"image not found: {src}")
                continue
            if mime is None:
                P.error(img, f"unsupported image type: {src} (use PNG or JPEG)")
                continue
            data = path.read_bytes()
            src = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            total += len(data)
        count += 1
        if not alt:
            P.warn(img, "img without alt")
        pg = img.get("data-pg")
        img.attrs = {"src": src, "alt": alt or ""}
        if pg:
            img["data-pg"] = pg
    return count, total


def reshape(article, P: Problems) -> None:
    """アドオンの取り込みに合わせて形を替える（モジュールの説明の「アドオンの取り込みに合わせて形を替える」）。"""
    for aside in article.find_all("aside"):
        if "pdf-footnote" in aside.get("class", []):
            aside.unwrap()
        else:
            P.warn(aside, f"<aside> without class pdf-footnote (its text gets no card): {snippet(aside)!r}")
    for lst in article.find_all(["ul", "ol"], attrs={"type": True}):
        if lst["type"].lower() == "none":
            del lst["type"]
            for li in lst.find_all("li", recursive=False):
                li["type"] = "none"


def tidy(article) -> None:
    for t in article.find_all(string=True):
        if "­" in t and not isinstance(t, Comment):
            t.replace_with(NavigableString(str(t).replace("­", "")))


# ---------------------------------------------------------------- 調べる

def check_structure(article, P: Problems) -> None:
    dropped = set(rules.DROP_TAGS) | set(rules.UNSAFE_TAGS)
    for t in article.find_all(True):
        if t.name in dropped:
            P.error(t, f"<{t.name}> is removed by the addon on import")
        for a in list(t.attrs):
            if a.lower().startswith("on"):
                P.error(t, f"event attribute {a} on <{t.name}> (removed on import)")
        if t.name == "tex":
            P.error(t, "<tex> left over")
        if t.name == "a":
            P.warn(t, f"link <a> (write URLs as text): {snippet(t)!r}")
    for p in article.find_all("p"):
        for b in p.find_all(list(rules.BLOCK_TAGS)):
            P.error(b, f"<{b.name}> inside <p> (the import closes the <p> there; what follows becomes another "
                       f"block or card): {snippet(p)!r}")
    for img in article.find_all("img"):
        if img.find_parent("figure") is None:
            P.error(img, "img outside <figure> (on import it is put in a <p> and becomes a card)")
    for t in article.find_all(True):
        cls = " ".join(t.get("class", []))
        if re.search(r"(^|\s)ir-", cls) or (t.get("id") or "").startswith("ir-"):
            P.error(t, f"class/id starting with ir- is used by the addon: <{t.name}>")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--lang", required=True)
    ap.add_argument("--pages")
    ap.add_argument("--allow-math-errors", action="store_true")
    a = ap.parse_args()
    work = Path(a.work)
    m = C.load_manifest(work)
    pages = C.parse_pages(a.pages, m["npages"])
    missing = [n for n in pages if not C.work_path(work, "frag", C.pname(n) + ".html").exists()]
    if missing:
        raise SystemExit(f"no fragment for page(s) {','.join(map(str, missing))} (WORK/frag/pNNNN.html)")

    P = Problems()
    doc, article = assemble(work, pages, P)
    article["lang"] = a.lang
    nmath = convert_math(work, article, P, a.allow_math_errors)
    nimg, img_bytes = embed_images(work, article, P)
    reshape(article, P)
    tidy(article)
    check_structure(article, P)
    for t in article.find_all(attrs={"data-pg": True}):
        del t["data-pg"]

    out = str(article) + "\n"
    for w in P.warnings:
        print("warning:", w)
    for e in P.errors:
        print("ERROR:", e)
    if P.errors:
        raise SystemExit(f"{len(P.errors)} error(s); fix the fragments and assemble again (nothing written)")
    Path(a.out).write_text(out, encoding="utf-8")
    ncards = len(article.find_all(CARD_TAGS, recursive=False))
    print(f"wrote {a.out}: {len(pages)} pages, {len(out.encode()) // 1024} KB, {nmath} formulas, "
          f"{nimg} images ({img_bytes // 1024} KB), about {ncards} card candidates (check.py counts exactly)")


if __name__ == "__main__":
    main()
