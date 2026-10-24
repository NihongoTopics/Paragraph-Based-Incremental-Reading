#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出力の HTML を、アドオンの取り込みの規則（写し）に通して点検する。

使い方: check.py OUT.html [--work WORK] [--render] [--max-segments 40]

- アドオンの取り込みの規則の写し（ir_rules.py の prepare_article。取り込みダイアログと取り込みが使う関数と同じもの）で読み、
  カード（文書の直下の <div data-ir>）を数える。取り込みで形が変わるもの（段落の中のブロック、p の中の見出し）を知らせる。
  取り込みダイアログは HTML のファイルを先に Readability に通すが、写しには Readability は無い。出力が記事の本文だけなら
  Readability はそれを残し、題の h1 を外して、題（と著者の行）を本文の前に置き直す（2026-10-16 に確かめた）。
- 取り込みで外れる属性と要素（Readability が外す style、class、表示用の属性と、取り込みの規則で減る要素）を誤りとして知らせる。
  スキルは取り込みで残らないものを出力に書かない。
- カードの一覧を WORK/check/cards.txt（--work が無ければ OUT の隣の OUT.cards.txt）に書く。
  各行: 段落の ID、ページ、文字数、先頭の文字。
- 警告: 見出しなのにカードになったもの（draft.py が見出しと見なした文か、番号などで始まる短い文）、短すぎるカード、数字だけのカード、同じ文のカード、前の段落の続きらしいカード（前が文の途中で終わり、
  小文字で始まる）、ハイフンで終わるカード、残った印（<tex>, merror, ソフトハイフン, data-pdf-cont）、
  言語と向きの属性の不足（右から左の文字に dir="rtl" が無い、かなに lang="ja" が無い、など）、
  どのカードにも入らない文字（見出し、キャプション、表、pre、blockquote、数式の外）。
- --work があれば、テキストレイヤのあるデジタルのページごとに、テキストレイヤの語のうち出力に入った割合を出す。
- 数式、表、図、右から左の段落があれば、見た目の確認が要ると知らせる（無ければ要らないと知らせる）。
- 取り込みダイアログのプレビューと同じページ（ir_rules.preview_html）を WORK/check/preview.html に書く。
  --render で、それを Chromium（playwright。setup.py --render で用意する）で描いた画像 WORK/check/preview-NN.png にする。
終了コード: 誤りがあれば 1（警告だけなら 0）。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import json
import re
from collections import Counter
from pathlib import Path

C.need("bs4", "beautifulsoup4")
from bs4 import BeautifulSoup, Comment, NavigableString, Tag  # noqa: E402

import compare  # noqa: E402
import ir_rules as rules  # noqa: E402  アドオンの取り込みの規則の写し

MARKER = "data-pdf-page"
_RTL = re.compile("[֐-ࣿיִ-﷿ﹰ-ﻼ]")
_KANA = re.compile("[぀-ヿ]")
_HANGUL = re.compile("[가-힯]")
_HAN = re.compile("[㐀-鿿]")
_LATINISH = re.compile("[A-Za-zÀ-ɏͰ-ϿЀ-ӿ]")
SENTENCE_END = re.compile(r"[.!?:;…。！？」』)\]\"'”’]\s*$")
# 見出しの書き出しらしい形（番号、章、節など）。カードになった見出しを探すのに使う
HEADING_START = re.compile(r"^(\d+(\.\d+)*\.?\s|[IVXLC]+\.\s|(Chapter|Section|Part|Appendix)\b|第.{1,4}[章節部編]|§)")
# カードにならなくてよい要素（アドオンの取り込みでカードにならない、と markup.md が書いているもの）
NON_CARD_OK = ("h1", "h2", "h3", "h4", "h5", "h6", "figcaption", "caption", "td", "th", "math", "pre", "blockquote")


def text_of(el) -> str:
    return compare.plain_text(el)


def inherited(el, attr: str):
    while el is not None and isinstance(el, Tag):
        if el.has_attr(attr):
            return el[attr]
        el = el.parent
    return None


def is_cjk_text(t: str) -> bool:
    return sum(C.is_no_space_char(ch) for ch in t) > len(t) / 3


