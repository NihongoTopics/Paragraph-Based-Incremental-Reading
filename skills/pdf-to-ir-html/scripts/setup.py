#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdf-to-ir-html の依存物を調べ、足りないものを用意する。最初に 1 回（環境が変わったらもう一度）動かす。

使い方: python3 setup.py [--render]

  --render  プレビューを画像にする道具（playwright と Chromium）も用意する（check.py --render に要る）

調べるもの:
  必須   poppler（pdfinfo, pdftoppm, pdftotext, pdfimages, pdffonts）、Python 3.9 以上、bs4、Pillow
  数式   node と mathjax-full（TeX を MathML にする。無ければ数式は画像にするしかない）
  任意   tesseract と言語（スキャンの下書き。無ければ画像から読む）、gs（図の候補。無ければ別の方法で探す）
用意するもの（CACHE は ~/.cache/pdf-to-ir-html、環境変数 PDF_IR_CACHE で変えられる）:
  - この Python に bs4 か Pillow（--render では playwright も）が無ければ、CACHE/venv に venv を作って pip で入れる
    （--system-site-packages。元の Python に入っているものはそのまま使う）
  - mathjax-full（Anki に同梱の MathJax と同じ 3.2.2）を CACHE/node に npm で入れる
  - --render: playwright の Chromium か、この機械の Chrome / Chromium を探す。無ければ playwright install chromium
結果を CACHE/env.json に書き、最後の行に使う Python を「PY=...」の形で出す。以後のスクリプトはこの PY で動かす。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

REQUIRED_TOOLS = ["pdfinfo", "pdftoppm", "pdftotext", "pdfimages", "pdffonts"]
HINT = {"poppler": "macOS: brew install poppler / Debian, Ubuntu: apt-get install poppler-utils",
        "tesseract": "macOS: brew install tesseract tesseract-lang / Debian, Ubuntu: apt-get install tesseract-ocr "
                     "tesseract-ocr-jpn ...（言語ごと）",
        "gs": "macOS: brew install ghostscript / Debian, Ubuntu: apt-get install ghostscript",
        "node": "https://nodejs.org か、macOS: brew install node / Debian, Ubuntu: apt-get install nodejs npm"}
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
CHROME_NAMES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"]


def has_modules(python: str, modules: list) -> list:
    """python で読み込めないモジュールの一覧。"""
    code = ("import importlib.util as u, sys; "
            f"print(','.join(m for m in {modules!r} if u.find_spec(m) is None))")
    r = subprocess.run([python, "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        return modules
    return [m for m in r.stdout.strip().split(",") if m]


def python_env(render: bool) -> str:
    """bs4 と Pillow（と playwright）のある Python のパス。この Python に無ければ venv を作って入れる。"""
    if sys.version_info < (3, 9):
        raise SystemExit(f"Python 3.9 or later is needed (this is {sys.version.split()[0]})")
    pkgs = {"bs4": "beautifulsoup4", "PIL": "pillow"}
    if render:
        pkgs["playwright"] = "playwright"
    if not has_modules(sys.executable, list(pkgs)):
        return sys.executable
    venv = C.CACHE / "venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        print(f"creating {venv}")
        C.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)])
    missing = has_modules(str(py), list(pkgs))
    if missing:
        names = [pkgs[m] for m in missing]
        print(f"installing into {venv}: {' '.join(names)}")
        r = subprocess.run([str(py), "-m", "pip", "install", "--quiet", *names], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"pip install failed:\n{r.stderr[-2000:]}")
    still = has_modules(str(py), list(pkgs))
    if still:
        raise SystemExit(f"still missing after pip install: {still}")
    return str(py)


