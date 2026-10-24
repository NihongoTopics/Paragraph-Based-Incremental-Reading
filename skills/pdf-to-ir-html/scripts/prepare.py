#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF の下調べ。作業フォルダを作り、ページごとの種類、段組、用字を manifest.json に書き、
ページの画像とテキストレイヤ（あれば）を書き出す。

使い方: prepare.py PDF WORK [--pages SPEC] [--force] [-j 4]

書き出すもの（N はページ番号、pNNNN は p0001 の形）:
  WORK/manifest.json         ページごとの kind, 文字数, 段組, 用字, 画像の大きさなど
  WORK/pages/pNNNN.png       ページの画像。Claude の画像の標準の上限（長辺 1568 px、1568 トークン）に収まる
                             大きさ（A4 で約 925x1308、約 112 dpi）。組み方を見るためのもので、細かい字は crop.py --zoom で読む
  WORK/pages/sheet-NN.png    4 ページずつを 1 枚に並べた画像（同じ上限に収まる）。文書の全体を安く見るため
  WORK/text/pNNNN.txt        pdftotext（既定のモード）のそのページ
  WORK/text/pNNNN.lines.json テキストレイヤの行と位置（ページに対する割合、左上が 0,0）

kind:
  digital    ふつうの文字を持つページ
  scan-ocr   ページ全体の画像に、OCR の文字（GlyphLessFont。ocrmypdf や tesseract が作るもの）
  scan-text  ページ全体の画像に、ほかの方法で作られた文字
  scan       画像だけで文字が無い
  no-text    文字も大きな画像も無い（白紙、線だけの図など）
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

XHTML = "{http://www.w3.org/1999/xhtml}"
# Claude の画像の上限（2026-09-25 に文書で確認）。標準の段階（長辺 1568 px、1568 トークン）を既定にする。
# 高解像度の段階（2576 px、4784 トークン）は 1 枚が約 3 倍のトークンになるので、crop.py --zoom --large でだけ使う
MAX_EDGE, MAX_TOKENS = 1568, 1568
HI_EDGE, HI_TOKENS = 2576, 4784
SHEET_PAGES = 4         # 1 枚の一覧の画像に並べるページの数（2 x 2）
SCAN_COVER = 0.8        # ページのこの割合以上を覆う画像があればスキャンのページ
SCAN_COVER_NO_TEXT = 0.5  # 文字が無いときは、これ以上でスキャンと見なす
BAND = 0.07             # 段組を調べるとき、柱とノンブルのためにページの上下のこの帯を除く
MAX_CROSS = 0.1         # 段が並ぶ範囲で段の間を横切ってよい行の割合
MIN_PAIRED = 5          # 段の間の左右に並ぶ行の最小の数
MIN_GAP, MAX_GAP = 0.012, 0.08  # 段の間の白い帯の幅（ページの幅に対する割合）

_RTL = re.compile("[֐-ࣿיִ-﷿ﹰ-ﻼ]")
_LTR = re.compile("[A-Za-zÀ-ɏͰ-ϿЀ-ӿ぀-鿿가-힯]")


# ---------------------------------------------------------------- 画像の大きさ

def resized_size(width: int, height: int, max_edge=MAX_EDGE, max_tokens=MAX_TOKENS) -> tuple:
    """Claude が画像を縮小したあとの大きさ。
    https://platform.claude.com/docs/en/build-with-claude/vision-coordinates の規則を写したもの。"""
    def fits(w, h):
        return (math.ceil(w / 28) * 28 <= max_edge and math.ceil(h / 28) * 28 <= max_edge
                and math.ceil(w / 28) * math.ceil(h / 28) <= max_tokens)
    if fits(width, height):
        return width, height
    if height > width:
        h, w = resized_size(height, width, max_edge, max_tokens)
        return w, h
    ar = width / height
    lo, hi = 1, width
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / ar), 1)):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / ar), 1)


