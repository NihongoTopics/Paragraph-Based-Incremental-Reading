#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ページの下書き（WORK/draft/pNNNN.html）を、テキストレイヤか OCR の行から作る。

使い方: draft.py WORK [--pages SPEC] [--source auto|text|ocr] [--frag]

  --source auto（既定）: ocr.py の結果があればそれ、無ければテキストレイヤ。どちらも無いページは飛ばして知らせる。
  --frag  下書きを清書（WORK/frag/pNNNN.html）としても書く（その清書がまだ無いページだけ）。文字の正しい
          デジタルのページを、打ち直さずに直すための出発点。下書きの印（コメント、data-box）は組み立てで消える。

下書きは Claude が清書（frag/）を書くための材料で、そのまま使うものではない。
- 行を読む順に並べる。テキストレイヤでは prepare.py が推した段組（manifest の columns）で、段をまたぐ行で
  ページを上下に区切り、その間を左の段、右の段の順に読む。同じ高さの行（文の中の別の字体の語など）は左から右へつなぐ。
  OCR では tesseract の順（段は tesseract が分ける）。
- 見出しは <h1>〜<h6> にする（アドオンでは見出しはカードにならず、<p> はカードになる）。テキストレイヤでは
  字の大きさと太字（prepare.py が pdftohtml から付ける）、OCR では行の高さで見分ける（find_headings）。
  見つけた見出しは WORK/draft/headings.json にも書く（check.py が、カードになった見出しを探すのに使う）。
- 行を段落にまとめる。テキストレイヤでは、区画（block）の変わり目、前の行が短い、行頭の字下げ、字の大きさか太字の変わり目で段落を分ける。
  OCR では tesseract の段落（par）。各段落は <p data-box="x0,y0,x1,y1">（ページに対する割合）。
- 行末のハイフン: ソフトハイフンで折れた所は取ってつなぐ。"-" と U+2010 は、つないだ語が文書のほかの箇所に
  ハイフン無しで出ていれば取り、ハイフン付きで出ていれば残す。どちらでもなければ残して <!--hy?--> を付ける。
- 印（Claude が確かめる所）:
  <!-- furniture: … -->  柱かページ番号らしい行（ページの上下の帯にあり、数字だけか、3 ページ以上の同じ位置に出る）
  <!--math?-->           数学用の文字（𝑥 など）を含む段落
  <!--lang? xx (SCRIPT)--> ページの主な用字と違う用字の段落（xx は推した言語）
  <!--cont?-->           前の段落の続きかもしれない段落（前が文の途中で終わり、小文字で始まる）
  dir="rtl"              右から左の文字が多い段落
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import html
import json
import re
from pathlib import Path

import compare   # furniture() と load_lines() を共有する

SHY = "­"
INDENT = 0.008       # 行頭がこれ（ページの幅に対する割合）以上右にあれば字下げ
SHORT = 0.05         # 行末が段の右端よりこれ以上左で終われば短い行
OVERLAP = 0.02       # 横に並ぶ行と見なす、左右の重なりの上限
_RTL = re.compile("[֐-ࣿיִ-﷿ﹰ-ﻼ]")
_MATH = re.compile("[\U0001D400-\U0001D7FF∀-⋿]")
_HYPHEN_END = re.compile(r"(\w+)[-‐]$")
_WORD_START = re.compile(r"^(\w+)")

LANG_OF_SCRIPT = {"ARABIC": "ar", "HEBREW": "he", "HIRAGANA": "ja", "KATAKANA": "ja", "CJK": "ja or zh",
                  "HANGUL": "ko", "GREEK": "el", "CYRILLIC": "ru?", "DEVANAGARI": "hi", "THAI": "th",
                  "LATIN": "en?"}


# ---------------------------------------------------------------- 並べる

def rows(lines: list) -> list:
    """同じ高さの行を 1 行にまとめる（左から右の順。右から左の行は右から左）。"""
    out = []
    for line in sorted(lines, key=lambda l: (l["y0"] + l["y1"]) / 2):
        yc = (line["y0"] + line["y1"]) / 2
        if out:
            r = out[-1]
            # 横に並ぶ（左右が重ならない）同じ高さの行だけをつなぐ（文の中の別の字体の語、pdftotext が
            # 1 行を 2 つに分けたもの）。上下の行は、箱の高さが行の間隔より大きくても（日本語など）つながない
            side_by_side = all(line["x0"] >= p["x1"] - OVERLAP or line["x1"] <= p["x0"] + OVERLAP
                               for p in r["parts"])
            if side_by_side and abs(yc - r["yc"]) < 0.5 * min(r["y1"] - r["y0"], line["y1"] - line["y0"]):
                r["parts"].append(line)
                r.update(x0=min(r["x0"], line["x0"]), x1=max(r["x1"], line["x1"]),
                         y0=min(r["y0"], line["y0"]), y1=max(r["y1"], line["y1"]))
                continue
        out.append({"yc": yc, "parts": [line], "x0": line["x0"], "x1": line["x1"],
                    "y0": line["y0"], "y1": line["y1"]})
    for r in out:
        rtl = r["rtl"] = sum(p.get("rtl", False) for p in r["parts"]) > len(r["parts"]) / 2
        r["parts"].sort(key=lambda p: -p["x0"] if rtl else p["x0"])
        r["text"] = " ".join(p["text"] for p in r["parts"])
        r["block"] = r["parts"][0].get("block")
        r["ids"] = [p["id"] for p in r["parts"]]
        sized = [p for p in r["parts"] if p.get("size")]
        if sized:   # prepare.py が付けた字の大きさと太字（字数で重み付け）
            n = sum(len(p["text"]) for p in sized) or 1
            r["size"] = sum(p["size"] * len(p["text"]) for p in sized) / n
            r["bold"] = all(p.get("bold") for p in sized)
    return out


