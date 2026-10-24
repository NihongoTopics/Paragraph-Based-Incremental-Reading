# -*- coding: utf-8 -*-
"""pdf-to-ir-html のスクリプトが共有するもの。

どのスクリプトも、setup.py が示す Python（PY）で動かす。要るのは Python 3.9 以上、bs4、Pillow。
TeX の変換には node と mathjax-full、プレビューの画像には playwright と Chromium（setup.py が用意する）。
アドオンと Anki は読まない。取り込みの規則はスキルの中の写し（ir_rules.py）を使う。
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # スキルのフォルダに __pycache__ を作らない

import json
import os
import re
import subprocess
import unicodedata
from pathlib import Path

# setup.py が依存物（venv、mathjax-full、env.json）を入れる場所。PDF_IR_CACHE で変えられる
CACHE = Path(os.environ.get("PDF_IR_CACHE") or Path.home() / ".cache" / "pdf-to-ir-html")
MATHJAX_VERSION = "3.2.2"   # Anki 26.09.2 に同梱の MathJax と同じ版

# アドオンの取り込みダイアログは HTML のファイルを Readability（0.6.0）に通す。Readability が本文から外す属性
# （Readability.js の PRESENTATIONAL_ATTRIBUTES と _cleanStyles。svg の中は外さない）。class も page 以外を外す（_cleanClasses）
READABILITY_STRIPS = ("align", "background", "bgcolor", "border", "cellpadding", "cellspacing", "frame", "hspace",
                      "rules", "style", "valign", "vspace", "class")
READABILITY_SIZE_ELEMS = ("table", "th", "td", "hr", "pre")   # width と height も外す（DEPRECATED_SIZE_ATTRIBUTE_ELEMS）


def env() -> dict:
    """setup.py が書いた CACHE/env.json（無ければ空）。"""
    try:
        with open(CACHE / "env.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def need(module: str, pip_name: str):
    """Python のモジュールを読み込む。無ければ setup.py を案内して止まる。"""
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        raise SystemExit(f"Python package {pip_name} is missing for {sys.executable}: "
                         f"run setup.py and use the Python it prints (PY)")


# ---------------------------------------------------------------- 作業フォルダ

def pname(n: int) -> str:
    """ページ番号（1 から）をファイル名の部品にする。p0001, p0002, ..."""
    return f"p{n:04d}"


def work_path(work, *parts) -> Path:
    return Path(work).joinpath(*parts)


def load_manifest(work) -> dict:
    with open(work_path(work, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def save_manifest(work, m: dict) -> None:
    p = work_path(work, "manifest.json")
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def parse_pages(spec, npages: int) -> list:
    """'1-3,7' のような指定をページ番号のリストにする。None か 'all' なら全ページ。"""
    if spec in (None, "", "all"):
        return list(range(1, npages + 1))
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a) if a else 1
            b = int(b) if b else npages
            out.extend(range(a, b + 1))
        else:
            out.append(int(part))
    bad = [n for n in out if n < 1 or n > npages]
    if bad:
        raise SystemExit(f"page out of range (1-{npages}): {bad}")
    return sorted(set(out))


def run(cmd: list, check: bool = True, text: bool = True, **kw) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=text, **kw)
    except FileNotFoundError:
        raise SystemExit(f"{cmd[0]} is not installed: run setup.py to see what is missing and how to install it")
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(map(str, cmd))}\n{err}")
    return r


# ---------------------------------------------------------------- 文字の扱い

# 語を空白で区切らない文字。1 文字を 1 語として比べる。
_NO_SPACE_RANGES = [
    (0x3000, 0x303F),   # CJK の記号と句読点（、。「」など）
    (0x3040, 0x30FF),   # ひらがな、カタカナ
    (0x31F0, 0x31FF),   # カタカナ拡張
    (0x3400, 0x4DBF),   # CJK 統合漢字拡張 A
    (0x4E00, 0x9FFF),   # CJK 統合漢字
    (0xF900, 0xFAFF),   # CJK 互換漢字
    (0xFF00, 0xFFEF),   # 全角の英数字と記号（，！？など）
    (0x20000, 0x3134F),  # CJK 統合漢字拡張 B 以降
    (0x0E00, 0x0E7F),   # タイ文字
    (0x0E80, 0x0EFF),   # ラオ文字
    (0x1000, 0x109F),   # ミャンマー文字
    (0x1780, 0x17FF),   # クメール文字
    (0x0F00, 0x0FFF),   # チベット文字
]


def is_no_space_char(ch: str) -> bool:
    o = ord(ch)
    return any(a <= o <= b for a, b in _NO_SPACE_RANGES)


_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = dict.fromkeys(map(ord, "‘’‚‛′"), "'")
_QUOTES.update(dict.fromkeys(map(ord, "“”„‟″"), '"'))


def norm_text(s: str) -> str:
    """比べるための正規化。NFKC（合字 ﬁ → fi など）、小文字、引用符とダッシュの統一、
    ソフトハイフンの除去、空白の統一。"""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("­", "")
    s = s.translate(_DASHES).translate(_QUOTES)
    s = s.lower()
    return " ".join(s.split())


_WORD = re.compile(r"\w+", re.UNICODE)


def tokens(s: str) -> list:
    """比べるための語の列。空白で区切らない文字は 1 文字ずつ、ほかは \\w+ の単位。"""
    out = []
    for m in _WORD.finditer(norm_text(s)):
        w = m.group(0)
        buf = ""
        for ch in w:
            if is_no_space_char(ch):
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
        if buf:
            out.append(buf)
    return out


def script_counts(s: str) -> dict:
    """文字の用字ごとの数（Unicode の名前の先頭の語で分ける。LATIN, CJK, HIRAGANA, ARABIC など）。"""
    counts: dict = {}
    for ch in s:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        key = name.split(" ")[0]
        counts[key] = counts.get(key, 0) + 1
    return counts