def view_long_edge(w_pt: float, h_pt: float) -> int:
    """ページの画像の長辺（px）。600 dpi 相当から縮小の規則で決める。"""
    w, h = resized_size(round(w_pt * 600 / 72), round(h_pt * 600 / 72))
    return max(w, h)


# ---------------------------------------------------------------- poppler

def page_geometry(pdf: str, npages: int) -> dict:
    """{n: (w_pt, h_pt, rot)}。w, h は回転を当てたあと（表示される向き）の大きさ。"""
    out = C.run(["pdfinfo", "-f", "1", "-l", str(npages), pdf]).stdout
    sizes = {int(m[1]): (float(m[2]), float(m[3]))
             for m in re.finditer(r"^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts", out, re.M)}
    rots = {int(m[1]): int(m[2]) % 360 for m in re.finditer(r"^Page\s+(\d+) rot:\s+(-?\d+)", out, re.M)}
    geo = {}
    for n in range(1, npages + 1):
        w, h = sizes[n]
        rot = rots.get(n, 0)
        geo[n] = (h, w, rot) if rot in (90, 270) else (w, h, rot)
    return geo


def pdf_info(pdf: str) -> dict:
    info = {}
    for line in C.run(["pdfinfo", pdf]).stdout.splitlines():
        k, _, v = line.partition(":")
        if k in ("Title", "Author", "Subject", "Creator", "Producer", "Pages") and v.strip():
            info[k] = v.strip()
    return info


def images_by_page(pdf: str) -> dict:
    """{n: [{"w", "h", "ppi", "drawn_pt": [w, h]}]}（pdfimages -list。位置は分からない）。"""
    r = C.run(["pdfimages", "-list", pdf], check=False)
    out = {}
    for line in r.stdout.splitlines()[2:]:
        f = line.split()
        if len(f) < 14 or f[2] != "image":
            continue
        try:
            n, w, h, xppi, yppi = int(f[0]), int(f[3]), int(f[4]), float(f[12]), float(f[13])
        except ValueError:
            continue
        if xppi <= 0 or yppi <= 0:
            continue
        out.setdefault(n, []).append({"w": w, "h": h, "ppi": round(max(xppi, yppi)),
                                      "drawn_pt": [w / xppi * 72, h / yppi * 72]})
    return out


def fonts_of(pdf: str, n: int) -> list:
    r = C.run(["pdffonts", "-f", str(n), "-l", str(n), pdf], check=False)
    return sorted({line.split()[0] for line in r.stdout.splitlines()[2:] if line.strip()})


def page_texts(pdf: str) -> list:
    """pdftotext（既定のモード）の全ページ。ページは改ページ文字で分かれる。"""
    text = C.run(["pdftotext", "-enc", "UTF-8", pdf, "-"]).stdout
    return text.split("\f")


# ---------------------------------------------------------------- テキストレイヤの行

def _is_rtl_word(w: str) -> bool:
    return bool(_RTL.search(w))


def logical_line(words: list) -> tuple:
    """-bbox-layout の行の語（見た目の左から右の順、右から左の語は文字も逆）を、読む順の文字にする。

    右から左の語は文字を逆にする。右から左の文字が多い行は、方向の同じ語のまとまりの順を逆にし、
    右から左のまとまりの中の語の順も逆にする（左から右のまとまり、たとえば数や英語の語の中は保つ）。
    戻り値は (文字, 右から左の行か)。
    """
    fixed = [w[::-1] if _is_rtl_word(w) else w for w in words]
    rtl = sum(len(_RTL.findall(w)) for w in fixed)
    ltr = sum(len(_LTR.findall(w)) for w in fixed)
    if rtl <= ltr:
        return " ".join(fixed), False
    groups = []
    for w in fixed:
        d = "r" if _is_rtl_word(w) or not _LTR.search(w) and not re.search(r"\d", w) else "l"
        if groups and groups[-1][0] == d:
            groups[-1][1].append(w)
        else:
            groups.append((d, [w]))
    parts = []
    for d, ws in reversed(groups):
        parts.extend(reversed(ws) if d == "r" else ws)
    return " ".join(parts), True


