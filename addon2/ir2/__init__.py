# -*- coding: utf-8 -*-
"""Paragraph-Based Incremental Reading: Anki のアドオン。

  core.py       aqt に依存しない層（文書、ノート、テンプレート）。GUI なしで pylib (anki) だけで動作確認できる。
  card.js       カードのテンプレートに入れる JS（文書の表示、段落の編集）。card.css はカードの CSS。
  reviewer.py   学習画面との接続（カードの JS からの pycmd の受け口、評価の前後の処理）と PBIR メニュー。
  documents.py  表示中のカードの文書に対するメニューの操作と New Deck。
  importer.py   取り込みダイアログと Check Duplicates。
  widgets.py    取り込みダイアログとメニューの両方が使う部品。
"""

from aqt import gui_hooks, mw

from . import core, reviewer

# プロファイルを開くたびに、アドオンのノートタイプのテンプレートと CSS を今の内容にする。Anki はアドオンを読み込んでから
# プロファイルを開き、そのあとで main_window_did_init を呼ぶ（26.09.2 の aqt/main.py の AnkiQt.__init__ と on_window_init）ので、
# ここで登録する
gui_hooks.profile_did_open.append(lambda: core.update_models(mw.col))
gui_hooks.main_window_did_init.append(reviewer.setup)
