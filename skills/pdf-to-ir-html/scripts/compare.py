#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ページの清書（WORK/frag/pNNNN.html）を、参照の読み（テキスト層か OCR の lines.json）と語の単位で突き合わせる。

使い方: compare.py WORK PAGE [--ref auto|text|ocr] [--frag PATH]
  PAGE は 3、1-4,7、all のどれか。--frag は PAGE が 1 ページのときだけ使える。
清書の文字（タグを除く。<tex> の中とコメントは除く。図の説明、表、脚注は含む）と参照の文字を語の列にして
対応を取り、次を出す。参照の語のうち清書にある割合（coverage）。清書の語のうち参照にある割合（precision）。
語の 6 割未満しか清書に無い参照の行（見直す場所）。参照に無い清書の 3 語以上（空白で区切らない文字だけなら
4 字以上）の続き（読み違いか書き足しの候補）。項目にはページ上の範囲（box）を付け、拡大のコマンドは最初に
1 行だけ出す。各種類 --max 件まで出し、残りは WORK/compare/pNNNN.json に書く（全部の結果もここ）。
柱とノンブル（draft.py と同じ規則）と、regions.py が見つけた図の範囲の中の行は参照から除く。
check.py もこのファイルの関数（text_pieces, furniture, line_tokens, align）を使う。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import difflib
import json
import re
import shlex
from pathlib import Path

C.need("bs4", "beautifulsoup4")
from bs4 import BeautifulSoup, NavigableString, Tag  # noqa: E402
from bs4.element import PreformattedString  # noqa: E402  コメント、doctype など

WEAK_LINE = 0.6      # 語のこの割合未満しか見つからない参照の行を報告する
MIN_RUN = 3          # 参照に無い清書の語の続きを報告する長さ（語）
MIN_RUN_NO_SPACE = 4  # 同じく、空白で区切らない文字（漢字、かな、タイ文字など）だけの続き（字）
POOR_CONF = 60       # OCR の平均の確からしさがこれ未満なら、参照そのものが悪いと警告する
BAND = 0.07          # 柱とノンブルを探す、ページの上と下の帯（ページの高さに対する割合）
SAME_PLACE = 0.02    # 繰り返す行を同じ位置と見なす、行の中心の高さの差
REPEAT_PAGES = 3     # この数以上のページの同じ位置に同じ行があれば柱と見なす

# 文字を取り出すとき、前後に空白を入れる要素（ブロックと改行。行列の升目も語を分ける）
BLOCK_LEVEL = {
    "address", "article", "aside", "blockquote", "br", "caption", "dd", "details", "dialog", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
    "td", "tfoot", "th", "thead", "tr", "ul", "mtr", "mtd",
}
MARKER = "data-pdf-page"


# ---------------------------------------------------------------- HTML の文字

def text_pieces(node, skip=frozenset()):
    """node の中の文字を文書順に返す。ブロックの境目と <br> では " " を返す。

    インライン要素の境目では何も足さない（"photo<em>graph</em>" は 1 語）。skip の要素は中身ごと
    飛ばす。コメントは飛ばす。data-pdf-page の span（data-pdf-page を持つ要素）は、その要素そのものを返す。
    """
    for c in node.children:
        if isinstance(c, Tag):
            if c.name in skip:
                continue
            if c.has_attr(MARKER):
                yield c
                continue
            block = c.name in BLOCK_LEVEL
            if block:
                yield " "
            yield from text_pieces(c, skip)
            if block:
                yield " "
        elif isinstance(c, NavigableString) and not isinstance(c, PreformattedString):
            yield str(c)


def plain_text(node, skip=frozenset()) -> str:
    """node の文字。空白をまとめ、data-pdf-page の spanは無視する。"""
    return " ".join("".join(p for p in text_pieces(node, skip) if isinstance(p, str)).split())


def show_tokens(toks) -> str:
    """語の列を読める形にする。空白で区切らない文字どうしの間には空白を入れない。"""
    out = ""
    for t in toks:
        if out and not (C.is_no_space_char(out[-1]) and C.is_no_space_char(t[0])):
            out += " "
        out += t
    return out


# ---------------------------------------------------------------- 参照の行

def load_lines(work, source: str, n: int):
    """WORK/<source>/pNNNN.lines.json の行のリスト。ファイルが無ければ None。"""
    p = C.work_path(work, source, C.pname(n) + ".lines.json")
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)["lines"]


def box_of(line) -> list:
    return [line["x0"], line["y0"], line["x1"], line["y1"]]


def union(boxes) -> list:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


_DIGITS = re.compile(r"\d+")
_NO_LETTERS_SOME_DIGIT = re.compile(r"[\W\d_]*\d[\W\d_]*")