SHY = "\u00ad"
TIGHT = 0.12   # 語の箱の間がこの割合（行の太さに対する）より狭ければ、同じ語の切れ端としてつなぐ


def merge_words(words: list) -> list:
    """-bbox-layout の語（(文字, 箱)、見た目の順）のうち、箱がほとんど接しているものをつなぐ。

    pdftotext は語をソフトハイフン（U+00AD）の位置や字間の詰めで切ることがある
    （"doc\u00ad" "u" "\u00adment"、"math\u00ad" "e" "m" "\u00adat\u00adics"。2026-09-26、Chromium の PDF で確認）。
    間の広さは、文字の進む向き（横書きのページなら x、回転したページなら y）で測る。
    """
    out = []
    for text, box in words:
        if out:
            ptext, pbox = out[-1]
            horizontal = abs((box[0] + box[2]) - (pbox[0] + pbox[2])) >= abs((box[1] + box[3]) - (pbox[1] + pbox[3]))
            if horizontal:
                gap, thick = box[0] - pbox[2], pbox[3] - pbox[1]
            else:
                gap, thick = box[1] - pbox[3], pbox[2] - pbox[0]
            if gap < TIGHT * thick or ptext.endswith(SHY) or text.startswith(SHY):
                out[-1] = (ptext + text, [min(box[0], pbox[0]), min(box[1], pbox[1]),
                                          max(box[2], pbox[2]), max(box[3], pbox[3])])
                continue
        out.append((text, box))
    return [t for t, _ in out]


def clean_soft_hyphens(text: str) -> str:
    """ソフトハイフンを消す。行末でソフトハイフンが行を折った所（U+00AD と U+2010 か "-"）は、
    行末の U+00AD 1 字にして残す（draft.py が、ハイフンを取ってつなぐ印に使う）。"""
    t = text.rstrip()
    soft_break = t.endswith((SHY + "\u2010", SHY + "-"))
    t = t.replace(SHY, "")
    if soft_break:
        t = t[:-1] + SHY
    return t


