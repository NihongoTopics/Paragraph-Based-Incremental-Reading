#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TeX を MathML にする。MathJax 3.2.2（mathjax-full、Anki に同梱の MathJax と同じ版）を node で動かす（tex2mml.js）。

ライブラリ（assemble.py が使う）:
    convert([(tex, display), ...], work=None) -> [{"mathml": str, "error": str | None}, ...]
  node の起動は 1 回の呼び出しにつき 1 回。work（作業フォルダ）を渡すと、MathJax の出力を
  WORK/tex-cache.json に保存し、次から使う。
CLI（確認用）:
    tex2mml.py 'TEX' [--display] [--work WORK]
  MathML を表示する。TeX の誤りがあれば、その内容を stderr に出して終了コード 1。
node と mathjax-full は setup.py が用意する（CACHE/env.json）。

出力の MathML（2026-09-26、Anki の QtWebEngine 6.11.2（Chromium 140）で確かめたこと）:
- xmlns と data-mjx-* を外し、タグの間の空白（MathJax の字下げ）を詰める。mi, mn, mo, mtext, ms の中の文字はそのまま。
- アドオンの取り込みが外す属性（style と、Readability の表示用の属性 frame, align など。_common.READABILITY_STRIPS）
  は付けない（取り込みで残らないものは出力に書かない）。書体は Anki の既定の数式の書体になる。
- noundefined パッケージを外す。入れたままだと、定義されていない命令（\\foo）が誤りにならず、赤い
  文字 "\\foo" の mtext になる。外すと "Undefined control sequence \\foo" の merror になる。
- 式番号: \\tag{1} は mlabeledtr になり、QtWebEngine は番号を式の前（左）に出す。そこで \\tag{X} を
  \\qquad (X)、\\tag*{X} を \\qquad X に書き換えてから変換する（式の直後に番号が出る）。
- mathvariant: QtWebEngine は mathvariant="bold" などを無視する（\\mathbf{M} が細字、\\mathbb{R} が
  ふつうの R になる）。そこで mi, mn, mo, mtext, ms の文字を Unicode の数学用英数字記号
  （𝐌 U+1D40C, ℝ U+211D など）に置き換え、属性を外す。対応する記号の無い文字はそのまま残す。
  mathvariant="normal"（\\mathrm{d}）は効くので残す。
- menclose（\\boxed, \\cancel など）は QtWebEngine では枠も斜線も出ない。枠と斜線を style で描くことはしない
  （取り込みで style が外れるため）。menclose は MathJax の出力のまま残り、中の式だけが出る。
