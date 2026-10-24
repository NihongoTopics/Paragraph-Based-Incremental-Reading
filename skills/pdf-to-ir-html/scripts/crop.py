#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ページの一部を PDF から切り出す。図の画像、細部を読むための拡大、座標を読むための目盛り付きの画像。

使い方:
  crop.py WORK PAGE --grid
      WORK/pages/pNNNN.grid.png を作る。ページの画像に 5% ごとの線と 10% ごとの目盛り（0〜100）を重ねたもの。
      座標（ページに対する割合）を読むのに使う。
  crop.py WORK PAGE --box x0,y0,x1,y1 --out figs/NAME [--dpi N] [--pad F] [--format auto|png|jpg]
          [--max-width 1600]
      図の画像を書き出す。座標はページに対する割合（左上 0,0、右下 1,1）。NAME に拡張子は付けない。
      dpi の既定: スキャンのページは画像の解像度（150〜400 に収める）、ほかは 250。
      format auto: スキャンのページと色の多い画像は JPEG（品質 88）、線画など色の少ない画像は PNG。
      幅が --max-width を超えれば縮める（カードの幅は 782 px）。
  crop.py WORK PAGE --box x0,y0,x1,y1 --zoom [--dpi 300] [--large]
      細部を読むための拡大を WORK/zoom/ に書き出す（Claude の画像の標準の上限、1568 トークンに収まる大きさ。
      --large で高解像度の上限、4784 トークン）。出力には使わない。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import tempfile
from pathlib import Path

C.need("PIL", "pillow")
from PIL import Image, ImageDraw, ImageFont

from prepare import HI_EDGE, HI_TOKENS, resized_size


def parse_box(s: str) -> list:
    try:
        box = [float(v) for v in s.split(",")]
    except ValueError:
        raise SystemExit(f"--box must be x0,y0,x1,y1 (fractions of the page): {s}")
    if len(box) != 4 or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
        raise SystemExit(f"--box must be x0,y0,x1,y1 with 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1: {s}")
    return box


def render_box(pdf: str, page: dict, box: list, dpi: int) -> Image.Image:
    """box（割合）の範囲を dpi で描いた画像。pdftoppm の -x -y -W -H は、回転を当てたページの画素。"""
    W, H = page["w_pt"] * dpi / 72, page["h_pt"] * dpi / 72
    x, y = int(box[0] * W), int(box[1] * H)
    w, h = max(1, round((box[2] - box[0]) * W)), max(1, round((box[3] - box[1]) * H))
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "crop"
        C.run(["pdftoppm", "-f", str(page["n"]), "-l", str(page["n"]), "-r", str(dpi),
               "-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h), "-singlefile", "-png",
               page["_pdf"], str(out)])
        with Image.open(out.with_suffix(".png")) as im:
            return im.convert("RGB")


def grid(work: Path, page: dict) -> Path:
    src = C.work_path(work, page["png"])
    with Image.open(src) as im:
        im = im.convert("RGB")
    W, H = im.size
    over = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = ImageFont.load_default(size=max(14, W // 70))
    for i in range(1, 20):
        major = i % 2 == 0
        color = (220, 0, 0, 150) if major else (0, 120, 220, 90)
        x, y = round(W * i / 20), round(H * i / 20)
        d.line([(x, 0), (x, H)], fill=color, width=2 if major else 1)
        d.line([(0, y), (W, y)], fill=color, width=2 if major else 1)
        if major:
            label = str(i * 5)
            for pos in ((x + 3, 2), (x + 3, H - font.size - 4)):
                d.text(pos, label, fill=(220, 0, 0, 255), font=font, stroke_width=3, stroke_fill=(255, 255, 255, 255))
            for pos in ((3, y + 2), (W - 2 * font.size - 6, y + 2)):
                d.text(pos, label, fill=(220, 0, 0, 255), font=font, stroke_width=3, stroke_fill=(255, 255, 255, 255))
    out = C.work_path(work, "pages", C.pname(page["n"]) + ".grid.png")
    Image.alpha_composite(im.convert("RGBA"), over).convert("RGB").save(out)
    return out


def is_scan(page: dict) -> bool:
    return page["kind"] in ("scan", "scan-ocr", "scan-text")


def default_dpi(page: dict) -> int:
    if is_scan(page) and page.get("scan_ppi"):
        return min(400, max(150, page["scan_ppi"]))
    return 250


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("work")
    ap.add_argument("page", type=int)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--box")
    ap.add_argument("--out", help="figs/NAME (no extension); relative to WORK")
    ap.add_argument("--zoom", action="store_true")
    ap.add_argument("--large", action="store_true", help="with --zoom: allow Claude's high-resolution size "
                    "(about 3 times the tokens); only for print too small to read otherwise")
    ap.add_argument("--dpi", type=int)
    ap.add_argument("--pad", type=float, help="margin added on every side, fraction of the page "
                    "(default 0.005 for figures, 0.01 for --zoom)")
    ap.add_argument("--format", choices=["auto", "png", "jpg"], default="auto")
    ap.add_argument("--max-width", type=int, default=1600)
    a = ap.parse_args()

    work = Path(a.work)
    m = C.load_manifest(work)
    page = next((p for p in m["pages"] if p["n"] == a.page), None)
    if page is None:
        raise SystemExit(f"page {a.page} is not prepared (run prepare.py)")
    page["_pdf"] = m["pdf"]

    if a.grid:
        print(grid(work, page))
        return
    if not a.box:
        raise SystemExit("give --grid, or --box with --out or --zoom")
    box = parse_box(a.box)

    if a.zoom:
        pad = 0.01 if a.pad is None else a.pad
        box = [max(0, box[0] - pad), max(0, box[1] - pad), min(1, box[2] + pad), min(1, box[3] + pad)]
        im = render_box(m["pdf"], page, box, a.dpi or 300)
        w, h = resized_size(*im.size, HI_EDGE, HI_TOKENS) if a.large else resized_size(*im.size)
        if (w, h) != im.size:
            im = im.resize((w, h), Image.LANCZOS)
        name = f"{C.pname(page['n'])}-{'-'.join(f'{v:.3f}'[2:] if v < 1 else '1000' for v in box)}.png"
        out = C.work_path(work, "zoom", name)
        out.parent.mkdir(exist_ok=True)
        im.save(out)
        print(f"{out}  {w}x{h}")
        return

    if not a.out:
        raise SystemExit("--box needs --out figs/NAME (or --zoom)")
    pad = 0.005 if a.pad is None else a.pad
    box = [max(0, box[0] - pad), max(0, box[1] - pad), min(1, box[2] + pad), min(1, box[3] + pad)]
    dpi = a.dpi or default_dpi(page)
    im = render_box(m["pdf"], page, box, dpi)
    if im.width > a.max_width:
        im = im.resize((a.max_width, round(im.height * a.max_width / im.width)), Image.LANCZOS)
    fmt = a.format
    if fmt == "auto":
        many_colors = im.getcolors(maxcolors=4096) is None
        fmt = "jpg" if is_scan(page) or many_colors else "png"
    out = C.work_path(work, a.out).with_suffix("." + fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    for old in (out.with_suffix(".png"), out.with_suffix(".jpg")):
        if old != out and old.exists():
            old.unlink()   # 同じ名前で形式の違う古い切り出しを残さない（assemble.py が迷う）
    if fmt == "jpg":
        im.save(out, quality=88, optimize=True)
    else:
        im.save(out, optimize=True)
    print(f"{out}  {im.width}x{im.height}  {out.stat().st_size // 1024} KB  ({dpi} dpi, {fmt})")


if __name__ == "__main__":
    main()