def lost_on_import(raw, soup, units) -> list:
    """取り込みで外れる属性（名前ごとの数）と、減る要素（名前ごとの数）の説明。

    要素の名前が替わる場合（中にブロックの無い div が p になるなど）も、元の名前の数が減るので知らせる。
    取り込みが足すもの（カードの div と data-ir、ブロックの間の文字を入れる p）は数えない。
    """
    def counts(root):
        a, e = Counter(), Counter()
        for t in root.find_all(True):
            e[t.name] += 1
            a.update(list(t.attrs))
        return a, e
    (a0, e0), (a1, e1) = counts(raw), counts(soup)
    e1["div"] -= len(units)
    out = [f"{n - a1[k]} {k}= attribute(s) are removed on import" for k, n in sorted(a0.items()) if n > a1[k]]
    out += [f"{n - e1[k]} <{k}> element(s) are removed or changed on import" for k, n in sorted(e0.items()) if n > e1[k]]
    # Readability が外す属性（写しには Readability が無いので、ここで数える）
    strip = Counter()
    for t in raw.find_all(True):
        if t.name == "svg" or t.find_parent("svg"):
            continue
        strip.update(a for a in t.attrs if a in C.READABILITY_STRIPS
                     or a in ("width", "height") and t.name in C.READABILITY_SIZE_ELEMS)
    out += [f"{n} {k}= attribute(s) are removed by Readability on import" for k, n in sorted(strip.items())]
    return out


def block_name(u) -> str:
    """カード（<div data-ir>）の中の最初のブロックの名前（p, ul など）。"""
    first = next((c for c in u.children if isinstance(c, Tag)), None)
    return first.name if first is not None else ""


def card_pages(soup, units) -> dict:
    """{段落の要素の id(): (始まりのページ, 終わりのページ)}。段落の文字が、どのdata-pdf-page の spanのあとにあるかで決める。"""
    owner = {}
    for u in units:
        for t in u.find_all(string=True):
            owner[id(t)] = id(u)
    out, page = {}, None
    for node in soup.descendants:
        if isinstance(node, Tag) and node.has_attr(MARKER):
            page = int(node[MARKER])
        elif isinstance(node, NavigableString) and not isinstance(node, Comment) and node.strip():
            u = owner.get(id(node))
            if u is not None:
                out[u] = (out[u][0], page) if u in out else (page, page)
    return out


