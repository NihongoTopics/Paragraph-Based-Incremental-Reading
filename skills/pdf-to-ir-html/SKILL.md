---
name: pdf-to-ir-html
description: PDF を、Anki のアドオン Paragraph-Based Incremental Reading（メニューは PBIR）で取り込める HTML 1 ファイルに変換する手順。スキャン（OCR の文字あり・なし、OCR が正確・不正確）、複数の言語と用字、図・画像・グラフ・表・樹形図・数式、2 段組を含む PDF に使う。「PDF を IR に入れたい」「この PDF を IR で読めるようにして」「この論文を IR に取り込めるようにして」と言われたとき、PDF の記事や論文や本の章を PBIR → Import で取り込みたいときに使う。このアドオンと関係の無い PDF の変換には使わない。
---

# PDF を Paragraph-Based Incremental Reading 用の HTML にする

PDF から、アドオン Paragraph-Based Incremental Reading が取り込める HTML ファイルを 1 本作る。利用者はそれを PBIR → Import ... → Choose File で選んで取り込む。
文字はテキストレイヤか OCR の結果を下書きにし、構造（段落、見出し、読む順、図、表、数式、脚注、言語）は Claude が決める。

トークンを使うのは、画像を見ることと、文字を書き直すこと。そこで、文字の正しいデジタルのページは下書きをその場で直すだけにし、
画像は一覧の画像と、手のかかるページでだけ見る。手のかかるページの手順は `references/complex-pages.md` にあり、
そういうページがあるときだけ読む。

## 出力の形

出力は、アドオンの取り込みが何も外さず、形も変えない HTML にする取り込みで残らないものは書かない。
理由と細かい規則は `references/markup.md` と `references/figures-tables-math.md`。

- アドオンは取り込む HTML のファイルを Mozilla Readability に通して本文を取り出し、題を h1 として前に置いてから、
  文書の直下の p とリスト（ul, ol, dl）を 1 個ずつカードにする。リストは項目ごとでなく全体で 1 枚。見出し（h1〜h6）、pre、blockquote（中の p も）、図、表はカードにならない。
  見出しを p で書くと、見出しがカードになる。見出しは必ず h1〜h6 で書く。
  本文の段落は p。脚注は aside.pdf-footnote の中の p で書く。assemble.py が脚注を、合印（本文の `<sup>`）のある段落のすぐ後へ移し、
  aside を外して本文の段落と並べる（カードになる）。
  題の周りの著者や日付は `<p>` で 1 行ずつ書く（カードになる）。
- 取り込みは style と class の属性と、Readability が表示用とする属性（table の border など）を外す。だから書かない。
  書体、色、表の罫線、小型大文字、数式の枠のような見た目は出力に無い。class を書くのは aside.pdf-footnote（assemble.py が外す）だけ。
  印刷どおりに番号を書いたリストは `type="none"` でブラウザの番号を消す（type は取り込みで残る。markup.md の 5）。
- 画像は data URI で HTML の中に入れる。数式は TeX で書き、assemble.py が MathML にする。
- 出力は `<article lang="…">` 1 個だけ（`<html>`, `<head>`, `<title>` は取り込みで使われないので書かない）。各ページの最初の要素の中に、そのページの番号を持つ空の
  `<span data-pdf-page="N" data-page-label="印刷されたページ番号">` を入れる（assemble.py がする）。
  この span は表示に出ず、どの文がどのページから来たかを残す。段落の間には入れない
  （入れると、そこにカードでないブロックができ、アドオンの PBIR → Merge Previous / Merge Next が使えなくなる）。

## 用意

このスキルは Anki にもアドオンにも頼らない。macOS でも Linux でも、Claude Code でも claude.ai でも動く。

- `S` はこの SKILL.md のあるフォルダの `scripts`（Claude Code では `${CLAUDE_SKILL_DIR}/scripts`）。
- 最初に `python3 $S/setup.py` を動かす。最後の行の `PY=…` が、以後のスクリプトを動かす Python（`PY`）。
  poppler が無いと動かない（setup.py が入れ方を示す。入れられなければ止める）。
  数式があれば node が要る。tesseract（スキャン）、gs（図の候補）、プレビューの画像（`--render`）は無くても進められる。
- 作業フォルダ `WORK` は書き込める一時の場所に `<PDF の名前>-ir/` として作る（Claude Code ならセッションの scratchpad、
  無ければ `/tmp` の下）。
- 出力は、利用者が別に指定しなければ PDF と同じフォルダの `<PDF の名前>.html`。そこに書けない環境
  （claude.ai では `/mnt/user-data/outputs`）では出力用のフォルダに書いて、ファイルとして渡す。
  同じ名前のファイルがあれば、上書きしてよいか聞く。

