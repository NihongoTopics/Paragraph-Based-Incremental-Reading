#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""図、写真、グラフ、樹形図、表のありそうな範囲の候補を出す。候補は手がかりで、範囲は画像を見て決める。

使い方: regions.py WORK PAGES

  PAGES は 3、1-4,7、all のどれか。
  デジタルのページ: 文字を描かずにページを描き（gs -dFILTERTEXT）、残った線や画像のまとまりを探す。
  gs が無ければ、スキャンのページと同じ方法（テキストレイヤの行の範囲を消す）で探す。
  スキャンのページ: ページの画像の黒い画素から、文字の行（テキストレイヤか ocr.py の行）の範囲を除き、
  残りのまとまりを探す。
  どちらも、近いまとまりをつなぎ、小さいもの（汚れ）と細い線（罫線 1 本）を除く。図の中の文字（目盛り、
  ラベル）は描かれないので、候補の近くの短い行を範囲に含める。

書き出すもの: WORK/regions/pNNNN.json と、番号付きの枠を重ねた WORK/pages/pNNNN.regions.png。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

C.need("PIL", "pillow")
from PIL import Image, ImageDraw, ImageFont

import compare   # load_lines

DPI = 72
CELL = 4             # まとまりを探す升目（画素）
JOIN = 0.015         # この距離（ページに対する割合）より近いまとまりはつなぐ
JOIN_SCAN = 0.035    # スキャンでは文字を消した跡で図が切れ切れになるので、広めにつなぐ
MIN_AREA = 0.002     # これより小さい候補は除く（ページの面積に対する割合）
MIN_SIDE = 0.006     # 幅か高さがこれより小さい候補（罫線 1 本）は除く
LABEL_NEAR = 0.03    # 候補からこの距離までの短い行を範囲に含める
LABEL_MAX_W = 0.2    # 短い行と見なす幅


def ink_digital(pdf: str, n: int) -> Image.Image:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "p.png"
        C.run(["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pnggray", f"-r{DPI}",
               f"-dFirstPage={n}", f"-dLastPage={n}", "-dFILTERTEXT", f"-sOutputFile={out}", pdf])
        with Image.open(out) as im:
            return im.point(lambda v: 255 if v < 245 else 0).convert("L")


