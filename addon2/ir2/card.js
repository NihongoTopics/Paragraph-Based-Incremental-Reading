// カードのテンプレートに入れる JS（文書の表示、段落の編集、PBIR メニューの操作）。core._doc_js が読み、印（__ で挟んだ大文字の名前）を
// core.py の値に置き換えてテンプレートに入れる。
//
// Python 側のメニューは window._ir2Cmd を呼ぶ。段落のカードの JS は状態（面、Create で問いと答えを書いている間か、
// Source Text か、前後の段落と結合できるか）を ir2:state で Python に知らせる。cloze のカードと quiz のカード
// （Basic、True or False、Multiple Choice、Enumeration）は表示だけで、メニューの操作は受けない。
//
// 注意: この JS はカードテンプレートにそのまま入るので、Anki のテンプレート記法として解釈される。コメントの中でも
// 二重の波括弧（開き括弧 2 個）を書いてはいけない。書くとフィールドの中身に置き換わり、中身に改行があると // コメントから
// 外れた行が JS の構文エラーになって、スクリプト全体が動かない（2026-09-23 に起きた）。core._doc_js が確かめる。
(function () {
  var root = document.getElementById('ir-root');
  if (!root || root.dataset.irInit) return;
  root.dataset.irInit = '1';
  var docid = root.dataset.doc, anchor = root.dataset.anchor, side = root.dataset.side;   // side は q（問題面）か a（回答面）
  var para = anchor.split('.')[0];
  // cloze カードかどうかは属性の有無で決める。Anki は Text に該当の cloze が無いと
  // cloze フィルタの結果を空で描画する。中身の真偽で決めると、そのとき段落カードとして扱い、
  // 文書のマークに色が付いただけの表示になり、編集までできてしまう。
  var isCloze = 'clozeHtml' in root.dataset;
  var clozeQ = isCloze ? root.dataset.clozeHtml : null;
  // quiz のカードかどうかも属性の有無で決める。テンプレートが data-qa に {kind, fields} を書く（core._qa_template）
  var isQuiz = 'qa' in root.dataset;
  var spec = isQuiz ? JSON.parse(root.dataset.qa) : null;
  // 段落のカードの問題面だけが編集でき、回答面は編集できない
  var editable = !isCloze && !isQuiz && side === 'q';
  // 出している問いと答え（openQA の戻り値）。quiz のカードと、段落のカードで Create → Basic などを開いている間
  var quiz = null;
  // ブロックの要素と中身に数える要素（core.py の BLOCK_TAGS と MEDIA_TAGS）、quiz の種類（core.py の QUIZ）
  var BLOCK_TAGS = __BLOCK_TAGS__, MEDIA = __MEDIA_TAGS__.join(', '), QUIZ_KINDS = __QUIZ_KINDS__;
  var BLOCK_SELECTOR = BLOCK_TAGS.join(', ');
  function isBlock(n) { return n.nodeType === 1 && BLOCK_TAGS.indexOf(n.nodeName.toLowerCase()) >= 0; }
  // 文字か、画像などの要素を持つか（core._has_content と同じ）
  function hasContent(n) { return /\S/.test(n.textContent) || !!(n.querySelector && n.querySelector(MEDIA)); }
  function unwrap(n) {
    while (n.firstChild) n.parentNode.insertBefore(n.firstChild, n);
    n.parentNode.removeChild(n);
  }

  // 文書を読み、記事を描画して編集の準備をする。カードを出したときと、Extract と Merge のあと
  // （sendStructural）に呼ぶ。Extract と Merge のあとに Anki の描画し直し（reviewer の
  // _redraw_current_card）を使わないのは、それが下の欄の時計を 0 に戻し、音声を流し直し、
  // 自動で答えへ進む待ち時間を始め直すため（2026-10-09）。
  render();
  function render() {
    return loadDoc().then(function (html) {
      // 読み込み中に次の描画（回答画面など）で差し替えられていたら何もしない。
      // 古い段落に window._ir2Cmd が向くのを防ぐ。
      if (!document.body.contains(root)) return;
      var doc = document.createElement('div');
      doc.id = 'ir-doc';
      doc.innerHTML = html;
      var target = doc.querySelector('[data-ir="' + para + '"]');
      if (!target) {                     // 文書と索引の食い違い。フォールバックのまま
        fallbackReason('paragraph ' + para + ' was not found in the document');
        return;
      }
      // 薄くする側を列挙しない。文書全体が既定で薄く、対象だけを戻す（CSS 参照）。
      target.classList.add('ir-focus');
      // 段落のカードの問題面では cloze のマークに色を付けない（CSS 参照）。ほかの出現の色も付けない（setupEdit）
      if (editable) doc.classList.add('ir-hide-cloze');
      // 対象の前後に続く、カードでないブロック（見出し、図など）は薄化を打ち消す。
      // 連続している限り辿り、次のカードの div に当たったところで止める。
      // 見出しや figure が対象の段落と一緒に読めるようにするため。
      ['previousElementSibling', 'nextElementSibling'].forEach(function (dir) {
        var el = target[dir];
        while (el && !el.hasAttribute('data-ir')) {
          el.classList.add('ir-near');
          el = el[dir];
        }
      });
      var warn = null;
      if (isCloze && clozeQ) {
        // cloze カード: 段落の中身を Anki が描画した cloze 表示に差し替える
        target.innerHTML = clozeQ;
        hideFallback();
      } else if (isCloze) {
        // Text に cloze が無い。段落はそのまま見せ、そのことを知らせる
        warn = warnBox('This cloze card has no cloze in its Text field. ' +
          'It is removed after you answer it, or remove it with Tools \u2192 Empty Cards.');
      } else if (isQuiz) {
        // quiz のカード: 問題面は段落の中身を隠して問いだけを出す。回答面は問いと答えを段落の中身の前に出す
        if (side === 'q') target.innerHTML = '';
        quiz = openQA(target, spec.kind, side, spec.fields);
        hideFallback();
      }
      root.innerHTML = '';
      if (warn) root.appendChild(warn);
      root.appendChild(doc);
      centerTarget(doc, target);
      setupEdit(target);
    }).catch(function (err) {
      // フォールバック表示のまま。黙って落ちると原因が分からないので、理由を画面に出す
      fallbackReason(String(err && err.message || err));
    });
  }

  // 対象の段落を画面の中央に出し、文書の高さが変わるたびに中央に出し直す（2026-10-31）。
  // 記事の画像の多くは大きさが書かれておらず、読み込みは中央に出したあとに終わる。Chromium の scroll anchoring は
  // 画面の上の方にある要素の位置を保つので、その要素と対象の間の画像が読み込まれると対象が下へ押される。
  // 回答面と cloze、quiz のカードでは、画面の上端と対象の間の画像の高さの分だけ下にずれた（Chromium で確かめた）。
  // 段落のカードの問題面では、フォーカスのある編集中の段落を Chromium が基準に選ぶので上の画像ではずれないが、
  // 段落の中の画像が読み込まれると段落が伸び、中央からずれた。
  // 出し直すのは、利用者が自分で動かす（ホイール、タッチ、キー、クリック）か、PBIR のメニューを使うか、
  // 次の描画に替わるまで。そのあとは読んでいる位置を動かさない
  function centerTarget(doc, target) {
    if (window._ir2StopCenter) window._ir2StopCenter();
    target.scrollIntoView({block: 'center'});
    if (!window.ResizeObserver) return;
    var USER_EVENTS = ['wheel', 'touchstart', 'keydown', 'mousedown'];
    var ro = new ResizeObserver(function () {
      if (document.body.contains(target)) target.scrollIntoView({block: 'center'});
      else stop();
    });
    function stop() {
      ro.disconnect();
      USER_EVENTS.forEach(function (t) { window.removeEventListener(t, stop, true); });
      if (window._ir2StopCenter === stop) window._ir2StopCenter = null;
    }
    USER_EVENTS.forEach(function (t) { window.addEventListener(t, stop, true); });
    window._ir2StopCenter = stop;
    ro.observe(doc);
  }

  // 文書を出せずにフィールドだけの表示（フォールバック）になったとき、その理由を上に出す
  // テンプレートの器（Anki が描画した cloze か、問いと答えのフィールド）。文書を出せたら隠す
  function fallbackBox() { return document.getElementById('ir-cloze-q') || document.getElementById('ir-qa-fb'); }
  function hideFallback() { var fb = fallbackBox(); if (fb) fb.style.display = 'none'; }
  function fallbackReason(text) {
    if (!document.body.contains(root)) return;
    var at = fallbackBox() || root;
    at.parentNode.insertBefore(warnBox('PBIR: the article could not be shown (' + text + ').'), at);
  }

  function warnBox(text) {
    var w = document.createElement('div');
    w.className = 'ir-warn';
    w.textContent = text;
    return w;
  }

  // 保存の直後の描画（回答面や同じ記事の次のカード）が編集の前の文書を受け取らないように、キャッシュを使わない。
  // Anki の media server は URL のパスだけでファイルを決める（26.09.2 の aqt/mediasrv.py の flask の route）ので、
  // 付けたクエリ文字列は読むファイルを変えない
  function loadDoc() {
    var url = '_ir2_' + docid + '.html';
    return fetch(url + '?t=' + Date.now(), {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' for ' + url);
      return r.text();
    });
  }

  // Source Text のときは、表示している文字（段落の HTML のソース）をそのまま送る。
  // Python は保存したあとの段落の HTML を返す（after に渡す）
  function save(target, after) {
    if (!sourceMode) marksFromStyles(target);
    pycmd('ir2:save:' + docid + ':' + para + ':' +
          encodeURIComponent(sourceMode ? target.textContent : target.innerHTML), after || null);
  }

  function setupEdit(target) {
    // reviewer は同じ webview をカード間で使い回すので、前のカードのリスナとコマンドを外す
    if (window._ir2SelHandler) document.removeEventListener('selectionchange', window._ir2SelHandler);
    window._ir2SelHandler = null;
    window._ir2Cmd = null;
    if (window.CSS && CSS.highlights) CSS.highlights.delete('ir-cloze');
    // cloze のカード（Anki の cloze 描画を含む）と quiz のカードは表示だけ。メニューの操作を受けず、状態も知らせない
    // （Python はメニューの段落の操作を灰色にしたままにする）
    if (isCloze || isQuiz) return;
    if (side === 'a') watchClozes(target);   // cloze の語の出現の色は回答面だけ
    // PBIR → Font のチェックを選択に合わせる
    window._ir2SelHandler = function () {
      if (document.body.contains(root)) pushFont(target);   // 次のカードに替わったあとは何もしない
    };
    document.addEventListener('selectionchange', window._ir2SelHandler);
    window._ir2Cmd = function (cmd, arg) {
      // メニューの操作（Create で問いの欄を出すなど）で段落の高さが変わっても、中央に出し直さない（centerTarget）
      if (window._ir2StopCenter) window._ir2StopCenter();
      if (quiz) return;   // Create → Basic などを開いている間。段落の操作はしない（quiz を作るのはカードを離れるとき）
      if (cmd === 'source') { setSource(target, !!arg); return; }
      // 以下は描画された段落に対する操作。PBIR → Create は回答面だけ、ほかは問題面だけ
      var create = cmd === 'cloze' || QUIZ_KINDS.indexOf(cmd) >= 0;
      var why = sourceMode ? ' is not available while Source Text is on'
              : create === editable ? ' is available only on the ' + (create ? 'back' : 'front') + ' side' : null;
      if (why) { notify(cmd.charAt(0).toUpperCase() + cmd.slice(1) + why); return; }
      trimSel(target);
      if (cmd === 'cloze') makeCloze(target);
      else if (create) openCreate(target, cmd);
      else if (cmd === 'highlight') toggleHighlight(target);
      else if (cmd === 'extract') extract(target);
      else if (cmd === 'merge') merge(target, arg);
      else if (FORMATS.indexOf(cmd) >= 0) format(target, cmd);
    };
    if (editable) {
      // キャレットを段落に置く（キーは文字の入力になる。枠は Chromium の既定のフォーカスの輪で、フォーカスのある間だけ出る）
      target.setAttribute('contenteditable', 'true');
      target.focus({preventScroll: true});
      // 段落からフォーカスが外れるたびに保存する（ページの中の別の所へ移ったときも、メニュー操作や別ウィンドウへの切り替えで
      // ページごとフォーカスを失ったときも）。編集は続く
      target.addEventListener('blur', function () {
        if (!structBusy && !sourceBusy) save(target);
      });
      // Anki 本体の js/webview.js（qt/aqt/data/web/js/webview.ts）は、ページ戻りを防ぐため
      // document の keydown で Backspace (keyCode 8) を preventDefault する。例外は
      // event.target が INPUT / TEXTAREA / DIV のときだけ。アドオンの editing host は div なので
      // 例外に入るが、host で伝播を止めて、document のリスナまで届かせない。
      target.addEventListener('keydown', function (ev) {
        if (target.isContentEditable && ev.keyCode === 8) ev.stopPropagation();
      });
      target.addEventListener('paste', onPaste);
    }
    // Source Text はカードをまたいで続く（Python が覚えている）。入っていればこのカードでも切り替える
    pushState(target, function (r) { if (r && r.source) setSource(target, true, true); });
  }

  // 貼り付け（段落と、問いと答えの欄）。Chromium はコピー元の計算済みの見た目（色、フォントなど）を style 属性として
  // クリップボードの HTML に書き込み、貼り付け先と違う分を残す。薄くしてある段落から
  // コピーすると薄い色が残り、その部分は表示中の段落でも薄いままになる。
  // 貼り付けの HTML から style と、この文書の印（data-ir, data-cloze）を外してから入れる。
  // insertHTML は Anki の外の編集と同じく取り消し（Cmd+Z）の対象になる。
  function onPaste(ev) {
    if (!ev.target.isContentEditable || !ev.clipboardData || sourceMode) return;  // Source Text は文字のまま
    var html = ev.clipboardData.getData('text/html');
    if (!html) return;  // 平文だけのときはブラウザに任せる
    ev.preventDefault();
    document.execCommand('insertHTML', false, cleanPasted(html));
  }

  var PASTE_DROP_ATTRS = ['style', 'class', 'id', 'data-ir',
                          'data-cloze', 'contenteditable'];
  function cleanPasted(html) {
    var tpl = document.createElement('template');
    tpl.innerHTML = html;
    var junk = tpl.content.querySelectorAll(__UNSAFE_TAGS__.concat('title').join(', '));
    for (var i = 0; i < junk.length; i++) junk[i].parentNode.removeChild(junk[i]);
    var els = tpl.content.querySelectorAll('*');
    for (var j = 0; j < els.length; j++) {
      PASTE_DROP_ATTRS.forEach(function (a) { els[j].removeAttribute(a); });
      // スクリプトを動かしうる属性（Python の sanitize と同じ規則。保存時にも外す）
      for (var k = els[j].attributes.length - 1; k >= 0; k--) {
        var at = els[j].attributes[k];
        if (/^on/i.test(at.name) ||
            /(javascript|vbscript):/i.test(at.value.replace(/[\x00-\x20]/g, '')))
          els[j].removeAttribute(at.name);
      }
    }
    // 貼り付ける先はふつうカードの中の p で、p の中にブロックは入れられない。そのまま渡すと Chromium が
    // span に置き換えて色を書き込むので、ブロックは外して間に改行を入れる
    var blocks = tpl.content.querySelectorAll(BLOCK_SELECTOR);
    for (var b = blocks.length - 1; b >= 0; b--) {
      var bl = blocks[b];
      if (bl.nextSibling && /\S/.test(bl.textContent))
        bl.parentNode.insertBefore(document.createElement('br'), bl.nextSibling);
      unwrap(bl);
    }
    // 属性の無くなった span は意味を持たないので中身だけにする
    var spans = tpl.content.querySelectorAll('span');
    for (var k = 0; k < spans.length; k++) if (!spans[k].attributes.length) unwrap(spans[k]);
    return tpl.innerHTML;
  }

  var FORMATS = ['bold', 'italic', 'underline', 'subscript', 'superscript'];

  // Python 側のメニュー（チェックと灰色表示）のための状態通知。段落のカードだけが送る。
  // creating は、Create → Basic などで問いと答えを書いている間
  function pushState(target, after) {
    pycmd('ir2:state:' + docid + ':' + para + ':' + JSON.stringify({
      side: side,
      creating: !!quiz,
      source: sourceMode,
      prev: !!adjacentPara(target, 'prev'),
      next: !!adjacentPara(target, 'next')
    }), after || null);
    pushFont(target);
  }

  // PBIR → Font のチェックのための状態通知。普通のエディタと同じく、選択範囲（キャレットだけのときはその位置）に
  // 付いている装飾にチェックを付ける。判定は付け外しと同じ基準にする。
  //   Bold など: queryCommandState。Chromium（main の style_commands.cc）では、状態の StateStyle と
  //   付け外しの ExecuteToggleStyle が同じ判定（Mac は選択の始まり、ほかは選択全体に付いているか）を使う。
  //   キャレットだけで Bold を押したあとの「次に打つ文字は太字」も含む。
  //   Highlight: toggleHighlight と同じく、選択範囲の文字が全部 <mark> の中か。キャレットだけなら <mark> の中か。
  //   Create → Cloze（回答面だけ）: makeCloze が cloze を外すときと同じく、選択範囲の文字が全部 cloze の箇所にあるか。
  //   回答面の段落は編集できずキャレットが見えないので、キャレットだけのときは付けない。
  // 段落の中に選択が無いとき（段落の外のキャレットや選択、Source Text の間、問いと答えを出している間）は、どれにもチェックを付けない。
  // 前に送ったものと同じときは送らない（selectionchange のたびに呼ぶため）。
  var lastFont = null;
  function pushFont(target) {
    var st = {highlight: false, bold: false, italic: false, underline: false, subscript: false, superscript: false, cloze: false};
    var sel = window.getSelection();
    var range = !sourceMode && !quiz && sel.rangeCount && cardRange(sel.getRangeAt(0), target);
    if (range) {
      st.highlight = sel.isCollapsed ? inMark(range.startContainer, target) : fullyMarked(range, target);
      for (var i = 0; i < FORMATS.length; i++)
        st[FORMATS[i]] = document.queryCommandState(FORMATS[i]);
      st.cloze = side === 'a' && !sel.isCollapsed && !!clozesInSelection(range, target);
    }
    var s = JSON.stringify(st);
    if (s === lastFont) return;
    lastFont = s;
    pycmd('ir2:font:' + docid + ':' + para + ':' + s);
  }

  // PBIR → Source Text。段落の HTML のソースを文字として表示する（文書に書かれているとおりの HTML を
  // Python から受け取る）。問題面では、そのまま文字として編集できる（contenteditable="plaintext-only"。
  // 打った文字も貼り付けも文字のまま入る）。切り替えるときは、問題面では今の中身を保存してから、
  // 保存された HTML（Python が整えたもの）で表示し直す。saved はカードを出したとき（中身は文書のまま）
  var sourceMode = false, sourceBusy = false;
  function setSource(target, on, saved) {
    if (on === sourceMode || sourceBusy) { pushState(target); return; }
    var done = function (stored) {
      sourceBusy = false;
      if (typeof stored !== 'string') { notify('Source Text failed'); pushState(target); return; }
      sourceMode = on;
      target.classList.toggle('ir-source', on);
      if (on) target.textContent = stored;
      else target.innerHTML = stored;
      if (editable) {
        target.setAttribute('contenteditable', on ? 'plaintext-only' : 'true');
        if (!saved) target.focus();
      }
      pushState(target);
    };
    sourceBusy = true;
    if (editable && !saved) save(target, done);
    else pycmd('ir2:source:' + docid + ':' + para + ':', done);
  }

  // 直前か直後の兄弟がカードの div ならそれ。結合できなければ null（core._adjacent_para と同じ。間は空白とコメントだけ。
  // メニューの灰色表示に使う。最終の判定は Python）
  function adjacentPara(target, dir) {
    var step = dir === 'prev' ? 'previousSibling' : 'nextSibling', n = target[step];
    while (n && (n.nodeType === 8 || (n.nodeType === 3 && !/\S/.test(n.data)))) n = n[step];
    return n && n.nodeType === 1 && n.hasAttribute('data-ir') ? n : null;
  }

  // PBIR → Extract。問題面だけ。選択の始まりと終わりに印の span を入れて中身を読み、印は外す。
  // Python が、選んでいない前と後ろを新しい段落にする（core.extract_paragraph）。
  function extract(target) {
    var bad = selectionProblem(target);
    if (bad) { notify('Extract failed: ' + bad); return; }
    marksFromStyles(target);
    var range = window.getSelection().getRangeAt(0);
    // 終わりから入れる。終わりに入れても始まりの位置は動かない
    var marks = ['end', 'start'].map(function (pos) {
      var m = document.createElement('span'), at = range.cloneRange();
      m.setAttribute('data-ir-split', pos);
      at.collapse(pos === 'start');
      at.insertNode(m);
      return m;
    });
    var html = target.innerHTML;
    marks.forEach(function (m) { m.parentNode.removeChild(m); });
    target.normalize();
    sendStructural(target, 'extract', '', html);
  }

  // PBIR → Merge Previous / Merge Next。今の中身も送り、先に保存させる。
  function merge(target, dir) {
    if (!adjacentPara(target, dir)) {
      notify('Merge failed: there is no paragraph ' + (dir === 'prev' ? 'right before' : 'right after') +
             ' this one with nothing in between');
      return;
    }
    marksFromStyles(target);
    sendStructural(target, 'merge', dir, target.innerHTML);
  }

  // 送る前に編集できなくする。そうしないと、描画し直すときの blur で Extract・結合の前の中身が
  // 保存され、結果を上書きする。成功したら文書を読み直して描画し直す（render）。断られたら編集できる状態に戻す。
  var structBusy = false;
  function sendStructural(target, cmd, arg, html) {
    structBusy = true;
    target.removeAttribute('contenteditable');
    pycmd('ir2:' + cmd + ':' + docid + ':' + para + ':' + (arg ? arg + ':' : '') +
          encodeURIComponent(html), function (res) {
      structBusy = false;
      if (res === 'ok') { render(); return; }
      if (editable) { target.setAttribute('contenteditable', 'true'); target.focus(); }
      pushState(target);
    });
  }

  // 段落の操作（_ir2Cmd）は今の選択に対して働く。選択が段落の外にはみ出していて、その部分に文字が無ければ、段落の中だけの
  // 選択にする（cardRange）。メニューを開いても選択とフォーカスはページに残る（2026-10-22、Anki 26.09.2 で確かめた）
  // 選択を置き直すのは cardRange が範囲を縮めたときだけ。置き直すと、execCommand('bold') などがキャレットに付けた装飾が
  // 消えるので、キャレットで Bold をもう一度選んでも外れず、また付いてしまう（2026-10-30、QtWebEngine 6.11.2 で確認）
  function trimSel(target) {
    var sel = window.getSelection(), range = sel.rangeCount && sel.getRangeAt(0);
    var r = range && cardRange(range, target);
    if (r && r !== range) { sel.removeAllRanges(); sel.addRange(r); }
  }

  // 選択のうち段落の外にはみ出した部分に文字が無ければ（空白だけで、画像なども無ければ）、段落の中だけの範囲を返す。
  // 段落の最後の行を 3 回クリックしたときや、最後の行の先まで引いて選んだとき、選択の終わりは次の段落の先頭に来る。
  // はみ出した部分に文字があれば null。段落の中にある選択はそのまま返す。
  function cardRange(range, target) {
    if (target.contains(range.commonAncestorContainer)) return range;
    function empty(a) { return !hasContent(a.cloneContents()); }
    var r = range.cloneRange(), outer = document.createRange(), a;
    outer.selectNodeContents(target);
    if (r.compareBoundaryPoints(Range.START_TO_START, outer) < 0) {
      a = document.createRange(); a.setStart(r.startContainer, r.startOffset); a.setEnd(target, 0);
      if (!empty(a)) return null;
      r.setStart(target, 0);
    }
    if (r.compareBoundaryPoints(Range.END_TO_END, outer) > 0) {
      a = document.createRange(); a.setStart(target, target.childNodes.length); a.setEnd(r.endContainer, r.endOffset);
      if (!empty(a)) return null;
      r.setEnd(target, target.childNodes.length);
    }
    return r.collapsed ? null : r;
  }

  // cloze の span が分割・複製されたり中身が動いたりしていないかを見るための指紋
  function clozeSig(target) {
    var cs = target.querySelectorAll('[data-cloze]'), a = [];
    for (var i = 0; i < cs.length; i++)
      a.push(cs[i].getAttribute('data-cloze') + '\u0000' + cs[i].textContent);
    return a.sort().join('\u0001');
  }

  // 数式（<math>）の中に要素が入ったり、数式が割れたりしていないかを見るための指紋
  function mathSig(target) {
    var ms = target.querySelectorAll('math'), a = [];
    for (var i = 0; i < ms.length; i++) a.push(ms[i].outerHTML);
    return a.join('\u0001');
  }

  // Bold, Italic, Underline, Subscript, Superscript。付け外しの判定はブラウザの execCommand に任せる
  // （普通のエディタと同じく、全部に付いていれば外し、そうでなければ付ける）。
  // 装飾を外すときにブラウザが祖先の要素を分割することがあり、cloze の span が
  // 二つに割れると id が重複する。そうなったら元に戻して断る。
  // 数式（<math>）もブラウザの装飾で壊れる。数式を丸ごと選んでも、ブラウザは <b> などを数式の中に入れる
  // （Bold で分数の 2 つの項が <b> に包まれた。2026-09-26、QtWebEngine 6.11.2 で確認）。
  // そこで数式の形が変わったら、cloze と同じく元に戻して断る。
  function format(target, cmd) {
    var before = target.innerHTML, sig = clozeSig(target), msig = mathSig(target);
    document.execCommand(cmd, false, null);
    var why = clozeSig(target) !== sig ? 'it would split an existing cloze'
            : mathSig(target) !== msig ? 'it would put formatting inside a formula (select only text outside formulas)'
            : null;
    if (why) {
      target.innerHTML = before;
      notify('Format failed: ' + why);
    }
    pushFont(target);
  }

  // 編集中に Chromium が <mark> を、線の色の背景を書いた span に書き換えることがある（線の行の先頭で Backspace を
  // 押して前の行とつないだときなど。2026-10-19、アプリ内の Chromium で確認）。画面では線に見えるが <mark> ではないので
  // Highlight の判定に入らず、保存の後始末（core.clean_styles）がアドオンの色として背景を外すので、文書では線が消える。
  // そこで保存と、線、cloze、Extract、結合の前に、背景が線の色の span を <mark> に戻す。span のほかの style は残す。
  // 選択（キャレット）の位置は保つ。
  function rgbKey(v) {
    v = (v || '').trim().toLowerCase();
    var m = /^#([0-9a-f]{6})$/.exec(v);
    if (m) return [0, 2, 4].map(function (i) { return parseInt(m[1].substr(i, 2), 16); }).join(',');
    m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(v);
    if (m && (m[4] === undefined || parseFloat(m[4]) === 1)) return m[1] + ',' + m[2] + ',' + m[3];
    return null;
  }
  function marksFromStyles(target) {
    var doc = target.closest('#ir-doc');
    var color = doc && rgbKey(getComputedStyle(doc).getPropertyValue('--ir-mark'));
    if (!color) return;
    var spans = Array.prototype.filter.call(target.querySelectorAll('span[style]'), function (s) {
      return rgbKey(s.style.backgroundColor) === color;
    });
    if (!spans.length) return;
    var sel = window.getSelection(), at = null;
    if (sel.rangeCount) {
      var r = sel.getRangeAt(0);
      at = [r.startContainer, r.startOffset, r.endContainer, r.endOffset];
    }
    spans.forEach(function (s) {
      var m = document.createElement('mark');
      s.parentNode.insertBefore(m, s);
      m.appendChild(s);
      s.style.removeProperty('background-color');
      if (!s.style.length) s.removeAttribute('style');
      if (!s.attributes.length) {   // 中身を mark へ移す。選択の端が span にあれば mark に付け替える（位置は同じ）
        unwrap(s);
        if (at && at[0] === s) at[0] = m;
        if (at && at[2] === s) at[2] = m;
      }
    });
    // 線の中の線は外す（線を引くときと同じ）
    var inner = target.querySelectorAll('mark mark');
    for (var i = 0; i < inner.length; i++) {
      var n = inner[i], p = n.parentNode, k = Array.prototype.indexOf.call(p.childNodes, n), len = n.childNodes.length;
      // 選択の端の位置を、中身を外へ出したあとの位置に直す
      for (var e = 0; at && e < 4; e += 2) {
        if (at[e] === p && at[e + 1] > k) at[e + 1] += len - 1;
        else if (at[e] === n) { at[e] = p; at[e + 1] += k; }
      }
      unwrap(n);
    }
    if (at && document.contains(at[0]) && document.contains(at[2])) {
      var nr = document.createRange();
      nr.setStart(at[0], at[1]); nr.setEnd(at[2], at[3]);
      sel.removeAllRanges(); sel.addRange(nr);
    }
  }

  // 選択の境界が <mark> の途中にあると、extractContents が中身の無い <mark> を残す
  function removeEmptyMarks(target) {
    var ms = target.querySelectorAll('mark');
    for (var i = 0; i < ms.length; i++)
      if (!ms[i].textContent && !ms[i].querySelector(MEDIA + ', br')) ms[i].parentNode.removeChild(ms[i]);
  }

  // node が <mark> か、その中にあるか（段落の中だけを見る）
  function inMark(node, target) {
    for (var n = node; n && n !== target; n = n.parentNode)
      if (n.nodeName === 'MARK') return true;
    return false;
  }

  // 選択範囲の中の文字（空白だけの部分は除く）がすべて <mark> の中にあるか
  function fullyMarked(range, target) {
    var w = document.createTreeWalker(target, NodeFilter.SHOW_TEXT), n, any = false;
    while ((n = w.nextNode())) {
      if (!range.intersectsNode(n)) continue;
      var s = (n === range.startContainer) ? range.startOffset : 0;
      var e = (n === range.endContainer) ? range.endOffset : n.length;
      if (!/\S/.test(n.data.slice(s, e))) continue;
      any = true;
      if (!inMark(n, target)) return false;
    }
    return any;
  }

  // 選択範囲から <mark> を外す。選択の外にはみ出している部分は <mark> のまま残す。
  // 境界に目印の要素を差し込んでから作業し、最後に目印の間を選択し直す。
  function unmarkSelection(range, target) {
    var S = document.createElement('span'), E = document.createElement('span');
    var er = range.cloneRange(); er.collapse(false); er.insertNode(E);
    var sr = range.cloneRange(); sr.collapse(true); sr.insertNode(S);
    var r = document.createRange();
    r.setStartAfter(S); r.setEndBefore(E);
    var marks = Array.prototype.filter.call(target.querySelectorAll('mark'),
      function (m) { return r.intersectsNode(m); });
    // m の中の、選択の外の部分（範囲 r）を新しい <mark> に移して、at の前に置く
    function keepOutside(m, r, at) {
      var k = document.createElement('mark');
      k.appendChild(r.extractContents());
      if (k.textContent) m.parentNode.insertBefore(k, at);
    }
    marks.forEach(function (m) {
      if (!m.parentNode) return;
      var r;
      if (m.contains(S)) {
        r = document.createRange(); r.setStart(m, 0); r.setEndBefore(S);
        keepOutside(m, r, m);
      }
      if (m.contains(E)) {
        r = document.createRange(); r.setStartAfter(E); r.setEnd(m, m.childNodes.length);
        keepOutside(m, r, m.nextSibling);
      }
      unwrap(m);
    });
    var nr = document.createRange();
    nr.setStartAfter(S); nr.setEndBefore(E);
    S.parentNode.removeChild(S); E.parentNode.removeChild(E);
    removeEmptyMarks(target);
    target.normalize();
    var sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(nr);
  }

  // PBIR → Font → Highlight。選択範囲が全部 <mark> なら外し、そうでなければ付ける。
  // 選択は残す（続けて押せば元に戻る）。
  function toggleHighlight(target) {
    var sel = window.getSelection();
    // 線を外すとき（unmarkSelection）も、端に目印の要素を入れるので、数式の中の端は断る
    var bad = selectionProblem(target) || mathConflict(sel.getRangeAt(0));
    if (bad) { notify('Highlight failed: ' + bad); return; }
    marksFromStyles(target);
    var range = sel.getRangeAt(0), before = target.innerHTML, sig = clozeSig(target), r = 'ok';
    if (fullyMarked(range, target)) {
      try { unmarkSelection(range, target); }
      catch (err) { r = 'could not remove the highlight (' + err.name + ')'; }
    } else {
      r = wrapSelection(target, 'mark');
    }
    if (r === 'ok' && clozeSig(target) !== sig) {
      target.innerHTML = before;
      r = 'it would split an existing cloze';
    }
    if (r === 'ok') save(target);
    else notify('Highlight failed: ' + r);
    pushFont(target);
  }

  // 選択範囲と要素の位置関係を 4 つに分ける
  //   disjoint  … 重ならない
  //   contains  … 選択が要素を丸ごと含む
  //   inside    … 選択が要素の中に収まっている
  //   crossing  … 境界をまたぐ。包むと要素が分割される
  function relateToRange(range, node) {
    if (!range.intersectsNode(node)) return 'disjoint';
    var outer = document.createRange();
    outer.selectNode(node);
    if (range.compareBoundaryPoints(Range.START_TO_START, outer) <= 0 &&
        range.compareBoundaryPoints(Range.END_TO_END, outer) >= 0) return 'contains';
    var inner = document.createRange();
    inner.selectNodeContents(node);
    if (range.compareBoundaryPoints(Range.START_TO_START, inner) >= 0 &&
        range.compareBoundaryPoints(Range.END_TO_END, inner) <= 0) return 'inside';
    return 'crossing';
  }

  // op: 'mark' か 'cloze'。断る理由を返す。問題なければ null。
  //   どちらも crossing は断る。cloze の span を分割すると同じ id の span が
  //   二つできて、どちらが本体か決まらなくなるため。
  //   cloze はさらに contains と inside も断る。入れ子にすると
  //   「cloze 1 個につきノート 1 件、序数は常に c1」という前提が崩れるため。
  function clozeConflict(range, target, op) {
    var clozes = target.querySelectorAll('[data-cloze]');
    for (var i = 0; i < clozes.length; i++) {
      var rel = relateToRange(range, clozes[i]);
      if (rel === 'disjoint') continue;
      if (rel === 'crossing') return 'selection crosses the edge of an existing cloze';
      if (op === 'cloze') return 'clozes cannot be nested';
    }
    return null;
  }

  // 選択の端が数式（<math>）の中にあれば断る理由を返す。問題なければ null。
  // 端が数式の中にあると、線や cloze の要素（HTML）が MathML の中に入るか、数式が二つに割れて、
  // 数式が壊れる（2026-09-26、QtWebEngine 6.11.2 で確認）。両端が数式の外なら、選択にかかる数式は
  // 丸ごと含まれるので壊れない。
  function mathConflict(range) {
    var ends = [range.startContainer, range.endContainer];
    for (var i = 0; i < ends.length; i++) {
      var el = ends[i].nodeType === 1 ? ends[i] : ends[i].parentNode;
      if (el && el.closest && el.closest('math'))
        return 'the selection starts or ends inside a formula (select the whole formula, or only text outside it)';
    }
    return null;
  }

  function notify(text) {
    pycmd('ir2:note:' + docid + ':' + para + ':' + encodeURIComponent(text));
  }

  function hasBlock(node) {
    return node.nodeType === 1 && (isBlock(node) || !!node.querySelector(BLOCK_SELECTOR));
  }
  // 選択範囲を、ブロック（p、li など）の中の、ブロックを含まない部分ごとの範囲に分ける（文書の順）。
  // 印の要素がブロックを含まないように、線はこの範囲ごとに別の <mark> で包む。
  // 選択に文字の無い範囲（空白だけで、画像なども無いもの）は入れない。そのため、選択の終わりが次のブロックの先頭にある
  // とき（行を 3 回クリックしたときなど）は、そのブロックは選択に入らない。
  function blockParts(range, target) {
    var parts = [];
    function add(first, last) {
      var r = document.createRange();
      r.setStartBefore(first); r.setEndAfter(last);
      if (range.compareBoundaryPoints(Range.START_TO_START, r) > 0) r.setStart(range.startContainer, range.startOffset);
      if (range.compareBoundaryPoints(Range.END_TO_END, r) < 0) r.setEnd(range.endContainer, range.endOffset);
      if (hasContent(r.cloneContents())) parts.push(r);
    }
    (function walk(node) {
      var first = null, last = null;
      for (var n = node.firstChild; n; n = n.nextSibling) {
        if (!range.intersectsNode(n)) continue;
        if (hasBlock(n)) {
          if (first) add(first, last);
          first = last = null;
          walk(n);
        } else {
          first = first || n;
          last = n;
        }
      }
      if (first) add(first, last);
    })(target);
    return parts;
  }
  // 線と cloze で選択を使えなければ、その理由。使えれば null
  function selectionProblem(target) {
    var sel = window.getSelection();
    if (!sel.rangeCount || sel.isCollapsed) return 'nothing selected';
    if (!target.contains(sel.getRangeAt(0).commonAncestorContainer)) return 'selection is outside the current paragraph';
    return null;
  }
  // 選択範囲を線（op が mark。<mark>）か cloze の印（cloze。data-cloze が PENDING の span）で包む。戻り値は 'ok' か失敗の理由。
  // surroundContents は要素の境界をまたぐ範囲に InvalidStateError を投げるので使わない。extractContents + insertNode なら
  // またいだ要素を分割して包める。線は、中の線を外し、包んだ範囲を選んだままにする（続けて押せば元に戻る）
  function wrapSelection(target, op) {
    var bad = selectionProblem(target);
    if (bad) return bad;
    var sel = window.getSelection(), range = sel.getRangeAt(0);
    var parts = blockParts(range, target);
    if (!parts.length) return 'nothing selected';
    // cloze の印は 1 個なので、二つ以上のブロック（p、li など）にまたがる選択は断る。線はブロックごとに包む
    if (parts.length > 1 && op === 'cloze')
      return 'selection spans more than one paragraph or list item';
    var conflict = mathConflict(range) || clozeConflict(range, target, op);
    if (conflict) return conflict;
    var before = target.innerHTML, els = [];
    try {
      // 後ろの範囲から包む。後ろの範囲を包んでも、前の範囲の境界は動かない
      for (var i = parts.length - 1; i >= 0; i--) {
        var el = document.createElement(op === 'mark' ? 'mark' : 'span');
        if (op === 'cloze') el.setAttribute('data-cloze', 'PENDING');
        el.appendChild(parts[i].extractContents());
        parts[i].insertNode(el);
        els.unshift(el);
      }
    } catch (err) {
      target.innerHTML = before;
      return 'could not wrap the selection (' + err.name + ')';
    }
    if (op === 'mark') {
      els.forEach(function (e) { Array.prototype.forEach.call(e.querySelectorAll('mark'), unwrap); });
      removeEmptyMarks(target);
    }
    target.normalize();
    sel.removeAllRanges();
    if (op === 'mark') {
      var nr = document.createRange();
      nr.setStart(els[0], 0);
      nr.setEnd(els[els.length - 1], els[els.length - 1].childNodes.length);
      sel.addRange(nr);
    }
    return 'ok';
  }

  // cloze のマークは選んだ箇所に 1 個だけで、CSS はマークにだけ色を付ける。段落の中のほかの出現にも
  // 同じ色を付ける。文書には書かない（CSS Custom Highlight API。DOM を変えないので、保存、編集、
  // cloze の指紋に影響しない）。段落が変わるたび（編集、線、cloze の id の受け取り、Source Text）
  // に数え直す。数え方は Python の cloze_text と同じ:
  // 大文字と小文字を区別しない、語の中の空白は種類と数を問わない、語の途中は数えない（かなと漢字は数える）、
  // ほかの data-cloze のマークの境目はまたいでよい、それ以外の要素で割れたものと数式の中は数えない。
  var UNSPACED = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\u{20000}-\u{3ffff}]/u;
  function joins(a, b) {
    return [a, b].every(function (c) { return /^[\p{L}\p{N}]$/u.test(c) && !UNSPACED.test(c); });
  }
  function charBefore(s, i) {   // i の直前の 1 文字（サロゲートペアは 2 単位で 1 文字）
    var c = s.charCodeAt(i - 1);
    return (c >= 0xdc00 && c <= 0xdfff && i >= 2) ? s.slice(i - 2, i) : s.charAt(i - 1);
  }
  function charAt(s, i) { return String.fromCodePoint(s.codePointAt(i)); }
  // 続けて読むテキストノードの並び。Python はほかのマークを外してから隣り合う文字列をつなぐので、
  // ほかの data-cloze の span の境目では切らない。そのマーク自身、数式、ほかの要素、コメントでは切る。
  function textRuns(root, own) {
    var runs = [], cur = [];
    function flush() { if (cur.length) runs.push(cur); cur = []; }
    (function walk(node) {
      for (var n = node.firstChild; n; n = n.nextSibling) {
        if (n.nodeType === 3) cur.push(n);
        else if (n.nodeType !== 1 || n === own || n.nodeName.toLowerCase() === 'math' ||
                 n.classList.contains('ir-qa')) flush();   // 問いと答え（Create → Basic など）は段落の文ではない
        else if (n.hasAttribute('data-cloze')) walk(n);
        else { flush(); walk(n); flush(); }
      }
    })(root);
    flush();
    return runs;
  }
  function rangeIn(run, a, b) {
    var r = document.createRange(), pos = 0, started = false;
    for (var i = 0; i < run.length; i++) {
      var t = run[i], end = pos + t.data.length;
      if (!started && a < end) { r.setStart(t, a - pos); started = true; }
      if (started && b <= end) { r.setEnd(t, b - pos); break; }
      pos = end;
    }
    return r;
  }
  // cloze のマークごとの、語のほかの出現の範囲。[{mark, word, ranges}]。word は比べるための語（空白をまとめ、小文字にしたもの）
  function clozeHits(target) {
    var out = [], marks = target.querySelectorAll('[data-cloze]');
    for (var i = 0; i < marks.length; i++) {
      var own = marks[i], ranges = [];
      var words = own.textContent.split(/\s+/).filter(Boolean);
      if (!words.length) continue;
      var re = new RegExp(words.map(function (w) {
        return w.replace(/[.*+?^$()|[\]\\\/{}]/g, '\\$&');
      }).join('\\s+'), 'giu');
      textRuns(target, own).forEach(function (run) {
        var s = run.map(function (t) { return t.data; }).join(''), m;
        re.lastIndex = 0;
        while ((m = re.exec(s))) {
          var a = m.index, b = a + m[0].length;
          if ((a > 0 && joins(charBefore(s, a), charAt(s, a))) ||
              (b < s.length && joins(charBefore(s, b), charAt(s, b)))) continue;
          ranges.push(rangeIn(run, a, b));
        }
      });
      out.push({mark: own, word: words.join(' ').toLowerCase(), ranges: ranges});
    }
    return out;
  }
  // 選択範囲の中の文字（空白だけの部分は除く）がすべて cloze の箇所（マークか、その語のほかの出現）にあれば、
  // それらの箇所の cloze のマークと、同じ語のほかの cloze のマーク。そうでなければ null。判定は fullyMarked と同じ形
  function clozesInSelection(range, target) {
    var hits = clozeHits(target), found = [], any = false;
    var w = document.createTreeWalker(target, NodeFilter.SHOW_TEXT), n;
    function at(n, i) {
      var inAny = false;
      hits.forEach(function (h) {
        if (h.mark.contains(n) || h.ranges.some(function (r) { return r.isPointInRange(n, i) && r.isPointInRange(n, i + 1); })) {
          inAny = true;
          if (found.indexOf(h) < 0) found.push(h);
        }
      });
      return inAny;
    }
    while ((n = w.nextNode())) {
      if (!range.intersectsNode(n)) continue;
      var s = (n === range.startContainer) ? range.startOffset : 0;
      var e = (n === range.endContainer) ? range.endOffset : n.length;
      for (var i = s; i < e; i++) {
        if (!/\S/.test(n.data.charAt(i))) continue;
        any = true;
        if (!at(n, i)) return null;
      }
    }
    if (!any) return null;
    var words = found.map(function (h) { return h.word; });
    return hits.filter(function (h) { return words.indexOf(h.word) >= 0; }).map(function (h) { return h.mark; });
  }
  function showClozes(target) {
    if (!window.CSS || !CSS.highlights || typeof Highlight === 'undefined') return;
    if (!document.body.contains(target)) return;   // 次のカードに替わっている
    var h = new Highlight();
    clozeHits(target).forEach(function (hit) { hit.ranges.forEach(function (r) { h.add(r); }); });
    CSS.highlights.set('ir-cloze', h);
  }
  function watchClozes(target) {
    var queued = false;
    new MutationObserver(function () {
      if (queued) return;
      queued = true;
      setTimeout(function () { queued = false; showClozes(target); }, 0);
    }).observe(target, {childList: true, characterData: true, subtree: true,
                        attributes: true, attributeFilter: ['data-cloze']});
    showClozes(target);
  }

  // PBIR → Create → Cloze。選択範囲の文字がすべて cloze の箇所なら、その cloze を外す（removeClozes）。
  // そうでなければ、選択範囲を data-cloze の span で包む（入れ子とまたぎは断る）
  function makeCloze(target) {
    var sel = window.getSelection();
    var marks = !selectionProblem(target) && clozesInSelection(sel.getRangeAt(0), target);
    if (marks) { removeClozes(target, marks); return; }
    marksFromStyles(target);
    var r = wrapSelection(target, 'cloze');
    if (r !== 'ok') { notify('Cloze failed: ' + r); return; }
    // Python が採番した id を受け取って DOM 側の PENDING も置き換える。
    // 置き換えないと、この後の保存（線を引くなど）で PENDING が文書に戻る。
    pycmd('ir2:cloze:' + docid + ':' + para + ':' +
          encodeURIComponent(target.innerHTML), function (id) {
      var p = target.querySelector('[data-cloze="PENDING"]');
      if (!p) return;
      if (id) { p.setAttribute('data-cloze', id); return; }
      unwrap(p);  // 失敗時は外す
      target.normalize();
    });
  }

  // cloze のマークを外して（中身の文字は残す）段落を保存する。外した cloze のノートは、Text を作り直すと cloze が無くなり、
  // 評価のあとの後始末で消える（core.remove_empty）
  function removeClozes(target, marks) {
    var ids = marks.map(function (m) { return m.getAttribute('data-cloze'); });
    marks.forEach(unwrap);
    target.normalize();
    save(target, function () {
      notify('Cloze removed (' + ids.join(', ') + '). ' + (ids.length === 1 ? 'Its card is' : 'Their cards are') +
             ' deleted after you answer this card');
    });
    pushFont(target);
  }

  // ---- quiz（Basic、True or False、Multiple Choice、Enumeration）の問いと答え

  // 上から順の行を返す。行は {head: 文字}（決まった文字）、{hr: true}（線）、
  // {field: フィールド名, dim, placeholder}（フィールド。作るときは書ける）、{html, dim}（フィールドから作った表示だけの行）。
  // qa.side は q（問題面）、a（回答面）、new（Create で作るとき）。
  // True or False は、問題面に True と False のどちらかの文（出すたびに選ぶ。qaPick）を出し、回答面にその文の答え（True か
  // False）と両方の文を出す。作るときは回答面と同じ並びで、答えの代わりに問いの文字を出す。
  // Multiple Choice は、作るときは選択肢を全部 1 個の欄（Choices）で出し、問題面と回答面では問いと選択肢 2 個（mcPick）を出す。
  // Enumeration は、作るときは項目を全部 1 個の欄（Items）で出し、問題面と回答面では Anki が描画した cloze を出す。
  function qaRows(qa) {
    var creating = qa.side === 'new';
    if (qa.kind === 'tf') {
      if (creating) return [{head: 'True or False?'}, {field: 'True'}, {field: 'False', dim: true}];
      var t = qaPick(qa).t;
      if (qa.side === 'q') return [{head: 'True or False?'}, {field: t ? 'True' : 'False'}];
      return [{head: t ? 'True' : 'False'}, {field: 'True'}, {field: 'False', dim: true}];
    }
    if (qa.kind === 'mc') {
      if (creating)
        return [{field: 'Question'},
                {field: 'Choices', placeholder: 'Right choice on the first line, wrong choices on the following lines'}];
      var p = mcPick(qa), rows = [{field: 'Question'}];
      // 無い選択肢（Choices の行が足りないとき）は空として出す
      if (qa.side === 'q') (p.swap ? [p.wrong, p.right] : [p.right, p.wrong]).forEach(function (h) { rows.push({html: h}); });
      else rows.push({html: p.right}, {html: p.wrong, dim: true});
      return rows;
    }
    if (qa.kind === 'enum') {
      if (creating)
        return [{field: 'Question'}, {field: 'Items', placeholder: 'Items in order, one per line'}];
      // Items は Anki が描画した cloze（対象の項目は問題面で空欄、回答面で答え。ほかの項目はそのまま）。問題面では対象より後の項目を外す
      return [{field: 'Question'}, {html: qa.side === 'q' ? enumUpToTarget(qa.values.Items || '') : (qa.values.Items || '')}];
    }
    if (qa.side === 'q') return [{field: 'Question'}];
    return [{field: 'Question'}, {hr: true}, {field: 'Answer'}];
  }

  // Enumeration の問題面: Anki が描画した Items の cloze から、対象の項目（class="cloze"）より後を外す。
  // 行は <br> で区切られ、項目ごとに 1 個の span（Anki の cloze の描画。26.09.2 の pylib で確かめた）
  function enumUpToTarget(html) {
    var d = fromHtml(html), top = d.querySelector('.cloze');
    if (!top) return html;
    while (top.parentNode !== d) top = top.parentNode;
    while (top.nextSibling) d.removeChild(top.nextSibling);
    return d.innerHTML;
  }

  // html を中身にした div（文書には入れない）
  function fromHtml(html) {
    var d = document.createElement('div');
    d.innerHTML = html;
    return d;
  }

  // Multiple Choice の Choices を行に分ける。1 行目が正しい選択肢、2 行目からが誤りの選択肢。
  // 行の区切りは、いちばん外側の <br> とブロックの要素の境目。中身の無い行は数えない（core.py の choice_lines と同じ）
  function choiceLines(html) {
    var box = fromHtml(html), lines = [], cur = [];
    function end() {
      var d = fromHtml(cur.join(''));
      cur = [];
      if (hasContent(d)) lines.push(d.innerHTML.trim());
    }
    (function walk(node) {
      for (var n = node.firstChild; n; n = n.nextSibling) {
        if (n.nodeName === 'BR') end();
        else if (isBlock(n)) { end(); walk(n); end(); }
        else if (n.nodeType === 1) cur.push(n.outerHTML);
        else if (n.nodeType === 3) { var t = document.createElement('span'); t.textContent = n.data; cur.push(t.innerHTML); }
      }
    })(box);
    end();
    return lines;
  }

  // 出すたびに乱数で選ぶもの: True or False の t（True の文を出すか）、Multiple Choice の w（何番目の誤りの選択肢か）と
  // swap（並び）。回答面は問題面と同じものを出す必要があるが、Anki は問題面と回答面を別に描画するので、問題面で選んだものを
  // window._ir2Pick に置き、回答面で読む。学習画面のページは問題面と回答面で同じもの（Anki が中身だけを差し替える。
  // setupEdit の注記）。印は文書、段落、種類で、回答面で印が合わなければ（プレビューで回答面だけを出したときなど）回答面で選ぶ
  // （問題面はいつも選び直して置くので、別のカードの選んだものを読むのは、回答面だけを出したときに限られる）。
  function qaPick(qa) {
    if (!qa.pick) {
      var key = docid + '\u0001' + para + '\u0001' + qa.kind, st = window._ir2Pick;
      qa.pick = (qa.side === 'a' && st && st.key === key) ? st
              : {key: key, t: Math.random() < 0.5, w: Math.random(), swap: Math.random() < 0.5};
      if (qa.side === 'q') window._ir2Pick = qa.pick;
    }
    return qa.pick;
  }

  // Multiple Choice で出す 2 個: 正しい選択肢と、誤りの選択肢から乱数で選んだ 1 個（qaPick の w。0 以上 1 未満の数で、
  // 誤りの選択肢の数を掛けて何番目かにする）。問題面では並びも乱数で決める。
  function mcPick(qa) {
    var lines = choiceLines(qa.values.Choices || ''), wrongs = lines.slice(1), p = qaPick(qa);
    return {right: lines[0] || '', wrong: wrongs[Math.floor(p.w * wrongs.length)] || '', swap: p.swap};
  }

  // 対象の段落の要素の先頭に .ir-qa を入れ、qaRows の行を並べる。fields は {フィールド名: HTML}（作るときは空）。
  // 作るとき（side が new）はフィールドを書けるようにする。戻り値は {box, kind, side, values}
  function openQA(target, kind, side, fields) {
    var box = document.createElement('div');
    box.className = 'ir-qa';
    var qa = {box: box, kind: kind, side: side, values: fields};
    qaRows(qa).forEach(function (row) {
      var el = document.createElement(row.hr ? 'hr' : 'div');
      if (row.head) el.textContent = row.head;
      if (row.html !== undefined) el.innerHTML = row.html;
      if (row.field) {
        el.innerHTML = fields[row.field] || '';
        el.setAttribute('data-field', row.field);
        if (side === 'new') {
          el.setAttribute('contenteditable', 'true');
          el.setAttribute('data-placeholder', row.placeholder || row.field);
        }
      }
      if (row.dim) el.className = 'ir-qa-dim';
      box.appendChild(el);
    });
    target.insertBefore(box, target.firstChild);
    return qa;
  }

  // PBIR → Create → Basic / True or False / Multiple Choice / Enumeration（kind）。段落のカードの回答面で、作る欄を出し、
  // 最初の欄にフォーカスを置く。中身が変わるたびに、全部の欄の今の中身を下書きとして Python に送る。
  // Python はカードを離れるときに下書きから quiz のノートを作る（reviewer.py の _commit_draft）。何も書かなければ下書きが無く、
  // ノートは作らない。作ったあとのノートは学習画面では変えない
  function openCreate(target, kind) {
    quiz = openQA(target, kind, 'new', {});
    var els = quiz.box.querySelectorAll('[data-field]');
    quiz.box.addEventListener('input', function () {
      var fields = {};
      Array.prototype.forEach.call(els, function (el) { fields[el.getAttribute('data-field')] = el.innerHTML; });
      pycmd('ir2:draft:' + docid + ':' + para + ':' + encodeURIComponent(JSON.stringify({kind: kind, fields: fields})));
    });
    quiz.box.addEventListener('paste', onPaste);   // 段落と同じ
    els[0].focus();
    pushState(target);
  }
})();