## 手順

### 1 下調べ

```
$PY $S/prepare.py PDF WORK
```

ページごとの種類（kind）、段組、画像の数（imgs）、数学用の文字の数（math）、用字を表にし、4 ページずつを 1 枚にした一覧の画像
`pages/sheet-NN.png` を作る。1 ページずつの画像ではなく、この一覧を見る。

- kind: `digital`（ふつうの文字）、`scan-ocr`（スキャンに OCR の文字）、`scan-text`（スキャンに別の方法の文字）、
  `scan`（画像だけ）、`no-text`（文字も大きな画像も無い）。
- 各ページを「簡単なページ」と「手のかかるページ」に分ける。簡単なページは、kind が `digital` で（表に `text only?` と出る）、
  一覧の画像で図、写真、グラフ、表、数式が見えないページ。それ以外は手のかかるページで、`references/complex-pages.md` に従う。
- 文書の主な言語を決める（手順 4 の `--lang`）。

### 2 簡単なページ: 下書きを清書としてその場で直す

```
$PY $S/draft.py WORK --pages 1-6 --frag
```

`draft/pNNNN.html` と同じものが `frag/pNNNN.html` にできる。`frag/pNNNN.html` を読み、書き直さずに、要るところだけを直す
（Claude Code なら Edit、ほかでも部分だけを書き換える道具）。文字はテキストレイヤのものなので打ち直さない。規則は `references/markup.md`。

- 見出し: draft.py が字の大きさと太字から見出しを見つけ、`<h1>`〜`<h6>` にしてある（いちばん大きい字の見出しが 1 つだけなら h1、
  ほかは大きさの順に h2 から）。一覧の画像と見比べて、見出しなのに `<p>` のものを `<h2>` などに直し、見出しでないのに
  `<h…>` のもの（題の周りの著者の行など）を直す。題の周りの著者や日付は `<p>` にする。
- 印を見て直す: `<!--cont?-->`（前の段落の続き。前の段落とつなぐ）、`<!--hy?-->`（行末のハイフン。残すか取るか）、
  `<!--lang? …-->`（`lang` を付ける）、`dir="rtl"`。`<!-- furniture: … -->`（柱とページ番号）は何もしなくてよい。
- 前のページから続く段落の先頭に `data-pdf-cont`、脚注は aside（ページの下に書いたままでよい。markup.md の 9）、箇条書きは ol/ul の li にする。
- 下書きだけで構造が分からないときに限り、そのページの画像 `pages/pNNNN.png` を見る。
- 照合（compare.py）は要らない（文字を写しただけなので）。

### 3 手のかかるページ

`references/complex-pages.md` を読み、それに従う（OCR、拡大して読む、図の切り出し、数式、照合）。

### 4 組み立て

```
$PY $S/assemble.py WORK OUT.html --lang en
```

誤り（TeX の誤り、段落の中のブロック、figure の外の img など）があれば一覧を出して止まる。`frag/` を直して組み立て直す。

### 5 点検

```
$PY $S/check.py OUT.html --work WORK
```

アドオンの取り込みの規則の写し（`scripts/ir_rules.py`）に通してカードを数え、警告を出す。

- 警告を一つずつ確かめる。「a heading became a card」と「looks like a heading」は、見出しがカードになっている警告なので、
  見出しなら必ず `<h1>`〜`<h6>` に直す。「removed on import」と「removed or changed on import」の誤りは、取り込みで外れるか形が変わるものを書いたという意味なので、
  `frag/` から消すか書き方を替える。ほかに、短すぎるカード、同じ文のカード、続きらしいカード、どのカードにも入らない文字、
  言語と向きの属性の不足など。
  カードの一覧 `WORK/check/cards.txt` は、警告のあるページのところだけ見ればよい。
- テキストレイヤの語のうち出力に入った割合が低いページは、抜けを探す。
- `visual check: worth doing …` と出たときだけ、`python3 $S/setup.py --render` のあと `--render` を付けて動かし、
  `WORK/check/preview-NN.png` で数式、表、図、右から左の文字をページの画像と見比べる。`not needed` なら画像は作らない。
- 直すときは `frag/` を直し、手順 4 からやり直す。出力の HTML を直接直さない。

## してはいけないこと

- 読めない文字を推測で埋める。読めた部分だけで式や語を作る。
- 本文を要約する、言い換える、並べ替える、補う。誤植を直す。
- 図の中の文字（目盛り、凡例、節点のラベル）を段落にする。
- 出力の HTML を手で直す（直すのは `frag/`）。
- プレビューや、ほかの環境で確かめたことを、実機の Anki で確かめたと書く。