def ink_scan(pdf: str, n: int, lines: list, dark: int = 150) -> Image.Image:
    """ページを描いて文字の行の範囲を消した画像。dark より暗い画素を黒と見なす。
    スキャンは紙の汚れを拾わないよう 150、デジタルのページ（gs が無いとき）は白地がきれいなので 245
    （淡い色の写真や細い線も拾う）。"""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "p"
        C.run(["pdftoppm", "-f", str(n), "-l", str(n), "-r", str(DPI), "-gray", "-singlefile", "-png", pdf, str(out)])
        with Image.open(out.with_suffix(".png")) as im:
            ink = im.convert("L").point(lambda v: 255 if v < dark else 0)
    W, H = ink.size
    d = ImageDraw.Draw(ink)
    # 文字の行を消す。OCR が図の中を読み違えた行（確からしさが低い、ふつうの行より高い）は消さない
    heights = sorted(l["y1"] - l["y0"] for l in lines)
    usual = heights[len(heights) // 2] if heights else 0
    lines = [l for l in lines if l.get("conf", 100) >= 60 and l["y1"] - l["y0"] <= 2 * usual]
    for l in lines:
        d.rectangle([l["x0"] * W - 2, l["y0"] * H - 2, l["x1"] * W + 2, l["y1"] * H + 2], fill=0)
    return ink


def components(ink: Image.Image) -> list:
    """升目ごとの黒の有無で、8 方向につながるまとまりの箱（割合）を返す。"""
    W, H = ink.size
    gw, gh = (W + CELL - 1) // CELL, (H + CELL - 1) // CELL
    small = ink.resize((gw, gh), Image.BOX)
    px = small.load()
    on = {(x, y) for y in range(gh) for x in range(gw) if px[x, y] > 20}
    boxes = []
    while on:
        stack = [on.pop()]
        x0 = x1 = stack[0][0]
        y0 = y1 = stack[0][1]
        while stack:
            x, y = stack.pop()
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (x + dx, y + dy)
                    if q in on:
                        on.remove(q)
                        stack.append(q)
        boxes.append([x0 / gw, y0 / gh, (x1 + 1) / gw, (y1 + 1) / gh])
    return boxes


def near(a, b, d) -> bool:
    return not (a[2] + d < b[0] or b[2] + d < a[0] or a[3] + d < b[1] or b[3] + d < a[1])


def merge(boxes: list, d: float) -> list:
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if near(boxes[i], boxes[j], d):
                    a, b = boxes[i], boxes.pop(j)
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    changed = True
                    break
            if changed:
                break
    return boxes


def with_labels(box: list, lines: list) -> list:
    """候補の近くの短い行（図の中の文字）を範囲に含める。図の中の文字は 1 行だけの区画（block）になる。
    段落の最後の短い行やキャプションの 2 行目（何行もある区画の行）は含めない。"""
    per_block = Counter(l.get("block") for l in lines)
    lines = [l for l in lines if per_block[l.get("block")] == 1]
    for _ in range(3):
        grown = box
        for l in lines:
            if l["x1"] - l["x0"] <= LABEL_MAX_W and near(grown, [l["x0"], l["y0"], l["x1"], l["y1"]], LABEL_NEAR):
                grown = [min(grown[0], l["x0"]), min(grown[1], l["y0"]),
                         max(grown[2], l["x1"]), max(grown[3], l["y1"])]
        if grown == box:
            break
        box = grown
    return box


def page_regions(work: Path, pdf: str, page: dict) -> list:
    n = page["n"]
    lines = (compare.load_lines(work, "ocr", n) or compare.load_lines(work, "text", n) or [])
    scan = page["kind"] in ("scan", "scan-ocr", "scan-text")
    if scan and not lines:
        raise SystemExit(f"page {n} is a scan without text lines: run ocr.py for it first "
                         f"(the text has to be removed before the figures can be found)")
    # デジタルのページは、文字を描かずに描く（gs）。gs が無ければスキャンと同じく、描いたページから文字の行を消す
    if scan:
        ink = ink_scan(pdf, n, lines)
    elif shutil.which("gs"):
        ink = ink_digital(pdf, n)
    else:
        ink = ink_scan(pdf, n, lines, dark=245)
    boxes = merge(components(ink), JOIN_SCAN if scan else JOIN)
    boxes = [b for b in boxes if (b[2] - b[0]) * (b[3] - b[1]) >= MIN_AREA
             and b[2] - b[0] >= MIN_SIDE and b[3] - b[1] >= MIN_SIDE]
    if not scan:
        boxes = merge([with_labels(b, lines) for b in boxes], 0)
    boxes.sort(key=lambda b: (b[1], b[0]))
    return [{"id": i, "box": [round(v, 3) for v in b]} for i, b in enumerate(boxes, 1)]


def overlay(work: Path, page: dict, regions: list) -> Path:
    with Image.open(C.work_path(work, page["png"])) as im:
        im = im.convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=max(18, W // 50))
    for r in regions:
        x0, y0, x1, y1 = r["box"]
        d.rectangle([x0 * W, y0 * H, x1 * W, y1 * H], outline=(220, 0, 0), width=4)
        d.text((x0 * W + 6, y0 * H + 4), str(r["id"]), fill=(220, 0, 0), font=font,
               stroke_width=3, stroke_fill=(255, 255, 255))
    out = C.work_path(work, "pages", C.pname(page["n"]) + ".regions.png")
    im.save(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("pages")
    a = ap.parse_args()
    work = Path(a.work)
    m = C.load_manifest(work)
    pages = {p["n"]: p for p in m["pages"]}
    C.work_path(work, "regions").mkdir(exist_ok=True)
    for n in C.parse_pages(a.pages, m["npages"]):
        if n not in pages:
            raise SystemExit(f"page {n} is not prepared (run prepare.py)")
        regions = page_regions(work, m["pdf"], pages[n])
        with open(C.work_path(work, "regions", C.pname(n) + ".json"), "w", encoding="utf-8") as f:
            json.dump({"page": n, "regions": regions}, f, indent=1)
        img = overlay(work, pages[n], regions)
        print(f"{C.pname(n)}: {len(regions)} candidate(s)  {img}")
        for r in regions:
            print(f"  {r['id']}: --box " + ",".join(f"{v:.3f}" for v in r["box"]))


if __name__ == "__main__":
    main()
