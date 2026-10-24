# Paragraph-Based Incremental Reading

An add-on for Anki (desktop) for reading long texts paragraph by paragraph. You import an article, and each paragraph becomes a card. While studying, you read the paragraph with the rest of the article around it, edit the article in place, and make Cloze, Basic, True or False, Multiple Choice and Enumeration cards from the paragraph. The add-on's menu in Anki is **PBIR**.

The article itself is kept once, in a document file in Anki's media folder. Every card shows that document and every edit changes it. Anki schedules the cards as usual; the add-on adds no scheduling of its own.

## Contents

- `addon2/ir2/` The add-on.
- `skills/pdf-to-ir-html/` A skill for Claude (Claude Code or claude.ai) that converts a PDF into an HTML file the add-on can import. Its instructions are in Japanese.

## Installing

Copy the folder `addon2/ir2` into Anki's add-ons folder (in Anki, Tools → Add-ons → View Files opens it) and restart Anki. The PBIR menu appears in the menu bar.

The add-on is developed with Anki 26.09.2 on macOS. It is made for the desktop version of Anki; showing its cards in AnkiDroid and AnkiMobile is not supported.

## Getting started

1. **PBIR → New Deck** creates a deck PBIR with subdecks Tier 1 to Tier 8 and Trash, and a deck options preset PBIR.
2. **PBIR → Import ...** imports a web page (URL), an HTML, plain-text or Markdown file. Choose a Tier as the deck.
3. Study the deck. On the front of a paragraph card you can edit the paragraph; on the back you can make cards from it (PBIR → Create).

## License

The add-on and the skill are free software: you can redistribute them and/or modify them under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. They are distributed in the hope that they will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See `LICENSE` for the full text.

`addon2/ir2/vendor/Readability.js` is Mozilla Readability 0.6.0, licensed under the Apache License 2.0 (`addon2/ir2/vendor/Readability-LICENSE.md` and `Readability-Apache-2.0.txt`).
