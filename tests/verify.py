#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF verifier. Takes its expectations from the site itself, not from www2pdf.py.

For each link in the PDF: the text under it is compared with the visible link text on the site,
links with identical text must keep the site's order, and the expected entry
(full name from summary, source code and title) must sit right at the jump target.
Also checks destination form, all page text (including hidden text), sheet order,
bookmarks one by one, /Contents tooltips, blank and cropped sheets, text overflowing the sheet.

Not checked: text color (white on white passes). Appearance is checked by eye.

Usage: python verify.py file.pdf SITE_URL [--markers markers.txt] [--json report.json]
Exit code is 0 only if there are no discrepancies.
"""
import argparse
import hashlib
import json
import posixpath
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from urllib.parse import unquote, urljoin, urlsplit

import pdfplumber
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.generic import ArrayObject, NullObject

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}
TOP_TOLERANCE = 3  # tolerance in pt: a glyph may start slightly above the jump target
LINE_TOLERANCE = 6  # characters vertically closer than this count as one line
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
MIN_ORDER_NODE = 5  # a node shorter than this may happen to be part of other text
NON_TEXT_TAGS = {"script", "style", "noscript", "template", "head", "title"}


def norm(text):
    """Compare without whitespace and hyphens: pdfplumber returns the hyphen from Chrome fonts as U+E088."""
    text = unicodedata.normalize("NFKC", text or "")  # font ligatures: ﬁ -> fi
    return re.sub(r"[\s\-‐-―­]+", "", text)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace"), r.headers.get("content-type", "")


def page_key(url):
    parts = urlsplit(url)
    path = parts.path or "/"
    if posixpath.basename(path).lower() in ("index.html", "index.htm"):
        path = posixpath.dirname(path).rstrip("/") + "/"
    return parts.scheme, parts.netloc, path, parts.query


def destination_form(item):
    """A bookmark must have the form /XYZ 0 y null."""
    return str(item["/Type"]) == "/XYZ" and float(item.left) == 0 and isinstance(item.zoom, (NullObject, type(None)))


def expected_text(el):
    """The line that must sit at the jump target for this element."""
    summary = el if el.name == "summary" else el.find("summary")
    if summary is not None:
        bold = summary.find("b")
        return norm((bold or summary).get_text())[:40]
    return norm(el.get_text())[:40]


def visible_strings(root):
    """All text nodes in document order, including hidden blocks."""
    result = []
    for node in root.find_all(string=True):
        if type(node).__name__ != "NavigableString":
            continue
        if any(p.name in NON_TEXT_TAGS for p in node.parents):
            continue
        text = norm(str(node))
        if text:
            result.append((text, node))
    return result


def expected_bookmarks(soup):
    """h1–h3 headings and summary elements in document order, as in the PDF outline."""
    titles = []
    for el in soup.find_all(["h1", "h2", "h3", "summary"]):
        if el.name != "summary" and el.find_parent("summary"):
            continue
        title = clean(el.get_text())
        if title:
            titles.append(title)
    return titles


class Site:
    """Crawls the site with standard tools, independently of the browser and www2pdf.py."""

    def __init__(self, root):
        self.root = root
        self.pages = {}
        self.image_heads = defaultdict(set)
        queue, seen = [root], {page_key(root)}
        while queue:
            url = queue.pop(0)
            try:
                html, ctype = fetch(url)
            except urllib.error.HTTPError:  # a broken link on the site is not a verification error
                continue
            except OSError as exc:
                raise SystemExit("ERROR: site did not respond, cannot verify: %s (%s)" % (url, exc))
            if "html" not in ctype:
                continue
            soup = BeautifulSoup(html, "html.parser")
            key = page_key(url)
            ids = {}
            for el in soup.find_all(id=True):
                ids.setdefault(el["id"], el)  # with duplicates the browser goes to the first one
            for el in soup.find_all("a", attrs={"name": True}):
                ids.setdefault(el["name"], el)
            links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                links.append({"href": href, "text": norm(a.get_text()), "title": a.get("title"),
                              "own": self.is_own(href)})
                if a.find("img") is not None and self.is_own(href):
                    head = next((h for h in (p.find(["h1", "h2", "h3", "h4", "figcaption"]) for p in a.parents) if h), None)
                    if head is not None:
                        self.image_heads[href].add(norm(head.get_text()))
                if self.is_own(href) and posixpath.splitext(urlsplit(href).path)[1].lower() not in IMAGE_EXT:
                    target = page_key(href)
                    if target not in seen:
                        seen.add(target)
                        queue.append(href.split("#")[0])
            self.pages[key] = {"url": url, "soup": soup, "ids": ids, "links": links,
                               "title": soup.title.get_text().strip() if soup.title else ""}
        if not self.pages:
            raise SystemExit("ERROR: no pages found on the site, cannot verify: %s" % root)

    def is_own(self, href):
        a, b = urlsplit(href), urlsplit(self.root)
        return a.scheme in ("http", "https") and (a.hostname, a.port) == (b.hostname, b.port)


class Pdf:
    def __init__(self, path):
        self.reader = PdfReader(path)
        self.plumber = pdfplumber.open(path)
        self.page_index = {p.indirect_reference.idnum: i for i, p in enumerate(self.reader.pages)}
        self.chars = {}
        self.shas = {}
        # Tolerances assume a font size of about 9 pt; at larger scales everything grows proportionally
        sizes = sorted(c["size"] for p in self.plumber.pages[:5] for c in p.chars[:2000]) or [9]
        self.zoom = max(1.0, sizes[len(sizes) // 2] / 9)

    def page_chars(self, index):
        if index not in self.chars:
            self.chars[index] = self.plumber.pages[index].chars
        return self.chars[index]

    def page_images(self, index):
        xobjects = self.reader.pages[index]["/Resources"].get("/XObject")
        images = [v.get_object() for v in (xobjects.get_object().values() if xobjects else [])]
        return [o for o in images if o.get("/Subtype") == "/Image"]

    def file_sha(self, href):
        if href not in self.shas:
            with urllib.request.urlopen(href, timeout=60) as r:
                self.shas[href] = hashlib.sha1(r.read()).hexdigest()
        return self.shas[href]

    def page_has_image(self, index, href):
        images = self.page_images(index)
        if not images:
            return False
        if not href.lower().endswith((".jpg", ".jpeg")):
            return True
        return any(hashlib.sha1(o._data).hexdigest() == self.file_sha(href) for o in images)

    def height(self, index):
        return float(self.reader.pages[index].mediabox.height)

    def first_line(self, index):
        """First line of the sheet: on the first sheet of a section this is usually the site header."""
        chars = [c for c in self.page_chars(index) if norm(c["text"])]
        if not chars:
            return ""
        top = min(c["top"] for c in chars)
        return norm("".join(c["text"] for c in chars if c["top"] - top <= LINE_TOLERANCE * self.zoom))

    def landing(self, index, y, want, window, tight, heading=False, header=""):
        """Checks that want sits right at the jump target. Returns (gap in pt, failure reason)."""
        top = self.height(index) - y
        window, tight = window * self.zoom, tight * self.zoom
        chars = [c for c in self.page_chars(index) if top - TOP_TOLERANCE <= c["top"] <= top + window]
        pieces = [norm(c["text"]) for c in chars]
        joined = "".join(pieces)
        at = joined.find(want)
        if at < 0:
            return None, "expected text not found: %r" % want
        position, start = 0, None
        for char, piece in zip(chars, pieces):
            if position >= at and piece:
                start = char
                break
            position += len(piece)
        gap = max(0, start["top"] - top)
        if gap > tight:
            return gap, "entry is %.1f pt below the jump target" % gap
        first_top = min(c["top"] for c, piece in zip(chars, pieces) if piece)
        if start["top"] - first_top > LINE_TOLERANCE * self.zoom:
            return gap, "other text sits above the entry"
        if heading:
            line = norm("".join(c["text"] for c in chars if abs(c["top"] - start["top"]) <= LINE_TOLERANCE * self.zoom))
            if header and line == header and line != want:
                return gap, "jump landed on the site header, not the heading"
            if joined.count(want) > 1:
                return gap, "same text further down: jump did not land on the heading"
        return gap, None

    def text_under(self, index, rect):
        """A character counts as under the link by its center: a comma can stick out of the rectangle."""
        height = self.height(index)
        x0, y0, x1, y1 = rect
        top, bottom = height - max(y0, y1), height - min(y0, y1)
        x0, x1 = min(x0, x1), max(x0, x1)
        return norm("".join(
            c["text"] for c in self.page_chars(index)
            if x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom))

    def annotations(self, index):
        result = []
        for ref in self.reader.pages[index].get("/Annots") or []:
            a = ref.get_object()
            if a.get("/Subtype") == "/Link":
                result.append(a)
        return result

    def explicit_target(self, dest):
        """Only [page_ref /XYZ 0 y null] with y within the sheet -> (sheet number, y)."""
        if not isinstance(dest, ArrayObject) or len(dest) != 5:
            return None
        idnum = getattr(list.__getitem__(dest, 0), "idnum", None)
        if idnum not in self.page_index or str(dest[1]) != "/XYZ":
            return None
        if float(dest[2]) != 0 or not isinstance(dest[4], NullObject):
            return None
        index, y = self.page_index[idnum], float(dest[3])
        if not 0 <= y <= self.height(index):
            return None
        return index, y


def walk_outline(items, out):
    for item in items:
        if isinstance(item, list):
            walk_outline(item, out)
        else:
            out.append(item)


def top_level_sections(pdf):
    """PDF sections from top-level bookmarks: (title, first sheet, nested bookmarks)."""
    sections = []
    items = pdf.reader.outline
    for i, item in enumerate(items):
        if isinstance(item, list):
            continue
        children = []
        if i + 1 < len(items) and isinstance(items[i + 1], list):
            walk_outline(items[i + 1], children)
        sections.append((str(item.title), pdf.reader.get_destination_page_number(item), item, children))
    return sections


def collect_pdf_links(pdf, first, last):
    """Link rectangles of a section in document order."""
    links = []
    for index in range(first, last):
        for a in pdf.annotations(index):
            action = a["/A"].get_object() if "/A" in a else None
            rect = [float(v) for v in a["/Rect"]]
            entry = {"page": index, "rect": rect, "text": pdf.text_under(index, rect),
                     "contents": str(a["/Contents"]) if "/Contents" in a else None}
            if "/Dest" in a:
                entry.update(kind="dest", raw=str(a["/Dest"]))
            elif action is None:
                entry.update(kind="noaction")
            elif action.get("/S") == "/URI":
                entry.update(kind="uri", uri=str(action["/URI"]))
            elif action.get("/S") == "/GoTo":
                entry.update(kind="goto", target=pdf.explicit_target(action["/D"]), raw=str(action["/D"]))
            else:
                entry.update(kind="other")
            links.append(entry)
    return links


def check_link(pdf, site, link, cand, args, section_by_title, counts, missing_list, gaps):
    """Checks that a PDF link goes where its site counterpart goes. Returns the failure reason."""
    href = cand["href"]
    if not cand["own"]:
        if link["kind"] == "uri" and unquote(link["uri"]) == unquote(href):
            counts["external_uri"] += 1
            return None
        return "external link changed"
    if link["kind"] != "goto" or not link.get("target"):
        return "not a GoTo of the form [sheet /XYZ 0 y null]"
    index, y = link["target"]
    if posixpath.splitext(urlsplit(href).path)[1].lower() in IMAGE_EXT:
        # Jumped to the images appendix sheet holding this exact file (JPEG is stored byte for byte)
        if link["text"] != "" or not pdf.page_has_image(index, href):
            return "this image is not on the sheet"
        if pdf.height(index) - y > args.window * pdf.zoom:
            return "jump is not to the top of the image sheet"
        counts["image_goto"] += 1
        return None
    target_key = page_key(href)
    if target_key not in section_by_title:
        return "no section for the target"
    first, last = section_by_title[target_key]
    frag = urlsplit(href).fragment
    target_page = site.pages[target_key]
    element = target_page["ids"].get(frag) or target_page["ids"].get(unquote(frag))
    if frag and element is not None:
        want = expected_text(element)
        if not first <= index < last:
            return "target is in another section"
        gap, why = pdf.landing(index, y, want, args.window, args.tight, element.name in HEADINGS, pdf.first_line(first))
        if why:
            return why
        gaps.append(gap)
        counts["anchor_exact"] += 1
        return None
    if index != first or not pdf.height(index) - 1 <= y <= pdf.height(index):
        return "expected the start of the section"
    if frag:
        counts["anchor_missing_to_section"] += 1
        missing_list.append("%s#%s" % (urlsplit(href).path.lstrip("/"), frag))
    else:
        counts["section_start"] += 1
    return None


def check_links(pdf, site, args, section_by_title, report, problems):
    counts = Counter()
    misses, unexplained, wrong_contents, missing_list, gaps = [], [], [], [], []
    tiny = 0
    for key, sp in site.pages.items():
        first, last = section_by_title[key]
        pdf_links = collect_pdf_links(pdf, first, last)
        pending = defaultdict(list)
        for link in sp["links"]:
            pending[link["text"]].append(link)
        report["site_links_total"] = report.get("site_links_total", 0) + len(sp["links"])
        i = 0
        while i < len(pdf_links):
            link = pdf_links[i]
            rect = link["rect"]
            if abs(rect[2] - rect[0]) < 2 and abs(rect[3] - rect[1]) < 2:
                tiny += 1
            text = link["text"]
            used = 1
            # A link spanning several lines produces several rectangles with one target
            while not pending.get(text) and i + used < len(pdf_links):
                nxt = pdf_links[i + used]
                if nxt.get("raw") != link.get("raw") or nxt.get("uri") != link.get("uri"):
                    break
                text += nxt["text"]
                used += 1
            if not pending.get(text):
                unexplained.append((sp["url"], link["page"] + 1, link["text"], link.get("raw") or link.get("uri")))
                i += 1
                continue
            # Strictly the first match in order: links with identical text cannot swap targets
            cand = pending[text].pop(0)
            why = check_link(pdf, site, link, cand, args, section_by_title, counts, missing_list, gaps)
            if why:
                misses.append({"from": sp["url"], "page": link["page"] + 1, "text": text, "href": cand["href"],
                               "why": why, "goto": str(link.get("target"))})
            rects = pdf_links[i:i + used]
            i += used
            # A link with an image inside its text also produces an empty rectangle with the same target
            while i < len(pdf_links) and pdf_links[i]["text"] == "" and pdf_links[i].get("raw") == link.get("raw"):
                rects.append(pdf_links[i])
                i += 1
            if any((cand.get("title") or None) != (r.get("contents") or None) for r in rects):
                wrong_contents.append((sp["url"], link["page"] + 1, text, cand.get("title"), [r.get("contents") for r in rects]))
            elif cand.get("title"):
                counts["title_in_contents"] += 1
        for lst in pending.values():
            for lost in lst:
                problems.append("site link not found in PDF: %s -> %s (%r)" % (sp["url"], lost["href"], lost["text"]))
    report["links"] = dict(counts)
    report["missing_anchors_on_site"] = sorted(set(missing_list))
    report["missing_anchor_links"] = len(missing_list)
    report["misses"] = len(misses)
    report["max_gap_to_target_pt"] = round(max(gaps), 1) if gaps else None
    if misses:
        problems.append("jumps missing their target: %d, examples: %s" % (len(misses), json.dumps(misses[:8], ensure_ascii=False)))
    if unexplained:
        problems.append("PDF links with no site counterpart: %d, examples: %s" % (len(unexplained), unexplained[:8]))
    if wrong_contents:
        problems.append("/Contents tooltip does not match title: %d, examples: %s" % (len(wrong_contents), wrong_contents[:5]))
    if tiny:
        problems.append("leftover tiny probe links: %d" % tiny)


class SectionText:
    """Section text as one string, plus the sheet and x position of each character."""

    def __init__(self, pdf, first, last):
        pieces, self.sheets, self.xs = [], [], []
        for i in range(first, last):
            for c in pdf.page_chars(i):
                piece = norm(c["text"])
                pieces.append(piece)
                self.sheets.extend([i] * len(piece))
                self.xs.extend([c["x0"]] * len(piece))
        self.text = "".join(pieces)


def consume(stream, text):
    """Removes one occurrence of text from the stream. Returns the new stream or None.

    A table row spanning two sheets is split in the stream by text from a neighboring cell, so
    up to four pieces of at least 8 characters are allowed.
    """
    if text in stream:
        return stream.replace(text, "\0", 1)
    rest, pieces = text, 0
    while rest:
        size = len(rest)
        while size >= 8 and rest[:size] not in stream:
            size -= 1
        if size < 8 or pieces == 4:
            return None
        stream = stream.replace(rest[:size], "\0", 1)
        rest = rest[size:]
        pieces += 1
    return stream


def check_text(pdf, site, args, section_by_title, report, problems):
    """All page text, including hidden text, must appear in its PDF section as many times as on the site.

    Each node takes its own occurrence from the stream, longest first, so a short node is not
    counted against a longer one that contains it. Order is not compared here:
    Chrome prints columns interleaved sheet by sheet. Nodes shorter than 8 characters are not checked.
    """
    total = lost_total = 0
    details_total = details_ok = 0
    examples, order_errors = [], []
    all_text = []
    for key, sp in site.pages.items():
        first, last = section_by_title[key]
        section = SectionText(pdf, first, last)
        stream = section.text
        all_text.append(stream)
        body = sp["soup"].body or sp["soup"]
        strings = visible_strings(body)
        total += len(strings)
        lost_nodes = set()
        for text, node in sorted(strings, key=lambda item: -len(item[0])):
            if len(text) < 8:
                continue
            updated = consume(stream, text)
            if updated is None:
                lost_total += 1
                lost_nodes.add(id(node))
                if len(examples) < 5:
                    examples.append((sp["url"], text[:80]))
            else:
                stream = updated
        for d in sp["soup"].find_all("details"):
            details_total += 1
            details_ok += not any(id(n) in lost_nodes for _, n in visible_strings(d))
        # Sheet order: left-column and full-width nodes cannot go backwards across sheets.
        # The right column is skipped: it starts again from the first sheet of the grid.
        half = float(pdf.plumber.pages[first].width) / 2
        counts = Counter(text for text, _ in strings)
        latest = first
        for text, _ in strings:
            if len(text) < MIN_ORDER_NODE or counts[text] > 1 or section.text.count(text) != 1:
                continue
            at = section.text.find(text)
            if section.xs[at] >= half:
                continue
            if section.sheets[at] < latest:
                order_errors.append((sp["url"], section.sheets[at] + 1, text[:40]))
            latest = max(latest, section.sheets[at])
    report["order_errors"] = len(order_errors)
    if order_errors:
        problems.append("sheets out of order: %d, examples: %s" % (len(order_errors), order_errors[:5]))
    report["text_nodes_site"] = total
    report["text_nodes_lost"] = lost_total
    report["details_site"] = details_total
    report["details_in_pdf"] = details_ok
    if lost_total:
        problems.append("text lost: %d, examples: %s" % (lost_total, examples))
    if args.markers:
        joined = "".join(all_text)
        markers = [m.strip() for m in open(args.markers, encoding="utf-8") if m.strip()]
        missing = [m for m in markers if norm(m) not in joined]
        report["markers"] = len(markers)
        report["markers_found"] = len(markers) - len(missing)
        if missing:
            problems.append("markers not found: %s" % missing[:10])


def check_bookmarks(pdf, site, sections, args, section_by_title, report, problems):
    bad, total = [], 0
    for key, sp in site.pages.items():
        first, _ = section_by_title[key]
        entry = next(s for s in sections if s[0] == sp["title"] and s[1] == first)
        section_item, children = entry[2], entry[3]
        total += 1 + len(children)
        check_section_bookmark(pdf, section_item, first, bad)
        want_titles = expected_bookmarks(sp["soup"])
        got_titles = [clean(str(c.title)) for c in children]
        if [norm(t) for t in want_titles] != [norm(t) for t in got_titles]:
            bad.append((sp["title"], "bookmark list does not match the site: %d vs %d" % (len(got_titles), len(want_titles))))
            continue
        previous = None
        headings = sp["soup"].find_all(["h1", "h2", "h3"])
        heading_titles = {norm(h.get_text()) for h in headings}
        for item in children:
            index = pdf.reader.get_destination_page_number(item)
            y = float(item.top)
            # Bookmarks follow the document: this exposes shuffled and dropped sheets
            if previous and (index, -y) < (previous[0], -previous[1] - 1):
                bad.append((str(item.title)[:50], "bookmark above the previous one: sheet %d" % (index + 1)))
            previous = (index, y)
            if not destination_form(item):
                bad.append((str(item.title)[:50], "destination form"))
                continue
            title = norm(str(item.title))
            _, why = pdf.landing(index, y, title[:25], args.window, args.tight, title in heading_titles, pdf.first_line(first))
            if why:
                bad.append((str(item.title)[:50], "sheet %d: %s" % (index + 1, why)))
    site_titles = {sp["title"] for sp in site.pages.values()}
    for title, first, item, children in sections:
        if title not in site_titles:
            total += 1 + len(children)
            check_section_bookmark(pdf, item, first, bad)
            check_image_bookmarks(pdf, site, children, args, bad)
    report["bookmarks_total"] = total
    report["bookmarks_bad"] = len(bad)
    if bad:
        problems.append("bad bookmarks: %d, examples: %s" % (len(bad), bad[:8]))


def check_section_bookmark(pdf, item, first, bad):
    if not destination_form(item):
        bad.append((str(item.title), "destination form of the section bookmark"))
    if not pdf.height(first) - 1 <= float(item.top) <= pdf.height(first):
        bad.append((str(item.title), "section bookmark is not at the top of the sheet"))


def check_image_bookmarks(pdf, site, children, args, bad):
    """An image bookmark sits at the top of its sheet, and its caption names the entry holding that image on the site."""
    by_sha = {pdf.file_sha(h): h for h in site.image_heads if h.lower().endswith((".jpg", ".jpeg"))}
    previous = -1
    for item in children:
        index = pdf.reader.get_destination_page_number(item)
        if not destination_form(item):
            bad.append((str(item.title)[:50], "destination form"))
            continue
        if index <= previous:
            bad.append((str(item.title)[:50], "images out of order"))
        previous = index
        _, why = pdf.landing(index, float(item.top), norm(str(item.title))[:25], args.window, args.tight)
        if why:
            bad.append((str(item.title)[:50], why))
        for image in pdf.page_images(index):
            href = by_sha.get(hashlib.sha1(image._data).hexdigest())
            if href and not all(h in norm(str(item.title)) for h in site.image_heads[href]):
                bad.append((str(item.title)[:50], "caption does not belong to this image: %s" % href))


def check_sheets(pdf, report, problems):
    """No blank sheets; text does not overflow the sheet and is not shrunk until unreadable."""
    blank, cropped, outside, microscopic = [], [], 0, 0
    for index, page in enumerate(pdf.plumber.pages):
        chars = pdf.page_chars(index)
        if not chars and not pdf.page_images(index):
            blank.append(index + 1)
        # Viewers display the CropBox: the sheet must not be cropped or rotated
        source = pdf.reader.pages[index]
        if list(map(float, source.cropbox)) != list(map(float, source.mediabox)) or source.get("/Rotate", 0):
            cropped.append(index + 1)
        for c in chars:
            if c["x0"] < -1 or c["x1"] > float(page.width) + 1 or c["top"] < -1 or c["bottom"] > float(page.height) + 1:
                outside += 1
            if c["size"] < 1:
                microscopic += 1
    report["sheets"] = len(pdf.plumber.pages)
    report["blank_sheets"] = blank
    if blank:
        problems.append("blank sheets: %s" % blank[:10])
    if cropped:
        problems.append("cropped or rotated sheets: %s" % cropped[:10])
    if outside:
        problems.append("characters outside the sheet: %d" % outside)
    if microscopic:
        problems.append("characters with font size below 1 pt: %d" % microscopic)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("site")
    ap.add_argument("--markers")
    ap.add_argument("--window", type=float, default=45, help="how far below the jump target to look for the entry, pt")
    ap.add_argument("--tight", type=float, default=20, help="how far down the entry may start, pt")
    ap.add_argument("--json")
    args = ap.parse_args()

    site = Site(args.site)  # if the site does not respond, verification exits with an error
    pdf = Pdf(args.pdf)
    problems = []
    report = {"site_pages": len(site.pages)}
    total_pages = len(pdf.reader.pages)

    catalog = pdf.reader.trailer["/Root"]
    names = catalog.get("/Names")
    if "/Dests" in catalog or (names is not None and "/Dests" in names.get_object()):
        problems.append("named destinations remain in the PDF")

    sections = top_level_sections(pdf)
    report["sections"] = [s[0] for s in sections]
    section_by_title = {}
    for sp in site.pages.values():
        for i, section in enumerate(sections):
            if section[0] == sp["title"]:
                end = sections[i + 1][1] if i + 1 < len(sections) else total_pages
                section_by_title[page_key(sp["url"])] = (section[1], end)
    for sp in site.pages.values():
        if page_key(sp["url"]) not in section_by_title:
            problems.append("no section for %s (%s)" % (sp["url"], sp["title"]))
    if problems:
        print("\n".join(problems))
        return 1

    check_links(pdf, site, args, section_by_title, report, problems)
    check_text(pdf, site, args, section_by_title, report, problems)
    check_bookmarks(pdf, site, sections, args, section_by_title, report, problems)
    check_sheets(pdf, report, problems)

    report["problems"] = problems
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in report.items() if k != "problems"}, ensure_ascii=False, indent=1))
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(" -", p[:1500])
        return 1
    print("ALL OK: no link misses its target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
