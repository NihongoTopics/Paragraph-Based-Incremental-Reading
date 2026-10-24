// TeX を MathML にする（MathJax 3 の mathjax-full を node で使う）。tex2mml.py が呼ぶ。
// 入力（stdin）: JSON [[tex, display], ...]。出力（stdout）: JSON [{m: MathML} か {x: 例外の文}, ...]。
// mathjax-full は setup.py が入れる（NODE_PATH で渡す）。Anki に同梱の MathJax と同じ 3.2.2。
// パッケージ: AllPackages から noundefined を外す（入れたままだと、定義されていない命令が誤りにならず、
// 赤い文字の mtext になる）。bussproofs も外す（出力の jax が要り、MathML だけを作るここでは全部の式が
// 誤りになる）。noerrors は残し、TeX の誤りは merror（data-mjx-error）で返す。
const {mathjax} = require('mathjax-full/js/mathjax.js');
const {TeX} = require('mathjax-full/js/input/tex.js');
const {liteAdaptor} = require('mathjax-full/js/adaptors/liteAdaptor.js');
const {RegisterHTMLHandler} = require('mathjax-full/js/handlers/html.js');
const {AllPackages} = require('mathjax-full/js/input/tex/AllPackages.js');
const {SerializedMmlVisitor} = require('mathjax-full/js/core/MmlTree/SerializedMmlVisitor.js');
const {STATE} = require('mathjax-full/js/core/MathItem.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const tex = new TeX({packages: AllPackages.filter((p) => p !== 'noundefined' && p !== 'bussproofs')});
const doc = mathjax.document('', {InputJax: tex});
const visitor = new SerializedMmlVisitor();

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', () => {
  const out = JSON.parse(input).map(([t, display]) => {
    try {
      const node = doc.convert(t, {display: !!display, end: STATE.CONVERT});
      return {m: visitor.visitTree(node, doc)};
    } catch (e) {
      return {x: String((e && e.message) || e)};
    }
  });
  process.stdout.write(JSON.stringify(out));
});