def mathjax() -> dict:
    """node と mathjax-full。戻り値 {"node": パス, "node_modules": パス} か、使えない理由 {"error": ...}。"""
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node:
        return {"error": "node is not installed (" + HINT["node"] + ")"}
    prefix = C.CACHE / "node"
    pkg = prefix / "node_modules" / "mathjax-full" / "package.json"
    version = None
    if pkg.exists():
        version = json.loads(pkg.read_text(encoding="utf-8")).get("version")
    if version != C.MATHJAX_VERSION:
        if not npm:
            return {"error": "npm is not installed, so mathjax-full cannot be installed (" + HINT["node"] + ")"}
        print(f"installing mathjax-full@{C.MATHJAX_VERSION} into {prefix}")
        prefix.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([npm, "install", "--prefix", str(prefix), f"mathjax-full@{C.MATHJAX_VERSION}",
                            "--no-audit", "--no-fund", "--silent"], capture_output=True, text=True)
        if r.returncode != 0 or not pkg.exists():
            return {"error": "npm install mathjax-full failed: " + (r.stderr or r.stdout)[-1000:]}
    return {"node": node, "node_modules": str(prefix / "node_modules")}


def find_browser(py: str) -> dict:
    """playwright で開ける Chromium。戻り値 {"chrome": "bundled" か実行ファイル} か {"error": ...}。"""
    probe = r'''
import sys
from playwright.sync_api import sync_playwright
cands = sys.argv[1:]
with sync_playwright() as p:
    for exe in [None] + cands:
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
            b.close()
            print(exe or "bundled")
            break
        except Exception:
            continue
'''
    cands = [c for c in CHROME_CANDIDATES if Path(c).exists()]
    cands += [shutil.which(n) for n in CHROME_NAMES if shutil.which(n)]
    for attempt in (1, 2):
        r = subprocess.run([py, "-c", probe, *cands], capture_output=True, text=True)
        found = r.stdout.strip()
        if found:
            return {"chrome": found}
        if attempt == 1:
            print("no Chromium found for playwright; running: playwright install chromium")
            subprocess.run([py, "-m", "playwright", "install", "chromium"], capture_output=True, text=True)
    return {"error": "no browser for playwright (playwright install chromium failed, and no Chrome/Chromium found)"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true", help="also set up playwright and a Chromium for check.py --render")
    a = ap.parse_args()
    C.CACHE.mkdir(parents=True, exist_ok=True)
    env = {"cache": str(C.CACHE)}
    problems = []

    missing = [t for t in REQUIRED_TOOLS if not shutil.which(t)]
    print("poppler:   " + ("ok" if not missing else f"MISSING {' '.join(missing)} ({HINT['poppler']})"))
    if missing:
        problems.append("poppler")

    py = python_env(a.render)
    env["python"] = py
    print(f"python:    ok ({py})")

    mj = mathjax()
    env.update({k: v for k, v in mj.items() if k != "error"})
    print("mathjax:   " + ("ok (mathjax-full " + C.MATHJAX_VERSION + ")" if "error" not in mj
                          else "NOT AVAILABLE: " + mj["error"] + " -> formulas can only be kept as images"))

    tess = shutil.which("tesseract")
    langs = []
    if tess:
        r = subprocess.run([tess, "--list-langs"], capture_output=True, text=True)
        langs = [l.strip() for l in r.stdout.splitlines()[1:] if l.strip()]
    env["tesseract_langs"] = langs
    print("tesseract: " + (f"ok ({len(langs)} languages: {' '.join(langs[:12])}{' ...' if len(langs) > 12 else ''})"
                           if tess else f"not installed (optional; scanned pages are read from the image) ({HINT['tesseract']})"))

    env["gs"] = bool(shutil.which("gs"))
    print("gs:        " + ("ok" if env["gs"] else "not installed (optional; regions.py uses the text layer instead)"))

    if a.render:
        br = find_browser(py)
        env.update({k: v for k, v in br.items() if k != "error"})
        print("render:    " + (f"ok ({br['chrome']})" if "error" not in br else "NOT AVAILABLE: " + br["error"]))
    else:
        print("render:    not set up (run setup.py --render to render previews with check.py --render)")

    with open(C.CACHE / "env.json", "w", encoding="utf-8") as f:
        json.dump(env, f, ensure_ascii=False, indent=1)
    if problems:
        raise SystemExit("required tools are missing: " + ", ".join(problems))
    print(f"PY={py}")


if __name__ == "__main__":
    main()