def order_text_lines(lines: list, gutters: list) -> list:
    """テキストレイヤの行を読む順の「行」（rows）の列にする。戻り値は [(段, row)]。段は 0 が段をまたぐ行。"""
    if not gutters:
        return [(1, r) for r in rows(lines)]
    g = gutters[0]
    zone = lambda l: 0 if l["x0"] < g < l["x1"] else (1 if l["x1"] <= g else 2)
    ordered, band = [], {1: [], 2: []}

    def flush():
        for z in (1, 2):
            ordered.extend((z, r) for r in rows(band[z]))
            band[z] = []
    for line in sorted(lines, key=lambda l: l["y0"]):
        z = zone(line)
        if z == 0:
            flush()
            ordered.append((0, rows([line])[0]))
        else:
            band[z].append(line)
    flush()
    return ordered


# ---------------------------------------------------------------- 段落

def paragraphs_text(ordered: list) -> list:
    """テキストレイヤの行（読む順）を段落に分ける。戻り値は行のリストのリスト。"""
    zone_left, zone_right = {}, {}
    for z, r in ordered:
        zone_left[z] = min(zone_left.get(z, 1), r["x0"])
        zone_right[z] = max(zone_right.get(z, 0), r["x1"])
    paras, prev = [], None
    for z, r in ordered:
        new = prev is None or z != prev[0]
        if not new:
            pz, pr = prev
            if r.get("rtl"):   # 右から左の行は、行頭が右、行末が左
                indented = r["x1"] < zone_right[z] - INDENT and pr["x1"] >= zone_right[z] - INDENT
                short = pr["x0"] > zone_left[z] + SHORT
            else:
                indented = r["x0"] > zone_left[z] + INDENT and pr["x0"] <= zone_left[z] + INDENT
                short = pr["x1"] < zone_right[z] - SHORT
            gap = r["y0"] - pr["y1"] > 0.9 * (pr["y1"] - pr["y0"])
            turn = bool(r.get("rtl")) != bool(pr.get("rtl"))   # 書字の向きが変わる
            # 字の大きさか太字が変わる（見出しと本文を同じ段落にしない）
            font = (r.get("size") is not None and pr.get("size") is not None
                    and (abs(r["size"] - pr["size"]) > 0.6 or r.get("bold") != pr.get("bold")))
            new = indented or short or gap or turn or font or (r["block"] != pr["block"] and z == 0)
        if new:
            paras.append([])
        paras[-1].append(r)
        prev = (z, r)
    return paras


def paragraphs_ocr(lines: list) -> list:
    paras, key = [], None
    for line in lines:
        k = (line["block"], line.get("par"))
        if k != key:
            paras.append([])
            key = k
        paras[-1].append({**line, "ids": [line["id"]]})
    return paras


# ---------------------------------------------------------------- 行をつなぐ

def vocabulary(all_lines: dict) -> tuple:
    """文書の語の集合（ハイフン無しの語、ハイフン付きの語）。行末のハイフンの判断に使う。"""
    plain, hyph = set(), set()
    for lines in all_lines.values():
        for line in lines:
            for w in re.findall(r"\w+(?:[-‐]\w+)+|\w+", line["text"].replace(SHY, "")):
                w = w.lower().replace("‐", "-")
                (hyph if "-" in w else plain).add(w)
    return plain, hyph


def join_lines(texts: list, plain: set, hyph: set) -> str:
    out = ""
    for t in texts:
        t = t.strip()
        if not out:
            out = t
            continue
        if out.endswith(SHY):
            out = out[:-1] + t
            continue
        m, n = _HYPHEN_END.search(out), _WORD_START.match(t)
        if m and n:
            a, b = m.group(1), n.group(1)
            joined, hyphened = (a + b).lower(), (a + "-" + b).lower()
            if hyphened in hyph:
                out = out[:-1] + "-" + t
            elif joined in plain:
                out = out[:-1] + t
            else:
                out = out[:-1] + "-<!--hy?-->" + t
            continue
        if C.is_no_space_char(out[-1]) and C.is_no_space_char(t[0]):
            out += t
        else:
            out += " " + t
    return out.replace(SHY, "")


