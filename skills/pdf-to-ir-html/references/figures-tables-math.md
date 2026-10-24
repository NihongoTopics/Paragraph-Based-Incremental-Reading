# 図、表、数式の書き方

`markup.md` の続き（節の番号はそちらと通し）。図、写真、グラフ、樹形図、表、数式のあるページで読む。
理由は 2026-09-25 に Anki 26.09.2（QtWebEngine 6.11.2、Chrome 140）で確かめたものと、2026-10-15 に アドオンの取り込みの規則の写し（scripts/ir_rules.py）で確かめたもの。

## 6 図、画像、グラフ、樹形図

- 写真、グラフ、図解、樹形図、地図、組めない表（下の 7）は画像にする。
  ```
  <figure><img src="figs/p0003-1.png" alt="…"><figcaption>Figure 2. …</figcaption></figure>
  ```
- 画像は crop.py で PDF から切り出す（SKILL.md 手順 4）。範囲は図の中の文字（軸の目盛り、節点のラベル）を含め、
  図の番号と説明（キャプション）は含めない。キャプションは `<figcaption>` に文字で書く。
- `<img>` を `<p>` の中に入れず、`<figure>` の外にも書かない。
  理由: カードになる（core.segment は p を、中身が画像だけでもカードにする。文書の直下の裸の img は p に入れられる）。
  figure の中（figcaption を含む）はカードにならない。
- `alt` には何の図かを短く書く。樹形図は括弧の書き方でも書く（`[S [NP [D the] [N student]] [VP …]]`）。
  グラフは、図の中に書かれた数値があれば並べる。読めないものは書かない。
- `<img>` には `src` と `alt` 以外の属性を付けない。
  理由: 小さい画像で、ほかの属性に .png などのファイル名があると、取り込みで src が差し替わる（Readability の _fixLazyImages）。
- 図の中の文字を本文の段落に書き出さない（軸の数値、凡例、節点のラベルが段落になるとカードになる）。

## 7 表

- 表は HTML の表で書く。
  ```
  <table><caption>Table 1. …</caption>
  <thead><tr><th rowspan="2">…</th><th colspan="2">…</th></tr>…</thead>
  <tbody><tr><td>…</td>…</tr></tbody></table>
  ```
  罫線（border）は付けない（取り込みが外す。Readability の _cleanStyles）。結合したセルは rowspan と colspan（取り込みで残る）。
  セルが 1 個だけの表は、取り込みがセルの中身に替えるので、表にしない（Readability の _prepArticle）。
- セルの中の改行は `<br>`。セルの中の数式は `<tex>`。
- 表はカードにならない（core.segment）。
- 表として組めないもの（斜線で区切ったセル、セルの中の図）は、表全体を画像にして 6 の形で書き、
  表の番号と説明を `<figcaption>` に書く。

## 8 数式

- 数式は TeX で書き、`<tex>` で囲む。本文の中の式は `<tex>x_i</tex>`、別の行に出す式は `<tex display>…</tex>`。
  assemble.py が MathJax（Anki に同梱のものと同じ 3.2.2）で MathML に変える。
  理由: Anki の MathJax は、カードの JS があとから読み込む文書には効かない（reviewer.js と core._doc_js）。
  MathML は QtWebEngine がそのまま描画する。
- 別の行の式が文の一部なら、その段落の `<p>` の中に書き、式のあとの文（"where x is …"）も同じ `<p>` に続ける。
  新しい段落が式のあとで始まっていれば（字下げなど）、そこで `<p>` を分ける。
- 式の番号は TeX の中に `\tag{1}` と書く。tex2mml.py が式のすぐ後ろの `(1)` にする。
  理由: MathJax の \tag のままでは、QtWebEngine は番号を式の前（左）に出す（2026-09-26、tex2mml.py の作成時に確認）。
- TeX の中の `<` と `>` は `\lt` と `\gt` か、`&lt;` と `&gt;` で書く。`&`（行列の区切り）はそのまま書いてよい。
- 斜体の変数、上付きと下付き、分数、根号、総和などで組まれているものは `<tex>`。数式でない上付きと下付き
  （"CO<sub>2</sub>"、"1<sup>st</sup>"、脚注の番号）は `<sub>` と `<sup>`。単なる数と単位は文字のまま。
- 式が読めないときは、読めた部分だけで式を作らない。式全体を画像にして 6 の形で書く。
- tex2mml.py は数式に style を付けず、取り込みが外す属性も外す。書体は QtWebEngine の既定の数式の書体になる。
  `\boxed` や `\cancel` は menclose のまま残るが、QtWebEngine は枠も斜線も描かないので、中の式だけが出る。

## 13 ページの断片の例

2 段組のページの一部。前のページから続く段落、脚注、図、式を含む。
```
<!-- page-label: 102 -->
<p data-pdf-cont="space">such as the Greek capital A, … within it.</p>
<h2>3 Formal notation</h2>
<p>Inline mathematics is set in the same line as the surrounding text. For instance, the angles of a triangle
satisfy <tex>\alpha + \beta + \gamma = \pi</tex>, … numbered at the right margin.<sup>2</sup></p>
<aside class="pdf-footnote"><p><sup>2</sup> Equation numbers belong to the page layout; …</p></aside>
<p>The first displayed equation is a sum with limits above and below the summation sign:
<tex display>\sum_{i=1}^{n} i^2 = \frac{n(n+1)(2n+1)}{6} \tag{1}</tex></p>
<figure><img src="figs/p0002-1.png" alt="Bar chart: A 12.4, B 7.9, C 3.1, D 0.8 errors per page">
<figcaption>Figure 1. Mean reading-order errors per page for four hypothetical converters.</figcaption></figure>
```