def page_texts(soup) -> dict:
    """ページごとの本文の文字（data-pdf-page の spanで区切る）。"""
    texts, page = {}, None
    for piece in compare.text_pieces(soup, {"title"}):
        if isinstance(piece, Tag):
            page = int(piece[MARKER])
            continue
        texts.setdefault(page, []).append(piece)
    return {p: "".join(v) for p, v in texts.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="the assembled HTML")
    ap.add_argument("--work")
    ap.add_argument("--render", action="store_true", help="render the preview to PNG images (needs --work)")
    ap.add_argument("--max-segments", type=int, default=40)
    a = ap.parse_args()
    out = Path(a.out)
    work = Path(a.work) if a.work else None
    errors, warnings = [], []

    html = out.read_text(encoding="utf-8")   # assemble.py は UTF-8 で書く
    raw = BeautifulSoup(html, "html.parser")
    # 取り込みで形が変わるもの（prepare_article の前の形で調べる）
    for p in raw.find_all("p"):
        for b in p.find_all(list(rules.BLOCK_TAGS)):
            errors.append(f"<{b.name}> inside <p> closes the <p> on import (what follows becomes another block or "
                          f"card): {text_of(p)[:60]!r}")
    try:
        soup, units = rules.prepare_article(html)
    except ValueError as e:
        raise SystemExit(f"the addon would refuse this file: {e}")
    errors += lost_on_import(raw, soup, units)

    pages = card_pages(soup, units)
    lines, texts = [], [text_of(u) for u in units]
    for u, t in zip(units, texts):
        a_, b_ = pages.get(id(u), (None, None))
        pg = f"{a_}" if a_ == b_ else f"{a_}-{b_}"
        lines.append(f"{u['data-ir']:>6}  p.{pg:<7} {len(t):>5}  {t[:80]}")
    cards_file = C.work_path(work, "check", "cards.txt") if work else out.with_suffix(".cards.txt")
    cards_file.parent.mkdir(parents=True, exist_ok=True)
    cards_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # draft.py が見出しと見なした文（カードになっていれば、見出しの要素にし忘れている）
    drafted = {}
    if work and C.work_path(work, "draft", "headings.json").exists():
        with open(C.work_path(work, "draft", "headings.json"), encoding="utf-8") as f:
            drafted = {C.norm_text(h["text"]): h["tag"] for h in json.load(f)}

    # カードごとの警告
    counts = Counter(t for t in texts if len(t) < 80)
    for i, (u, t) in enumerate(zip(units, texts)):
        where = f"{u['data-ir']} (p.{pages.get(id(u), (None,))[0]})"
        short = len(t) < (6 if is_cjk_text(t) else 20)
        if re.fullmatch(r"[\divxlcIVXLC.\-–\s]+", t):
            warnings.append(f"{where}: card of only numbers (page number?): {t!r}")
        elif short and not u.find(["math", "img"]):
            warnings.append(f"{where}: very short card: {t!r}")
        if counts.get(t, 0) >= 2 and len(t) < 80:
            warnings.append(f"{where}: the same text is in {counts[t]} cards (running head?): {t!r}")
        tag = drafted.get(C.norm_text(t))
        if tag:
            warnings.append(f"{where}: a heading became a card (the draft found it as <{tag}>; headings are not "
                            f"cards in the addon, so write it as <{tag}>): {t[:60]!r}")
        elif len(t) <= 80 and HEADING_START.match(t) and not re.search(r"[.。!?！？]\s*$", t):
            warnings.append(f"{where}: looks like a heading but is a card (if it is a heading, write it as <h2>-<h6>): "
                            f"{t[:60]!r}")
        if t.endswith(("-", "‐")):
            warnings.append(f"{where}: card ends with a hyphen: ...{t[-40:]!r}")
        if (i + 1 < len(units) and block_name(u) == block_name(units[i + 1]) == "p"
                and _LATINISH.search(t[-20:] or "")):
            nxt = texts[i + 1]
            if t and not SENTENCE_END.search(t) and re.match(r"^[a-zа-яα-ω]", nxt):
                warnings.append(f"{where}: ends mid-sentence and the next card starts in lower case "
                                f"(missed continuation?): ...{t[-30:]!r} | {nxt[:30]!r}")
        letters = re.sub(r"\W", "", t)
        blk = next((c for c in u.children if isinstance(c, Tag)), u)   # lang と dir はカードの div の中のブロックに付く
        lang = (inherited(blk, "lang") or "").lower()
        if letters and len(_RTL.findall(t)) > len(letters) / 2 and (inherited(blk, "dir") or "").lower() != "rtl":
            warnings.append(f"{where}: right-to-left text without dir=\"rtl\": {t[:40]!r}")
        if _KANA.search(t) and len(_KANA.findall(t)) > len(letters) / 5 and not lang.startswith("ja"):
            warnings.append(f"{where}: Japanese text without lang=\"ja\" (lang is {lang or 'unset'}): {t[:30]!r}")
        if len(_HANGUL.findall(t)) > len(letters) / 3 and not lang.startswith("ko"):
            warnings.append(f"{where}: Korean text without lang=\"ko\": {t[:30]!r}")
        if (len(_HAN.findall(t)) > len(letters) / 3 and not _KANA.search(t)
                and not lang.startswith(("ja", "zh"))):
            warnings.append(f"{where}: Han text whose lang is {lang or 'unset'} (ja or zh-Hans/zh-Hant?): {t[:30]!r}")

    # 残った印と画像
    if soup.find("tex"):
        errors.append(f"{len(soup.find_all('tex'))} <tex> left (assemble.py converts them)")
    if soup.find("merror"):
        errors.append(f"{len(soup.find_all('merror'))} formula(s) with a TeX error (merror)")
    if soup.find(attrs={"data-pdf-cont": True}):
        errors.append("data-pdf-cont left in the output")
    if "­" in soup.get_text():
        warnings.append("soft hyphens (U+00AD) left in the text")
    img_bytes = 0
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src.startswith("data:"):
            errors.append(f"image not embedded (the import does not copy files): src={src[:60]!r}")
        elif not rules.DATA_URI_RE.match(src.strip()):
            errors.append("image data URI is not base64 (the import does not extract it)")
        else:
            n = len(src) * 3 // 4
            img_bytes += n
            if n > 1_000_000:
                warnings.append(f"large image ({n // 1024} KB): alt={img.get('alt', '')[:40]!r}")

    # どのカードにも入らない文字
    in_card = set()
    for u in units:
        in_card.update(id(t) for t in u.find_all(string=True))
    loose = Counter()
    for t in soup.find_all(string=True):
        if isinstance(t, Comment) or not t.strip() or id(t) in in_card:
            continue
        parent = t.parent
        if parent.find_parent(NON_CARD_OK) or parent.name in NON_CARD_OK:
            continue
        box = parent
        loose[(box.name, text_of(box)[:60])] += 1
    for (name, t), _ in loose.items():
        warnings.append(f"text in no card, inside <{name}>: {t!r}")

    # テキストレイヤとの突き合わせ
    coverage = []
    if work:
        m = C.load_manifest(work)
        digital = [p["n"] for p in m["pages"] if p["kind"] == "digital"]
        all_lines = {n: compare.load_lines(work, "text", n) for n in digital}
        all_lines = {n: v for n, v in all_lines.items() if v}
        furn = compare.furniture(all_lines)
        by_page = page_texts(soup)
        for n, lines_n in sorted(all_lines.items()):
            outtok = C.tokens(by_page.get(n, ""))
            kept = [l for l in lines_n if (n, l["id"]) not in furn]
            ref, owner = compare.line_tokens(kept, set(outtok))
            if not ref:
                continue
            r2o, _ = compare.align(ref, outtok)
            cov = sum(o >= 0 for o in r2o) / len(ref)
            coverage.append((n, cov))
            if cov < 0.9:
                missing = Counter(owner[i] for i, o in enumerate(r2o) if o < 0)
                worst = [kept[i]["text"][:70] for i, c in missing.most_common(5)]
                warnings.append(f"p.{n}: only {cov:.0%} of the text-layer words are in the output; "
                                f"e.g. {worst}")

    # プレビュー
    preview = C.work_path(work, "check", "preview.html") if work else out.with_suffix(".preview.html")
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_text(rules.preview_html(soup), encoding="utf-8")

    per_page = Counter(pages.get(id(u), (None,))[0] for u in units)
    print(f"{out}: {len(units)} cards; {len(soup.find_all('img'))} images ({img_bytes // 1024} KB); "
          f"{len(soup.find_all('math'))} formulas")
    print("cards per page: " + ", ".join(f"p.{p}: {c}" for p, c in sorted(per_page.items(), key=lambda x: x[0] or 0)))
    if coverage:
        print("text-layer coverage: " + ", ".join(f"p.{n} {c:.0%}" for n, c in coverage))
    print(f"card list: {cards_file}")
    worth = [(len(soup.find_all("math")), "formula(s)"), (len(soup.find_all("table")), "table(s)"),
             (len(soup.find_all("figure")), "figure(s)"),
             (len(soup.find_all(attrs={"dir": "rtl"})), "right-to-left block(s)")]
    worth = [f"{n} {what}" for n, what in worth if n]
    print("visual check: " + ("worth doing for " + ", ".join(worth) + " (--render, then look at the images)"
                              if worth else "not needed (text only; skip --render)"))
    print(f"preview (as in the import dialog): {preview}")
    for w in warnings:
        print("warning:", w)
    for e in errors:
        print("ERROR:", e)
    sys.stdout.flush()
    if a.render:
        if not work:
            raise SystemExit("--render needs --work")
        for p in render(preview, C.work_path(work, "check"), a.max_segments):
            print("rendered:", p)
    if errors:
        sys.exit(1)