def script_of_para(text: str):
    """段落の用字。かながあれば HIRAGANA（日本語）。無ければいちばん多い用字。"""
    counts = C.script_counts(text)
    if not counts:
        return None
    if counts.get("HIRAGANA") or counts.get("KATAKANA"):
        return "HIRAGANA"
    return max(counts, key=counts.get)


def page_script(text: str):
    """ページの主な用字（いちばん多い用字。かなと漢字はまとめて CJK と数える）。"""
    counts = C.script_counts(text)
    for k in ("HIRAGANA", "KATAKANA"):
        counts["CJK"] = counts.get("CJK", 0) + counts.pop(k, 0)
    return max(counts, key=counts.get) if counts else None


def same_script(para: str, page: str) -> bool:
    group = lambda s: "CJK" if s in ("HIRAGANA", "KATAKANA", "CJK") else s
    return group(para) == group(page)


def build_paras(page: dict, source: str, lines: list, furn: set) -> list:
    n = page["n"]
    kept = [l for l in lines if (n, l["id"]) not in furn]
    if source == "text":
        return paragraphs_text(order_text_lines(kept, page["columns"]["gutters"]))
    return paragraphs_ocr(kept)


SENTENCE_END = re.compile(r"[.。!?！？,;、，；]\s*$")


def _weighted_mode(pairs) -> float:
    """[(値, 重み)] で重みの合計がいちばん大きい値（0.5 刻みに丸める）。"""
    acc = {}
    for v, w in pairs:
        k = round(v * 2) / 2
        acc[k] = acc.get(k, 0) + w
    return max(acc, key=acc.get) if acc else 0.0