def font_runs(pdf: str) -> dict:
    """pdftohtml -xml の文字のかたまりと、その字の大きさと太字。{n: [(x0, y0, x1, y1, size, bold, 字数)]}（pt）。

    pdftotext には字の大きさが無いので、見出しを見分けるためにこれを使う。pdftohtml は太字を <b> で示す
    （フォントの名前に Bold が無くても。2026-09-28、Chrome の出した PDF で確認）。かたまりは字間の詰めで細かく切れるが、
    大きさと太字はかたまりごとに付いている。回転したページでは位置が合わないことがあり、そのときは付かない。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "f"
        r = C.run(["pdftohtml", "-xml", "-i", "-q", "-zoom", "1", "-noroundcoord", "-hidden", pdf, str(base)],
                  check=False)
        xml = base.with_suffix(".xml")
        if r.returncode != 0 or not xml.exists():
            return {}
        root = ET.fromstring(xml.read_text(encoding="utf-8", errors="replace"))
    sizes, out = {}, {}
    for page in root.iter("page"):
        for fs in page.iter("fontspec"):
            sizes[fs.get("id")] = float(fs.get("size") or 0)
        runs = []
        for t in page.iter("text"):
            chars = len("".join(t.itertext()).strip())
            if not chars:
                continue
            x0, y0 = float(t.get("left")), float(t.get("top"))
            bold = t.find("b") is not None or t.find("./i/b") is not None
            runs.append((x0, y0, x0 + float(t.get("width")), y0 + float(t.get("height")),
                         sizes.get(t.get("font"), 0.0), bold, chars))
        out[int(page.get("number"))] = runs
    return out


def add_fonts(lines: list, runs: list, w: float, h: float) -> None:
    """行（割合の座標）に、重なる文字のかたまりから字の大きさ（字数で重み付けした平均、pt）と太字（字の 6 割以上）を付ける。"""
    for line in lines:
        x0, y0, x1, y1 = line["x0"] * w, line["y0"] * h, line["x1"] * w, line["y1"] * h
        hit = [r for r in runs if x0 - 1 <= (r[0] + r[2]) / 2 <= x1 + 1 and y0 - 1 <= (r[1] + r[3]) / 2 <= y1 + 1]
        n = sum(r[6] for r in hit)
        if n:
            line["size"] = round(sum(r[4] * r[6] for r in hit) / n, 1)
            line["bold"] = sum(r[6] for r in hit if r[5]) >= 0.6 * n


def bbox_lines(pdf: str, geo: dict) -> dict:
    """pdftotext -bbox-layout の全ページを読み、{n: [行]} にする。位置は表示される向きのページに対する割合
    （-bbox-layout の語の位置は回転を当てたあとの座標。page の width と height は回転前。2026-09-26 に確認）。"""
    xml = C.run(["pdftotext", "-enc", "UTF-8", "-bbox-layout", pdf, "-"]).stdout
    root = ET.fromstring(xml)
    out = {}
    for n, page in enumerate(root.iter(XHTML + "page"), 1):
        w, h, _ = geo[n]
        lines, block_no = [], 0
        for flow_no, flow in enumerate(page.findall(XHTML + "flow")):
            for block in flow.findall(XHTML + "block"):
                block_no += 1
                for line_no, line in enumerate(block.findall(XHTML + "line"), 1):
                    words = [(wd.text or "", [float(wd.get(k)) for k in ("xMin", "yMin", "xMax", "yMax")])
                             for wd in line.findall(XHTML + "word")]
                    text, rtl = logical_line(merge_words(words))
                    text = clean_soft_hyphens(text)
                    if not text.strip():
                        continue
                    box = [float(line.get(k)) for k in ("xMin", "yMin", "xMax", "yMax")]
                    lines.append({
                        "id": f"b{block_no}l{line_no}", "flow": flow_no, "block": block_no,
                        "x0": round(box[0] / w, 4), "y0": round(box[1] / h, 4),
                        "x1": round(box[2] / w, 4), "y1": round(box[3] / h, 4),
                        "text": text, "rtl": rtl,
                    })
        out[n] = lines
    return out


# ---------------------------------------------------------------- 段組

def detect_columns(lines: list) -> dict:
    """行の位置から、1 段組か 2 段組かを推す。戻り値 {"count": 1 か 2, "gutters": [段の間の x（割合）]}。

    段の間（gutter）の候補は、左右に同じ高さの行が、狭い白い帯（MIN_GAP から MAX_GAP）を挟んで並ぶ縦の位置。
    左右に並ぶ範囲の中で帯を横切る行が MAX_CROSS 以下のものだけを採る（題や要旨のように段の外で横切るのはよい。
    斜めのスキャンの OCR で行が切れ切れになった 1 段組のページはここで外れる）。候補のうち、左右に並ぶ行が
    いちばん多い位置を段の間とする（表の升目の間の空きは並ぶ行が少ない）。ページの上下の帯（柱とノンブル）は除く。
    """
    body = [l for l in lines if BAND < (l["y0"] + l["y1"]) / 2 < 1 - BAND and l["x1"] > l["x0"]]
    if len(body) < 8:
        return {"count": 1, "gutters": []}
    best = None   # (左右に並ぶ行の数, x)
    for x in (i / 200 for i in range(40, 161)):   # 0.20 .. 0.80
        left = [l for l in body if l["x1"] <= x]
        right = [l for l in body if l["x0"] >= x]
        paired = [a for a in left
                  if any(abs((a["y0"] + a["y1"]) - (b["y0"] + b["y1"])) / 2 < (a["y1"] - a["y0"])
                         and MIN_GAP <= b["x0"] - a["x1"] <= MAX_GAP for b in right)]
        if len(paired) < MIN_PAIRED:
            continue
        top, bottom = min(a["y0"] for a in paired), max(a["y1"] for a in paired)
        inside = [l for l in body if top <= (l["y0"] + l["y1"]) / 2 <= bottom]
        if sum(l["x0"] < x < l["x1"] for l in inside) > MAX_CROSS * len(inside):
            continue
        if best is None or len(paired) > best[0]:
            best = (len(paired), x)
    return {"count": 2, "gutters": [round(best[1], 3)]} if best else {"count": 1, "gutters": []}


# ---------------------------------------------------------------- ページの種類

def classify(chars: int, fonts: list, cover: float) -> str:
    if cover >= SCAN_COVER and chars == 0:
        return "scan"
    if cover >= SCAN_COVER and "GlyphLessFont" in fonts:
        return "scan-ocr"
    if cover >= SCAN_COVER:
        return "scan-text"
    if chars > 0:
        return "digital"
    if cover >= SCAN_COVER_NO_TEXT:
        return "scan"
    return "no-text"


def render(pdf: str, n: int, long_edge: int, dest: Path) -> None:
    C.run(["pdftoppm", "-f", str(n), "-l", str(n), "-scale-to", str(long_edge), "-singlefile", "-png",
           pdf, str(dest.with_suffix(""))])


def make_sheets(work: Path, pages: list) -> list:
    """4 ページずつを 2 x 2 に並べ、ページ番号を書いた一覧の画像を作る。戻り値は作ったファイル。"""
    from PIL import Image, ImageDraw, ImageFont
    out = []
    for i in range(0, len(pages), SHEET_PAGES):
        group = pages[i:i + SHEET_PAGES]
        ims = [Image.open(C.work_path(work, p["png"])).convert("RGB") for p in group]
        cw, ch = max(im.width for im in ims), max(im.height for im in ims)
        cols = 2 if len(ims) > 1 else 1
        rows = (len(ims) + cols - 1) // cols
        gap = 12
        sheet = Image.new("RGB", (cols * cw + (cols - 1) * gap, rows * ch + (rows - 1) * gap), (150, 150, 150))
        d = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=max(28, cw // 18))
        for k, (p, im) in enumerate(zip(group, ims)):
            x, y = (k % cols) * (cw + gap), (k // cols) * (ch + gap)
            sheet.paste(im, (x, y))
            d.text((x + 10, y + 6), f"p.{p['n']}", fill=(220, 0, 0), font=font, stroke_width=4, stroke_fill=(255, 255, 255))
        w, h = resized_size(*sheet.size)
        sheet = sheet.resize((w, h), Image.LANCZOS)
        dest = C.work_path(work, "pages", f"sheet-{i // SHEET_PAGES + 1:02d}.png")
        sheet.save(dest)
        out.append(dest)
    return out


def png_size(p: Path) -> list:
    from PIL import Image
    with Image.open(p) as im:
        return list(im.size)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("work")
    ap.add_argument("--pages", help="pages to prepare, e.g. 1-10 (default: all)")
    ap.add_argument("--force", action="store_true", help="render the page images again")
    ap.add_argument("-j", type=int, default=4, help="parallel renders (default 4)")
    a = ap.parse_args()

    pdf = str(Path(a.pdf).resolve())
    if not Path(pdf).is_file():
        raise SystemExit(f"no such file: {pdf}")
    work = Path(a.work)
    for d in ("pages", "text", "draft", "frag", "figs", "zoom"):
        C.work_path(work, d).mkdir(parents=True, exist_ok=True)

    info = pdf_info(pdf)
    npages = int(info.get("Pages", 0))
    if npages < 1:
        raise SystemExit("pdfinfo reports no pages")
    pages = C.parse_pages(a.pages, npages)
    geo = page_geometry(pdf, npages)
    images = images_by_page(pdf)
    texts = page_texts(pdf)
    lines_all = bbox_lines(pdf, geo)
    fonts_all = font_runs(pdf)
    for n, lines in lines_all.items():
        if not geo[n][2]:   # 回転したページには付けない
            add_fonts(lines, fonts_all.get(n, []), geo[n][0], geo[n][1])

    try:
        manifest = C.load_manifest(work)
        if manifest.get("pdf") != pdf:
            raise SystemExit(f"{work} was prepared for another PDF: {manifest.get('pdf')}")
    except FileNotFoundError:
        manifest = {"pdf": pdf, "npages": npages, "info": info, "pages": []}
    by_n = {p["n"]: p for p in manifest["pages"]}

    todo = []
    for n in pages:
        w, h, rot = geo[n]
        text = texts[n - 1] if n - 1 < len(texts) else ""
        chars = len(re.sub(r"\s", "", text))
        fonts = fonts_of(pdf, n) if chars else []
        imgs = images.get(n, [])
        cover = max((i["drawn_pt"][0] * i["drawn_pt"][1] / (w * h) for i in imgs), default=0.0)
        kind = classify(chars, fonts, cover)
        scan_ppi = max((i["ppi"] for i in imgs
                        if i["drawn_pt"][0] * i["drawn_pt"][1] / (w * h) >= SCAN_COVER_NO_TEXT), default=None)
        lines = lines_all.get(n, [])
        name = C.pname(n)
        if chars:
            C.work_path(work, "text", name + ".txt").write_text(text, encoding="utf-8")
            with open(C.work_path(work, "text", name + ".lines.json"), "w", encoding="utf-8") as f:
                json.dump({"page": n, "source": "text", "lines": lines}, f, ensure_ascii=False, indent=0)
        png = C.work_path(work, "pages", name + ".png")
        if a.force or not png.exists():
            todo.append((n, view_long_edge(w, h), png))
        by_n[n] = {
            "n": n, "w_pt": round(w, 2), "h_pt": round(h, 2), "rot": rot, "kind": kind, "chars": chars,
            "fonts": fonts, "image_cover": round(cover, 3), "scan_ppi": scan_ppi,
            "columns": detect_columns(lines), "rtl_lines": sum(l["rtl"] for l in lines),
            # ページの一部を占める画像（写真や図）の数と、テキストレイヤの数学用の文字（𝑥 など）の数
            "images": sum(1 for i in imgs if i["drawn_pt"][0] * i["drawn_pt"][1] / (w * h) < SCAN_COVER_NO_TEXT),
            "math_chars": len(re.findall("[\U0001D400-\U0001D7FF]", text)),
            "scripts": dict(sorted(C.script_counts(text).items(), key=lambda kv: -kv[1])[:6]),
            "png": f"pages/{name}.png",
        }

    with ThreadPoolExecutor(max_workers=max(1, a.j)) as ex:
        list(ex.map(lambda t: render(pdf, t[0], t[1], t[2]), todo))
    for n in pages:
        by_n[n]["png_size"] = png_size(C.work_path(work, by_n[n]["png"]))

    manifest["pages"] = [by_n[k] for k in sorted(by_n)]
    C.save_manifest(work, manifest)
    sheets = make_sheets(work, manifest["pages"])

    print(f"{pdf}\n{npages} pages; prepared {len(pages)}; rendered {len(todo)}; work dir {work}")
    if info.get("Title"):
        print(f"Title (metadata): {info['Title']}")
    print(f"{'page':>5} {'kind':10} {'chars':>6} {'cols':>4} {'rtl':>4} {'imgs':>4} {'math':>4}  scripts / notes")
    for n in pages:
        p = by_n[n]
        note = ", ".join(f"{k} {v}" for k, v in p["scripts"].items())
        if p["rot"]:
            note += f"; rotated {p['rot']}"
        if p["scan_ppi"]:
            note += f"; scan {p['scan_ppi']} ppi"
        if p["kind"] == "digital" and not (p["images"] or p["math_chars"] or p["rtl_lines"]):
            note = "text only? " + note   # 図（線で描いたもの）と表は画像で確かめる
        print(f"{n:>5} {p['kind']:10} {p['chars']:>6} {p['columns']['count']:>4} {p['rtl_lines']:>4} "
              f"{p['images']:>4} {p['math_chars']:>4}  {note}")
    print("overview sheets (4 pages each): " + " ".join(str(x) for x in sheets))


if __name__ == "__main__":
    main()