def render(page: Path, outdir: Path, max_segments: int) -> list:
    """プレビューを Chromium（playwright）で描き、幅 900 px、高さ 2000 px ずつの画像にする。
    ブラウザは setup.py --render が見つけたもの（CACHE/env.json の chrome）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("note: no renderer (playwright): run setup.py --render, then use its PY; skipped the images")
        return []
    chrome = C.env().get("chrome")
    if not chrome:
        print("note: no browser for rendering: run setup.py --render; skipped the images")
        return []
    for old in outdir.glob("preview-*.png"):
        old.unlink()
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch() if chrome == "bundled" else p.chromium.launch(executable_path=chrome)
        view = browser.new_page(viewport={"width": 900, "height": 2000})
        view.goto(page.resolve().as_uri())
        view.wait_for_timeout(500)
        height = int(view.evaluate("Math.ceil(document.documentElement.scrollHeight)"))
        y, i = 0, 1
        while y < height and i <= max_segments:
            p_ = outdir / f"preview-{i:02d}.png"
            view.screenshot(path=str(p_), full_page=True,
                            clip={"x": 0, "y": y, "width": 900, "height": min(2000, height - y)})
            paths.append(p_)
            y += 2000
            i += 1
        browser.close()
    if y < height:
        print(f"note: rendered the first {max_segments} segments only ({y} of {height} px); "
              f"raise --max-segments to see the rest")
    return paths


if __name__ == "__main__":
    main()