def find_headings(paras_by_page: dict, source_by_page: dict) -> dict:
    """見出しと見なす段落と、その見出しの段（1〜6）。戻り値 {(ページ, 段落の番号): 段}。

    テキストレイヤ: 本文の字の大きさ（字数でいちばん多い大きさ）より 1pt（か 1 割）以上大きいか、全部が太字で
    本文の 0.85 倍以上の大きさの段落。OCR: 行の高さが本文の行の 1.3 倍以上の段落（太字は分からない）。
    どちらも、160 字以下、3 行以下で、文の終わりの記号（。. , など）で終わらないものに限る。
    段は大きさの順。いちばん大きい大きさの見出しが文書に 1 つだけなら、それを h1（題）にし、ほかを h2 から振る。
    """
    text_rows = [r for n, paras in paras_by_page.items() if source_by_page[n] == "text"
                 for para in paras for r in para if r.get("size")]
    body = _weighted_mode((r["size"], len(r["text"])) for r in text_rows)
    ocr_rows = [r for n, paras in paras_by_page.items() if source_by_page[n] == "ocr" for para in paras for r in para]
    heights = sorted(r["y1"] - r["y0"] for r in ocr_rows)
    body_h = heights[len(heights) // 2] if heights else 0
    cand = {}
    for n, paras in paras_by_page.items():
        for i, para in enumerate(paras):
            text = " ".join(r["text"] for r in para).strip()
            if (not text or len(text) > 160 or len(para) > 3 or SENTENCE_END.search(text) or text[0].islower()
                    or _MATH.search(text)):   # 数式の行は数式の字体で大きく見えるだけ
                continue
            if source_by_page[n] == "text":
                if not all(r.get("size") for r in para) or not body:
                    continue
                size = sum(r["size"] * len(r["text"]) for r in para) / max(1, sum(len(r["text"]) for r in para))
                bold = all(r.get("bold") for r in para)
                if size >= body + max(1.0, 0.1 * body) or (bold and size >= 0.85 * body):
                    cand[(n, i)] = (round(size * 2) / 2, bold)
            elif body_h and len(para) <= 2:
                h = sum(r["y1"] - r["y0"] for r in para) / len(para)
                if h >= 1.3 * body_h:
                    cand[(n, i)] = (round(h / body_h * 10) / 10, False)
    keys = sorted(set(cand.values()), key=lambda k: (-k[0], not k[1]))
    first = 1 if keys and list(cand.values()).count(keys[0]) == 1 else 2
    level = {k: min(6, first + j) for j, k in enumerate(keys)}
    return {pos: level[k] for pos, k in cand.items()}


def render_page(page: dict, source: str, lines: list, paras: list, headings: dict, furn: set,
                plain: set, hyph: set) -> str:
    n = page["n"]
    kept = [l for l in lines if (n, l["id"]) not in furn]
    main = page_script(" ".join(l["text"] for l in kept))
    out = [f"<!-- draft of page {n} from {'the text layer' if source == 'text' else 'OCR (ocr.py)'}; "
           f"kind {page['kind']}; columns {page['columns']['count']} -->"]
    if page.get("rot"):
        out.append(f"<!-- the page is rotated {page['rot']} degrees: check the text order against the image "
                   f"(poppler may reverse CJK text on rotated pages) -->")
    for l in lines:
        if (n, l["id"]) in furn:
            out.append(f"<!-- furniture: {l['text'].replace('--', '- -')} -->")
    prev_text = None
    for i, para in enumerate(paras):
        text = join_lines([r["text"] for r in para], plain, hyph)
        box = ",".join(f"{v:.3f}" for v in (min(r["x0"] for r in para), min(r["y0"] for r in para),
                                             max(r["x1"] for r in para), max(r["y1"] for r in para)))
        marks = []
        if _MATH.search(text):
            marks.append("<!--math?-->")
        script = script_of_para(text)
        if script and main and not same_script(script, main):
            marks.append(f"<!--lang? {LANG_OF_SCRIPT.get(script, '?')} ({script})-->")
        tag = f"h{headings[(n, i)]}" if (n, i) in headings else "p"
        if tag == "p" and prev_text and not re.search(r"[.!?:;。！？」』)\]]\s*$", prev_text) and re.match(r"^[a-z]", text):
            marks.append("<!--cont?-->")
        rtl = len(_RTL.findall(text)) > len(re.findall(r"\w", text)) / 2
        safe = html.escape(text, quote=False).replace("-&lt;!--hy?--&gt;", "-<!--hy?-->")
        attrs = f'data-box="{box}"' + (' dir="rtl"' if rtl else "")
        out.append(f'<{tag} {attrs}>{"".join(marks)}{safe}</{tag}>')
        prev_text = None if tag != "p" else text
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("--pages")
    ap.add_argument("--source", choices=["auto", "text", "ocr"], default="auto")
    ap.add_argument("--frag", action="store_true", help="also write the draft as WORK/frag/pNNNN.html if that "
                    "fragment does not exist yet")
    a = ap.parse_args()
    work = Path(a.work)
    m = C.load_manifest(work)
    pages = {p["n"]: p for p in m["pages"]}
    nums = [n for n in C.parse_pages(a.pages, m["npages"]) if n in pages]

    def source_of(n):
        have = [s for s in ("ocr", "text") if C.work_path(work, s, C.pname(n) + ".lines.json").exists()]
        if a.source != "auto":
            return a.source if a.source in have else None
        return have[0] if have else None

    sources = {n: source_of(n) for n in pages}
    all_lines = {}
    for n, s in sources.items():
        if s:
            all_lines[n] = compare.load_lines(work, s, n)
    furn = compare.furniture(all_lines)
    plain, hyph = vocabulary(all_lines)
    C.work_path(work, "draft").mkdir(exist_ok=True)
    # 見出しの段と本文の字の大きさは文書全体で決める（下書きを作らないページも数に入れる）
    paras = {n: build_paras(pages[n], s, all_lines[n], furn) for n, s in sources.items() if s}
    headings = find_headings(paras, sources)
    with open(C.work_path(work, "draft", "headings.json"), "w", encoding="utf-8") as f:
        json.dump([{"page": n, "text": join_lines([r["text"] for r in paras[n][i]], plain, hyph).replace("-<!--hy?-->", "-"),
                    "tag": f"h{lv}"} for (n, i), lv in sorted(headings.items())], f, ensure_ascii=False, indent=0)
    skipped = []
    for n in nums:
        s = sources[n]
        if not s:
            skipped.append(n)
            continue
        text = render_page(pages[n], s, all_lines[n], paras[n], headings, furn, plain, hyph)
        C.work_path(work, "draft", C.pname(n) + ".html").write_text(text, encoding="utf-8")
        frag_note = ""
        if a.frag:
            frag = C.work_path(work, "frag", C.pname(n) + ".html")
            frag.parent.mkdir(exist_ok=True)
            if frag.exists():
                frag_note = "; frag/ exists, left as is"
            else:
                frag.write_text(text, encoding="utf-8")
                frag_note = "; also written to frag/"
        print(f"{C.pname(n)}: {s:4} {text.count('<p ')} paragraphs, {len(re.findall(r'<h[1-6] ', text))} headings, "
              f"{text.count('<!-- furniture')} furniture, {text.count('<!--hy?-->')} hy?, "
              f"{text.count('<!--cont?-->')} cont?{frag_note}")
    if skipped:
        print(f"no text layer and no OCR for page(s) {','.join(map(str, skipped))}: run ocr.py first")


if __name__ == "__main__":
    main()