def furniture(lines_by_page: dict) -> set:
    """柱とノンブルと見なす行の (ページ, 行の id) の集合。draft.py の規則と同じ。

    対象はページの上と下の BAND の帯に中心がある行で、次のどちらかに当たるもの。
    - 文字が数字（と記号）だけ（ノンブル）
    - 数字を # に伏せて同じになる行が、REPEAT_PAGES 以上のページの、高さの差 SAME_PLACE 以内にある（柱）
    """
    found, candidates = set(), {}
    for n, lines in lines_by_page.items():
        for line in lines:
            yc = (line["y0"] + line["y1"]) / 2
            if BAND <= yc <= 1 - BAND:
                continue
            text = C.norm_text(line["text"])
            if not text:
                continue
            if _NO_LETTERS_SOME_DIGIT.fullmatch(text):
                found.add((n, line["id"]))
            else:
                candidates.setdefault(_DIGITS.sub("#", text), []).append((n, line["id"], yc))
    for same in candidates.values():
        for n, lid, yc in same:
            pages = {m for m, _, y in same if abs(y - yc) <= SAME_PLACE}
            if len(pages) >= REPEAT_PAGES:
                found.add((n, lid))
    return found


def line_tokens(lines, vocab) -> tuple:
    """行の文字を語の列にする。戻り値は (語のリスト, 各語が属する行の添字のリスト)。

    行末のハイフンやテキスト層の語の切れ目で分かれた語（"dic‐" と "tionary"、"tion­a" と "ry"）は、
    続く 2〜4 語をつなぐと vocab（突き合わせる相手の語の集合）にあるとき、つないで 1 語にする。
    つなぐのは、分かれた部分のどれかが vocab に無いときだけ（"in" "to" を "into" にはしない）。
    """
    toks, owner = [], []
    for i, line in enumerate(lines):
        for t in C.tokens(line["text"]):
            toks.append(t)
            owner.append(i)
    merged, merged_owner = [], []
    i = 0
    while i < len(toks):
        k = 1
        if toks[i] not in vocab or (i + 1 < len(toks) and toks[i + 1] not in vocab):
            k = next((k for k in (4, 3, 2)
                      if i + k <= len(toks) and "".join(toks[i:i + k]) in vocab), 1)
        merged.append("".join(toks[i:i + k]))
        merged_owner.append(owner[i])
        i += k
    return merged, merged_owner


# ---------------------------------------------------------------- 突き合わせ

def runs(flags):
    """flags が True の連続部分の (始め, 終わり) を順に返す。"""
    start = None
    for i, f in enumerate(flags):
        if f and start is None:
            start = i
        elif not f and start is not None:
            yield start, i
            start = None
    if start is not None:
        yield start, len(flags)


