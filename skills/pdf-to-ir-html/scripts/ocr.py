#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ページを tesseract で OCR し、行と位置を書き出す。スキャンのページと、OCR の文字が使えないページに使う。

使い方: ocr.py WORK --lang jpn+eng [--pages SPEC] [--psm 3] [--dpi 300] [-j 4]

  --lang   tesseract の言語。いちばん多い言語を先に書く（jpn+eng と eng+jpn は結果が違う。後者は日本語の
           字の間に空白を入れる）。縦書きの日本語は jpn_vert。使える言語は tesseract --list-langs。
  --pages  既定は kind が scan のページ全部。
  --psm    tesseract のページの分け方。3（既定）と 1 は段組の段を分けて読む。4 と 6 は分けない。

書き出すもの:
  WORK/ocr/pNNNN.lines.json  行（tesseract の段落 par と区画 block、位置はページに対する割合、conf は語の
                             確からしさの平均）
  WORK/ocr/pNNNN.txt         行の文字。tesseract の段落の間は空行
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import csv
import io
import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_DPI = 400


def join_words(words: list) -> str:
    """語をつなぐ。空白で区切らない文字（漢字、かななど）どうしの間には空白を入れない。"""
    out = ""
    for w in words:
        if out and not (C.is_no_space_char(out[-1]) and C.is_no_space_char(w[0])):
            out += " "
        out += w
    return out


def parse_tsv(tsv: str, width: int, height: int) -> list:
    """tesseract の TSV から行のリストを作る（level 4 が行、5 が語。位置は画像の画素）。"""
    rows = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    lines = {}
    for r in rows:
        try:
            level = int(r["level"])
            key = (int(r["block_num"]), int(r["par_num"]), int(r["line_num"]))
        except (TypeError, ValueError):
            continue
        if level == 4:
            l, t, w, h = (int(r[k]) for k in ("left", "top", "width", "height"))
            lines[key] = {"box": (l, t, l + w, t + h), "words": [], "confs": []}
        elif level == 5 and (r["text"] or "").strip() and key in lines:
            lines[key]["words"].append(r["text"].strip())
            lines[key]["confs"].append(float(r["conf"]))
    out = []
    for (block, par, line), v in lines.items():
        if not v["words"]:
            continue
        x0, y0, x1, y1 = v["box"]
        out.append({
            "id": f"b{block}p{par}l{line}", "block": block, "par": par,
            "x0": round(x0 / width, 4), "y0": round(y0 / height, 4),
            "x1": round(x1 / width, 4), "y1": round(y1 / height, 4),
            "text": join_words(v["words"]), "conf": round(sum(v["confs"]) / len(v["confs"]), 1),
        })
    return out


def ocr_page(pdf: str, page: dict, work: Path, lang: str, psm: int, dpi: int) -> str:
    n = page["n"]
    dpi = min(MAX_DPI, max(dpi, page.get("scan_ppi") or 0))
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "page"
        C.run(["pdftoppm", "-f", str(n), "-l", str(n), "-r", str(dpi), "-gray", "-singlefile", "-png",
               pdf, str(img)])
        png = img.with_suffix(".png")
        from PIL import Image
        with Image.open(png) as im:
            width, height = im.size
        base = Path(tmp) / "out"
        r = C.run(["tesseract", str(png), str(base), "-l", lang, "--psm", str(psm), "--dpi", str(dpi), "tsv"],
                  check=False)
        tsv = base.with_suffix(".tsv")
        if not tsv.exists():   # tesseract は書けなかったときも終了コード 0 を返す
            raise SystemExit(f"page {n}: tesseract wrote no output\n{r.stderr}")
        lines = parse_tsv(tsv.read_text(encoding="utf-8"), width, height)
    name = C.pname(n)
    with open(C.work_path(work, "ocr", name + ".lines.json"), "w", encoding="utf-8") as f:
        json.dump({"page": n, "source": "ocr", "lang": lang, "psm": psm, "dpi": dpi, "lines": lines},
                  f, ensure_ascii=False, indent=0)
    text, prev = [], None
    for line in lines:
        if prev is not None and (line["block"], line["par"]) != prev:
            text.append("")
        text.append(line["text"])
        prev = (line["block"], line["par"])
    C.work_path(work, "ocr", name + ".txt").write_text("\n".join(text) + "\n", encoding="utf-8")
    conf = sum(l["conf"] for l in lines) / len(lines) if lines else 0
    return f"{n:>5} {len(lines):>6} lines  mean conf {conf:5.1f}  ({dpi} dpi)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("--lang", required=True, help="tesseract languages, dominant first, e.g. jpn+eng")
    ap.add_argument("--pages", help="default: the pages whose kind is scan")
    ap.add_argument("--psm", type=int, default=3)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("-j", type=int, default=4, help="pages in parallel (default 4)")
    a = ap.parse_args()

    work = Path(a.work)
    m = C.load_manifest(work)
    installed = set(C.run(["tesseract", "--list-langs"]).stdout.split()[1:])
    missing = [x for x in a.lang.split("+") if x not in installed]
    if missing:
        raise SystemExit(f"tesseract has no language data for: {', '.join(missing)} (see tesseract --list-langs)")
    if a.pages:
        nums = C.parse_pages(a.pages, m["npages"])
    else:
        nums = [p["n"] for p in m["pages"] if p["kind"] == "scan"]
        if not nums:
            raise SystemExit("no page of kind scan; give --pages")
    pages = {p["n"]: p for p in m["pages"]}
    unknown = [n for n in nums if n not in pages]
    if unknown:
        raise SystemExit(f"pages not prepared yet (run prepare.py): {unknown}")
    C.work_path(work, "ocr").mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=max(1, a.j)) as ex:
        results = list(ex.map(lambda n: ocr_page(m["pdf"], pages[n], work, a.lang, a.psm, a.dpi), nums))
    m = C.load_manifest(work)
    for p in m["pages"]:
        if p["n"] in nums:
            p["ocr"] = {"lang": a.lang, "psm": a.psm}
    C.save_manifest(work, m)
    print(f"{'page':>5} {'lines':>6}")
    print("\n".join(results))


if __name__ == "__main__":
    main()