"""
import sys
sys.dont_write_bytecode = True   # before importing _common, so no __pycache__ is written
import _common as C

import argparse
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path


CACHE_NAME = "tex-cache.json"
TIMEOUT = 300           # node の変換の上限（秒）

BRIDGE = Path(__file__).resolve().parent / "tex2mml.js"

_TOKENS = {"mi", "mn", "mo", "mtext", "ms"}


# ---------------------------------------------------------------- TeX の書き換え

_TAG = re.compile(r"\\tag(\*?)\s*\{")


def rewrite_tags(tex: str) -> str:
    """\\tag{X} を \\qquad (X) に、\\tag*{X} を \\qquad X にする（括弧の対応を数えて X を取る）。"""
    out, pos = [], 0
    for m in _TAG.finditer(tex):
        if m.start() < pos:
            continue
        depth, i = 1, m.end()
        while i < len(tex) and depth:
            if tex[i] == "\\":
                i += 2
                continue
            depth += {"{": 1, "}": -1}.get(tex[i], 0)
            i += 1
        if depth:   # 閉じ括弧が無い。MathJax に誤りとして報告させる
            break
        label = tex[m.end():i - 1]
        out.append(tex[pos:m.start()])
        out.append(r"\qquad " + (label if m.group(1) else f"({label})"))
        pos = i
    out.append(tex[pos:])
    return "".join(out)


# ---------------------------------------------------------------- mathvariant → Unicode

def _alnum_map() -> dict:
    """{mathvariant: {文字: 数学用英数字記号}}。Unicode の Mathematical Alphanumeric Symbols の並びと、
    その穴を埋める Letterlike Symbols の文字（ℬ ℝ など）。"""
    latin = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [chr(c) for c in range(ord("a"), ord("z") + 1)]
    starts = {  # mathvariant: A の位置（52 文字が続く）
        "bold": 0x1D400, "italic": 0x1D434, "bold-italic": 0x1D468, "script": 0x1D49C,
        "bold-script": 0x1D4D0, "fraktur": 0x1D504, "double-struck": 0x1D538, "bold-fraktur": 0x1D56C,
        "sans-serif": 0x1D5A0, "bold-sans-serif": 0x1D5D4, "sans-serif-italic": 0x1D608,
        "sans-serif-bold-italic": 0x1D63C, "monospace": 0x1D670,
    }
    holes = {
        "italic": {"h": "ℎ"},
        "script": {"B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ", "L": "ℒ", "M": "ℳ", "R": "ℛ",
                   "e": "ℯ", "g": "ℊ", "o": "ℴ"},
        "fraktur": {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"},
        "double-struck": {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"},
    }
    table = {}
    for variant, start in starts.items():
        m = {ch: chr(start + i) for i, ch in enumerate(latin)}
        m.update(holes.get(variant, {}))
        table[variant] = m
    digits = {"bold": 0x1D7CE, "double-struck": 0x1D7D8, "sans-serif": 0x1D7E2,
              "bold-sans-serif": 0x1D7EC, "monospace": 0x1D7F6}
    for variant, start in digits.items():
        table[variant].update({str(d): chr(start + d) for d in range(10)})
    greek_upper = "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡϴΣΤΥΦΧΨΩ∇"
    greek_lower = "αβγδεζηθικλμνξοπρςστυφχψω∂ϵϑϰϕϱϖ"
    greek = {"bold": 0x1D6A8, "italic": 0x1D6E2, "bold-italic": 0x1D71C,
             "bold-sans-serif": 0x1D756, "sans-serif-bold-italic": 0x1D790}
    for variant, start in greek.items():
        table[variant].update({ch: chr(start + i) for i, ch in enumerate(greek_upper + greek_lower)})
    table["bold"].update({"Ϝ": "𝟊", "ϝ": "𝟋"})
    return table


_ALNUM = _alnum_map()


def _apply_mathvariant(el, variant: str) -> None:
    """el（字句の要素）の文字を variant の記号に替える。替えられない文字が残れば属性を残す。"""
    if variant == "normal":
        if el.tag == "mi" and el.text and len(el.text) == 1:
            return   # 1 文字の mi だけが既定で斜体になる。normal はそれを止める意味があるので残す
        del el.attrib["mathvariant"]
        return
    m = _ALNUM.get(variant)
    if m is None or not el.text:
        return
    el.text = "".join(m.get(ch, ch) for ch in el.text)
    if all(ch in m.values() or not ch.isalnum() for ch in el.text):
        del el.attrib["mathvariant"]


# ---------------------------------------------------------------- MathJax の出力の後処理

def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _clean(el, in_token: bool, errors: list) -> None:
    el.tag = _local(el.tag)
    for k in list(el.attrib):
        if k.startswith("data-mjx-"):
            if k == "data-mjx-error":
                errors.append(el.attrib[k])
            del el.attrib[k]
        elif _local(k) in C.READABILITY_STRIPS:
            del el.attrib[k]
    token = in_token or el.tag in _TOKENS
    if not token and el.text is not None and not el.text.strip():
        el.text = None
    for child in el:
        _clean(child, token, errors)
        if not token and child.tail is not None and not child.tail.strip():
            child.tail = None
    if el.tag in _TOKENS and "mathvariant" in el.attrib:
        _apply_mathvariant(el, el.attrib["mathvariant"])


def _attr(v: str) -> str:
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _serialize(el, out: list) -> None:
    attrs = "".join(f' {_local(k)}="{_attr(v)}"' for k, v in el.attrib.items())
    out.append(f"<{el.tag}{attrs}>")
    if el.text:
        out.append(html.escape(el.text, quote=False))
    for child in el:
        _serialize(child, out)
        if child.tail:
            out.append(html.escape(child.tail, quote=False))
    out.append(f"</{el.tag}>")


def postprocess(raw: str) -> tuple:
    """MathJax の MathML を HTML に入れる形にする。戻り値は (mathml, 誤りの文 | None)。"""
    root = ET.fromstring(raw)
    errors = []
    _clean(root, False, errors)
    out = []
    _serialize(root, out)
    return "".join(out), ("; ".join(errors) or None)


def error_math(tex: str, display: bool) -> str:
    """MathJax が例外を返したときの MathML（merror に TeX をそのまま入れる）。"""
    d = ' display="block"' if display else ""
    return (f'<math{d}><merror><mtext>{html.escape(tex, quote=False)}'
            f'</mtext></merror></math>')


# ---------------------------------------------------------------- node

def _node() -> tuple:
    """(node のパス, mathjax-full のある node_modules)。setup.py が env.json に書いたもの。"""
    e = C.env()
    if not e.get("node") or not e.get("node_modules"):
        raise SystemExit("tex2mml: node and mathjax-full are not set up: run setup.py")
    return e["node"], e["node_modules"]


def _run_mathjax(items: list) -> list:
    """[(tex, display)] → [{"m": 生の MathML} か {"x": 例外の文}]。"""
    node, modules = _node()
    r = C.run([node, str(BRIDGE)], input=json.dumps([[t, d] for t, d in items]),
              env={**os.environ, "NODE_PATH": modules}, timeout=TIMEOUT)
    return json.loads(r.stdout)


# ---------------------------------------------------------------- 変換

def _engine_id() -> str:
    return "mathjax-full-" + C.MATHJAX_VERSION


def _load_cache(work) -> dict:
    if work is None:
        return {}
    try:
        with open(C.work_path(work, CACHE_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data.get("raw", {}) if data.get("engine") == _engine_id() else {}


def _save_cache(work, raw: dict) -> None:
    p = C.work_path(work, CACHE_NAME)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"engine": _engine_id(), "raw": raw}, f, ensure_ascii=False)
    os.replace(tmp, p)


def convert(items, work=None) -> list:
    """[(tex, display)] → [{"mathml": str, "error": str | None}]（同じ順）。

    error は TeX の誤り（MathJax の merror の文か例外の文）。誤りがあっても mathml には merror を含む
    MathML が入る。
    """
    items = [(rewrite_tags(t), bool(d)) for t, d in items]
    key = [("D:" if d else "I:") + t for t, d in items]
    cache = _load_cache(work)
    todo = sorted({k: it for k, it in zip(key, items) if k not in cache}.items())
    raised = {}
    if todo:
        for (k, _), r in zip(todo, _run_mathjax([it for _, it in todo])):
            if "m" in r:
                cache[k] = r["m"]
            else:
                raised[k] = r["x"]
        if work is not None:
            _save_cache(work, cache)
    out = []
    for k, (tex, display) in zip(key, items):
        if k in raised:
            out.append({"mathml": error_math(tex, display), "error": raised[k]})
        else:
            mathml, err = postprocess(cache[k])
            out.append({"mathml": mathml, "error": err})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="TeX を MathML にする（確認用）。MathML を stdout に出す。")
    ap.add_argument("tex", help="TeX（$ で囲まない）")
    ap.add_argument("--display", action="store_true", help="別行立ての式として変換する")
    ap.add_argument("--work", help="作業フォルダ（tex-cache.json を使う）")
    a = ap.parse_args()
    t0 = time.time()
    r = convert([(a.tex, a.display)], a.work)[0]
    print(r["mathml"])
    print(f"({time.time() - t0:.1f} s)", file=sys.stderr)
    if r["error"]:
        print(f"error: {r['error']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
