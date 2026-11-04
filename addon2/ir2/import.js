// How an article becomes an IR2 document, for Paragraph-Based Incremental Reading (the Anki add-on,
// ir2/import.js) and Pilcrow (vendor/PBIR/import.js). The two files are the same: change both.
//
// It runs in a web page, after Mozilla Readability (Readability.js): the add-on's import dialog runs it in
// a hidden page (importer.py), Pilcrow in a web view of its own (DocumentImport.swift). irImport makes the
// document; the app then stores its images as files, and irPlaceImages puts their names and sizes in.
// Pilcrow's card page uses the constants and helpers at the top too. The add-on's core.py has the same
// constants for its own use.
"use strict";

// Blocks: the elements a browser doesn't put inside a p (a start tag of one closes an open p), and
// those of tables (core.BLOCK_TAGS).
const IR_BLOCKS = ["address", "article", "aside", "blockquote", "center", "details", "dialog", "dir", "div",
    "dl", "fieldset", "figcaption", "figure", "footer", "header", "hgroup", "main", "menu", "nav", "ol", "p",
    "search", "section", "summary", "ul", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "listing", "form", "li",
    "dd", "dt", "plaintext", "table", "hr", "xmp", "thead", "tbody", "tfoot", "tr", "td", "th"];
const IR_BLOCK_SELECTOR = IR_BLOCKS.join(", ");
// What counts as content besides text (core.MEDIA_TAGS).
const IR_MEDIA = "img, svg, video, audio, canvas, iframe, object, embed, math";
// What could run code (core.UNSAFE_TAGS).
const IR_UNSAFE = ["script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "meta",
    "link", "noscript"];
// The blocks that become cards: a p, or a whole list.
const IR_CARD_TAGS = ["p", "ul", "ol", "dl"], IR_LIST_TAGS = ["ul", "ol", "dl"];

function irIsBlock(node) { return node.nodeType === 1 && IR_BLOCKS.includes(node.localName); }
// Text, or an image or the like; white space and <br> alone are not content (core._has_content).
function irHasContent(node) {
    return /\S/.test(node.textContent) || !!(node.querySelector && node.querySelector(IR_MEDIA));
}
// Takes out what could run code: script and the like, on… attributes, and javascript: or vbscript: in
// attribute values (core.sanitize).
function irSanitize(root) {
    for (const element of root.querySelectorAll(IR_UNSAFE.join(", "))) { element.remove(); }
    for (const element of [root, ...root.querySelectorAll("*")]) {
        if (!element.attributes) { continue; }
        for (const attribute of Array.from(element.attributes)) {
            if (/^on/i.test(attribute.name)
                || /(javascript|vbscript):/i.test(attribute.value.replace(/[\x00-\x20]/g, ""))) {
                element.removeAttribute(attribute.name);
            }
        }
    }
}

// An article as IR2's document. `kind` is html (a web page or HTML file, whose article Readability takes
// out), markdown (already made HTML) or text (plain text). Returns irPrepare's result with the article's
// byline and date, or {error} if Readability finds no article.
function irImport(source, kind, url) {
    let html = source, byline = "", date = "";
    if (kind === "html") {
        const article = irReadArticle(source, url);
        if (article.error) { return { error: article.error }; }
        ({ content: html, byline, date } = article);
    }
    return Object.assign(irPrepare(html, kind === "text"), { byline, date });
}

// The article in a page, by Readability. The page is read with DOMParser, so its scripts don't run and its
// images don't load; a <base> with its URL lets Readability make relative URLs absolute. Readability gives
// the title, byline and date apart from the article; as in Firefox's Reader View they go before it, the
// title as an h1 and the byline and date as one line in a div, which isn't a card.
function irReadArticle(html, url) {
    try {
        const doc = new DOMParser().parseFromString(html, "text/html");
        if (url) {
            const base = doc.createElement("base");
            base.setAttribute("href", url);
            (doc.head || doc.documentElement).prepend(base);
        }
        const article = new Readability(doc).parse();
        if (!article) { return { error: "no article was found" }; }
        // Built in a document of its own, where images don't load and attributes don't run.
        const out = document.implementation.createHTMLDocument("");
        out.body.innerHTML = article.content;
        const date = /^\d{4}-\d{2}-\d{2}/.exec(article.publishedTime || "");
        const line = [article.byline, date && date[0]].filter((s) => s && s.trim()).join(", ");
        if (line) {
            const by = out.createElement("div");
            by.textContent = line;
            out.body.prepend(by);
        }
        if (article.title) {
            const h = out.createElement("h1");
            h.textContent = article.title;
            out.body.prepend(h);
        }
        return { content: out.body.innerHTML, byline: article.byline || "", date: date ? date[0] : "" };
    } catch (e) {
        return { error: String((e && e.message) || e) };
    }
}