def align(ref: list, out: list) -> tuple:
    """二つの語の列の対応。戻り値は (ref の各語に対応する out の添字, out の各語に対応する ref の添字)。
    対応の無い語は -1。

    1 回目は difflib で順序を保った対応を取る。2 回目は、1 回目で対応の無い ref の連続部分を、
    対応の無い out の語の中から順序を問わず探す（段組みの読む順、図や脚注の置き場所の違いで、
    まとまりごと別の場所にある文を拾う）。2 回目に採るのは 3 語以上続く一致だけ（連続部分が
    3 語より短ければ、その全体が続いて見つかったときだけ）。
    """
    r2o, o2r = [-1] * len(ref), [-1] * len(out)
    sm = difflib.SequenceMatcher(None, ref, out, autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            r2o[a + k], o2r[b + k] = b + k, a + k
    free = [t if o2r[j] < 0 else None for j, t in enumerate(out)]
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(free)
    for start, end in list(runs([j < 0 for j in r2o])):
        sm.set_seq1(ref[start:end])
        need = min(3, end - start)
        for a, b, size in sm.get_matching_blocks():
            if size < need:
                continue
            for k in range(size):
                if o2r[b + k] < 0:  # 別の連続部分が先に使った語は使わない
                    r2o[start + a + k], o2r[b + k] = b + k, start + a + k
    return r2o, o2r


# ---------------------------------------------------------------- 1 ページの比較

SCRIPTS_DIR = Path(__file__).resolve().parent


def figure_boxes(work, n: int) -> list:
    """regions.py が見つけた図の範囲（WORK/regions/pNNNN.json。無ければ空）。"""
    p = C.work_path(work, "regions", C.pname(n) + ".json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return [r["box"] for r in json.load(f)["regions"]]


def inside_any(line, boxes) -> bool:
    """行の中心が、どれかの範囲の中にあるか（図の中の文字は清書に書かないので比べない）。"""
    x, y = (line["x0"] + line["x1"]) / 2, (line["y0"] + line["y1"]) / 2
    return any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in boxes)


def zoom_command(work, n: int, box) -> str:
    b = ",".join(f"{v:.3f}" for v in box)
    return " ".join(shlex.quote(str(x)) for x in
                    (sys.executable, SCRIPTS_DIR / "crop.py", Path(work).resolve(), n, "--box", b, "--zoom"))


def choose_ref(work, page: dict, want: str) -> str:
    """参照に使う読み（"text" か "ocr"）。auto: 生まれつきデジタルのページはテキスト層、
    ほかは ocr.py の結果があればそれ（テキスト層は他の OCR か、画像に重ねた文字）、無ければテキスト層。"""
    n = page["n"]
    have = [s for s in ("text", "ocr") if C.work_path(work, s, C.pname(n) + ".lines.json").exists()]
    if want != "auto":
        if want not in have:
            hint = "run ocr.py" if want == "ocr" else "the page has no text layer (prepare.py)"
            raise SystemExit(f"page {n}: no {want}/{C.pname(n)}.lines.json ({hint})")
        return want
    order = ["text", "ocr"] if page.get("kind") == "digital" else ["ocr", "text"]
    for s in order:
        if s in have:
            return s
    raise SystemExit(f"page {n}: no reference reading (no text layer; run ocr.py for this page)")


def reference_quality(lines, toks) -> dict:
    """参照そのものの質の目安。OCR の平均の確からしさ（語の数で重み付け）と、
    空白で区切る文字の語のうち 1〜2 字の語の割合。"""
    confs = [(line["conf"], len(line["text"].split())) for line in lines if line.get("conf") is not None]
    words = sum(w for _, w in confs)
    conf = round(sum(c * w for c, w in confs) / words, 1) if words else None
    spaced = [t for t in toks if not C.is_no_space_char(t[0])]
    short = round(sum(len(t) <= 2 for t in spaced) / len(spaced), 3) if spaced else None
    return {"conf": conf, "short_token_share": short, "poor": conf is not None and conf < POOR_CONF}


def compare_page(work, manifest: dict, n: int, want: str, frag_path=None) -> dict:
    page = next(p for p in manifest["pages"] if p["n"] == n)
    source = choose_ref(work, page, want)
    frag_path = Path(frag_path or C.work_path(work, "frag", C.pname(n) + ".html"))
    if not frag_path.exists():
        raise SystemExit(f"page {n}: no fragment {frag_path}")
    frag = BeautifulSoup(frag_path.read_text(encoding="utf-8"), "html.parser")
    out = C.tokens("".join(p for p in text_pieces(frag, {"tex"}) if isinstance(p, str)))

    all_lines = {m["n"]: load_lines(work, source, m["n"]) for m in manifest["pages"]}
    furn = furniture({m: ls for m, ls in all_lines.items() if ls})
    figs = figure_boxes(work, n)
    in_fig = [line for line in all_lines[n] if (n, line["id"]) not in furn and inside_any(line, figs)]
    lines = [line for line in all_lines[n] if (n, line["id"]) not in furn and not inside_any(line, figs)]
    ref, owner = line_tokens(lines, set(out))
    r2o, o2r = align(ref, out)

    # 見つからない語の多い参照の行。同じブロックで続く行はまとめる
    found_in_line, total_in_line = [0] * len(lines), [0] * len(lines)
    for i, o in zip(owner, r2o):
        total_in_line[i] += 1
        found_in_line[i] += o >= 0
    weak, prev = [], None
    for i, line in enumerate(lines):
        if not total_in_line[i] or found_in_line[i] / total_in_line[i] >= WEAK_LINE:
            prev = None
            continue
        item = {"id": line["id"], "text": line["text"], "found": round(found_in_line[i] / total_in_line[i], 2),
                "math": bool(re.search("[\U0001D400-\U0001D7FF]", line["text"]))}
        if prev is not None and lines[prev].get("block") == line.get("block"):
            weak[-1]["lines"].append(item)
            weak[-1]["box"] = union([weak[-1]["box"], box_of(line)])
        else:
            weak.append({"box": box_of(line), "lines": [item]})
        prev = i

    # 参照に無い清書の語の続き。場所は、前後で対応の付いた語の行から推す
    extra = []
    for start, end in runs([r < 0 for r in o2r]):
        toks = out[start:end]
        need = MIN_RUN_NO_SPACE if all(C.is_no_space_char(t[0]) for t in toks) else MIN_RUN
        if len(toks) < need:
            continue
        near = [lines[owner[o2r[j]]] for j in (start - 1, end) if 0 <= j < len(out) and o2r[j] >= 0]
        extra.append({"text": show_tokens(toks), "words": len(toks),
                      "box": union([box_of(x) for x in near]) if near else [0, 0, 1, 1]})
    for item in weak + extra:
        item["zoom"] = zoom_command(work, n, item["box"])

    found = sum(o >= 0 for o in r2o)
    used = sum(r >= 0 for r in o2r)
    return {
        "page": n, "kind": page.get("kind"), "ref": source,
        "ref_file": str(C.work_path(work, source, C.pname(n) + ".lines.json")), "frag": str(frag_path),
        "coverage": round(found / len(ref), 3) if ref else None,
        "precision": round(used / len(out), 3) if out else None,
        "ref_tokens": len(ref), "ref_found": found, "frag_tokens": len(out), "frag_found": used,
        "furniture": [line["text"] for line in all_lines[n] if (n, line["id"]) in furn],
        "in_figures": [line["text"] for line in in_fig],
        "weak_lines": weak, "extra": extra, "quality": reference_quality(lines, ref),
    }


# ---------------------------------------------------------------- 表示

def ratio(num, den) -> str:
    return f"{num / den:.3f} ({num}/{den})" if den else "n/a (no words)"


def fmt_box(b) -> str:
    return ",".join(f"{v:.3f}" for v in b)


def print_result(r: dict, limit: int) -> None:
    ref = {"text": "text layer", "ocr": "OCR (ocr.py)"}[r["ref"]]
    q = r["quality"]
    qual = f"; OCR confidence {q['conf']}" if q["conf"] is not None else ""
    print(f"{C.pname(r['page'])} vs {ref}{qual}: coverage {ratio(r['ref_found'], r['ref_tokens'])}, "
          f"precision {ratio(r['frag_found'], r['frag_tokens'])}; left out: {len(r['furniture'])} furniture, "
          f"{len(r['in_figures'])} line(s) inside figures")
    if r["ref"] == "text" and r["kind"] != "digital":
        print("  (this text layer is not typeset text: OCR by another program or text over a scan)")
    if q["poor"]:
        print(f"  WARNING: the reference looks poor (confidence < {POOR_CONF}); trust the page image, not the mismatches")
    for title, items in (("reference lines mostly missing from the fragment", r["weak_lines"]),
                         ("fragment text not in the reference", r["extra"])):
        if not items:
            continue
        print(f"  {title} ({len(items)}):")
        for item in items[:limit]:
            if "lines" in item:
                first = item["lines"][0]
                more = f" (+{len(item['lines']) - 1} lines)" if len(item["lines"]) > 1 else ""
                math = " [math]" if any(l["math"] for l in item["lines"]) else ""
                print(f"    {fmt_box(item['box'])}  {first['found']:.0%} {first['text'][:70]!r}{more}{math}")
            else:
                print(f"    {fmt_box(item['box'])}  {item['text'][:70]!r}")
        if len(items) > limit:
            print(f"    ... {len(items) - limit} more in WORK/compare/{C.pname(r['page'])}.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work", help="work dir (made by prepare.py)")
    ap.add_argument("page", help="page number, or a list such as 1-4,7, or all")
    ap.add_argument("--ref", choices=["auto", "text", "ocr"], default="auto",
                    help="reference reading (default auto: text layer for digital pages, else ocr.py's result)")
    ap.add_argument("--frag", help="fragment to compare instead of WORK/frag/pNNNN.html (one page only)")
    ap.add_argument("--max", type=int, default=12, help="list at most this many items of each kind per page (default 12)")
    a = ap.parse_args()
    work = Path(a.work)
    if not C.work_path(work, "manifest.json").exists():
        raise SystemExit(f"no manifest.json in {work}: run prepare.py first")
    manifest = C.load_manifest(work)
    pages = C.parse_pages(a.page, manifest["npages"])
    if a.frag and len(pages) != 1:
        raise SystemExit("--frag needs a single page")
    C.work_path(work, "compare").mkdir(exist_ok=True)
    print(f"to look closer at a box: {shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / 'crop.py'))} "
          f"{shlex.quote(str(work))} PAGE --box BOX --zoom")
    missing = []
    for n in pages:
        frag = a.frag or C.work_path(work, "frag", C.pname(n) + ".html")
        if not Path(frag).exists():
            missing.append(n)
            continue
        r = compare_page(work, manifest, n, a.ref, frag)
        with open(C.work_path(work, "compare", C.pname(n) + ".json"), "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print_result(r, a.max)
    if missing:
        raise SystemExit(f"no fragment for page(s) {','.join(map(str, missing))} (WORK/frag/pNNNN.html)")


if __name__ == "__main__":
    main()