// HTML, or plain text (`isText`, split into paragraphs at blank lines), in IR2's document form, read by
// the browser's parser (which also takes a heading out of a p it is in). What could run code goes, with
// nav, form and button, IR2's own marks and source comments; wrappers (div, section, …) are taken out, so
// the article is a run of blocks, and text between blocks goes into a p, as Readability does; each p and
// list with content goes in a numbered <div data-ir="pN">. Headings, pre, quotes, tables and figures stay
// in the document but aren't cards.
//
// Returns {html, paragraphs: [{id, html}], images}. `images` are the sources of the images to store as
// files, each once, in document order: embedded ones (base64 data: URIs that decode), and those on the
// web (http:, https:, or //, which becomes https:). Their srcset and sizes and a picture's sources go, so
// the stored file is drawn, or else the address import tried: a browser picks one of them and doesn't try
// the others if it can't load it.
function irPrepare(source, isText) {
    const parsed = new DOMParser().parseFromString(isText ? "" : source, "text/html");
    const body = parsed.body;
    if (isText) {
        for (const block of source.replace(/\r\n?/g, "\n").split("\n\n")) {
            if (!block.trim()) { continue; }
            const p = parsed.createElement("p");
            p.textContent = block.trim();
            body.append(p);
        }
    }
    for (const element of body.querySelectorAll("nav, form, button")) { element.remove(); }
    for (const element of body.querySelectorAll("*")) {
        for (const name of ["data-ir", "data-cloze", "data-ir-split", "contenteditable"]) { element.removeAttribute(name); }
    }
    irSanitize(body);
    const comments = parsed.createTreeWalker(body, NodeFilter.SHOW_COMMENT);
    const sourceComments = [];
    while (comments.nextNode()) {
        if (comments.currentNode.data.trim().startsWith("ir:source")) { sourceComments.push(comments.currentNode); }
    }
    sourceComments.forEach((comment) => comment.remove());

    const images = [];
    for (const img of body.querySelectorAll("img[src]")) {
        let src = img.getAttribute("src").trim();
        const data = /^data:[\w.+-]+\/[\w.+-]+;base64,(.*)$/s.exec(src);
        if (data) {
            try { if (!atob(data[1].replace(/\s+/g, ""))) { continue; } } catch { continue; }
        } else if (/^(https?:)?\/\//i.test(src)) {
            if (src.startsWith("//")) { src = "https:" + src; }
        } else {
            continue;
        }
        img.setAttribute("src", src);
        img.removeAttribute("srcset");
        img.removeAttribute("sizes");
        if (img.parentElement && img.parentElement.localName === "picture") {
            for (const s of img.parentElement.querySelectorAll(":scope > source")) { s.remove(); }
        }
        if (!images.includes(src)) { images.push(src); }
    }

    // The wrappers taken out, if they hold blocks.
    const isContainer = (node) => node.nodeType === 1
        && ["div", "section", "article", "main", "header"].includes(node.localName)
        && Array.from(node.childNodes).some(irIsBlock);
    const isBlank = (node) => node.nodeType === 8 || (node.nodeType === 3 && !node.data.trim());
    const wrapRuns = (parent) => {
        let run = [];
        const flush = () => {
            if (run.some((node) => !isBlank(node))) {
                const p = parsed.createElement("p");
                run[0].before(p);
                p.append(...run);
            }
            run = [];
        };
        for (const node of Array.from(parent.childNodes)) {
            if (irIsBlock(node)) { flush(); } else { run.push(node); }
        }
        flush();
    };
    // A document that is one wrapper keeps it (with its attributes, such as lang), and is made inside it.
    let root = body;
    for (;;) {
        const kids = Array.from(root.childNodes).filter((node) => !isBlank(node));
        if (kids.length === 1 && isContainer(kids[0])) { root = kids[0]; } else { break; }
    }
    while (Array.from(root.childNodes).some(isContainer)) {
        for (const node of Array.from(root.childNodes)) {
            if (isContainer(node)) { wrapRuns(node); node.replaceWith(...node.childNodes); }
        }
    }
    wrapRuns(root);
    const paragraphs = [];
    for (const node of Array.from(root.children)) {
        if (!IR_CARD_TAGS.includes(node.localName) || !irHasContent(node)) { continue; }
        const box = parsed.createElement("div");
        box.dataset.ir = "p" + (paragraphs.length + 1);
        node.replaceWith(box);
        box.append(node);
        paragraphs.push({ id: box.dataset.ir, html: box.innerHTML });
    }
    return { html: body.innerHTML, paragraphs, images };
}

// After the app has stored the images: `placed` gives, by an image's source (irPrepare's images), the
// source to use instead (its file) and the image's width and height. Each such image gets them as its
// width and height attributes, so the card has its place before it loads: with both attributes, they
// stay; with one that is a number, the other follows from the ratio; with neither, the image's own. An
// image not in `placed`, such as one that couldn't be fetched, keeps its address. Returns the document
// and its paragraphs ({html, paragraphs}) with them.
function irPlaceImages(html, paragraphs, placed) {
    const number = (value) => /^\s*\d+(\.\d+)?\s*$/.test(value || "") ? parseFloat(value) : null;
    const place = (source) => {
        const template = document.createElement("template");
        template.innerHTML = source;
        for (const img of template.content.querySelectorAll("img[src]")) {
            if (!Object.hasOwn(placed, img.getAttribute("src"))) { continue; }
            const p = placed[img.getAttribute("src")];
            img.setAttribute("src", p.src);
            if (!p.width || !p.height || (img.hasAttribute("width") && img.hasAttribute("height"))) { continue; }
            const width = number(img.getAttribute("width")), height = number(img.getAttribute("height"));
            if (width) {
                img.setAttribute("height", Math.round(width * p.height / p.width));
            } else if (height) {
                img.setAttribute("width", Math.round(height * p.width / p.height));
            } else if (!img.hasAttribute("width") && !img.hasAttribute("height")) {
                img.setAttribute("width", p.width);
                img.setAttribute("height", p.height);
            }
        }
        return template.innerHTML;
    };
    return { html: place(html), paragraphs: paragraphs.map((p) => ({ id: p.id, html: place(p.html) })) };
}
