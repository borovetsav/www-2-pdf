#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generator of a mock site with sticky headers, disclosures and cross-page anchors.

4 pages and a sticky site header, ~160 entries in li#M-P-xxxx > details, ~120 sources
#M-A-xxxx in a table with display:block, links with anchors between pages and within
a page, an external link. Every hidden block contains a unique marker.
Markers are saved to markers.txt next to the site.
"""
import base64
import os
import sys

ENTRIES = 160
SOURCES = 120
PER_PAGE = ENTRIES // 2

CSS = """:root { --bg: #faf8f4; --fg: #22201c; --card: #fff; --line: #ddd5c7; --accent: #8a3b12; --link: #1f5f8b; }
@media (prefers-color-scheme: dark) { :root { --bg: #1b1a18; --fg: #ebe6dc; --card: #252320; --line: #3b3731; --link: #7db8e3; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 -apple-system, Arial, sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 0 16px; }
header { background: var(--card); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
nav a { margin-right: 14px; }
a { color: var(--link); }
table { border-collapse: collapse; width: 100%; display: block; overflow-x: auto; }
td, th { border: 1px solid var(--line); padding: 6px 8px; min-width: 160px; vertical-align: top; }
.badge { font-size: .74rem; padding: 2px 7px; border-radius: 10px; color: #fff; background: #2f8a4c; }
.tree summary { padding: 4px 8px; background: var(--card); border: 1px solid var(--line); border-radius: 6px; display: inline-block; }
.info { padding: 8px 12px; border: 1px dashed var(--line); }
.tabpanel[hidden] { display: none; }
.collapse { display: none; }
"""

FILLER = ("Detailed filler text that keeps the layout long enough so that anchors "
          "land in different places on the sheet at any scale. ")

# A small real PNG: 1x1 pixel
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def page(title, active, body):
    nav = "".join('<a href="%s">%s</a>' % (h, t) for h, t in [
        ("index.html", "Home"), ("part1.html", "Part one"),
        ("part2.html", "Part two"), ("sources.html", "Sources")])
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s · Mock</title>'
            '<link rel="stylesheet" href="style.css"></head><body><header><div class="wrap"><nav>%s</nav></div></header>'
            '<main class="wrap">%s</main></body></html>' % (title, nav, body))


def src_link(n):
    return ('<a class="ref" href="sources.html#M-A-%04d" title="Full description of source number %d">A-%04d</a>'
            % (n, n, n))


def entry(prefix, n, markers):
    marker = "HIDDEN-%s-%04d" % (prefix, n)
    markers.append(marker)
    return ('<li class="node" id="%s-P-%04d"><details><summary><span class="role">item</span> '
            '<b>Entry %s-P-%04d</b> <span class="badge">draft</span></summary><div class="info">'
            '<p>Notes (R-%04d): %s %s</p><p>Source: %s</p></div></details></li>'
            % (prefix, n, prefix, n, n, FILLER, marker, src_link(n % SOURCES + 1)))


def entries_page(title, prefix, numbers, markers, other_page, other_prefix):
    items = "".join(entry(prefix, n, markers) for n in numbers)
    jumps = "".join('<a href="#%s-P-%04d">to %s-P-%04d</a>, ' % (prefix, n, prefix, n) for n in numbers[::5])
    cross = "".join('<a href="%s#%s-P-%04d">there %s-P-%04d</a>, ' % (other_page, other_prefix, n, other_prefix, n)
                    for n in (range(1, PER_PAGE + 1, 7) if prefix == "P" else range(PER_PAGE + 1, ENTRIES + 1, 7)))
    # Cyrillic ids are kept on purpose: they test non-ASCII handling
    return page(title, prefix, '<h1>%s</h1><h2 id="переходы">Jumps within the page</h2><p>%s</p><h2>Neighbouring line</h2><p>%s</p>'
                '<h2 id="раздел">Entries</h2><ul class="tree">%s</ul>' % (title, jumps, cross, items))


def make(out):
    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    markers = []
    with open(os.path.join(out, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)
    with open(os.path.join(out, "img", "photo.png"), "wb") as f:
        f.write(PNG)

    index_links = "".join('<li><a href="part1.html#M-P-%04d">To entry M-P-%04d</a></li>' % (n, n) for n in range(1, PER_PAGE + 1, 3))
    index_links += "".join('<li><a href="part2.html#P-P-%04d">To entry P-P-%04d</a></li>' % (n, n) for n in range(PER_PAGE + 1, ENTRIES + 1, 3))
    index_links += "".join("<li>%s</li>" % src_link(n) for n in range(1, SOURCES + 1, 2))
    index_body = (
        # Cyrillic ids and fragments are kept on purpose: they test non-ASCII handling
        '<h1 id="главная">Home</h1><p><a href="https://example.com/external?x=1">External site</a></p>'
        # A Cyrillic id, a missing anchor and an image
        '<p><a href="part1.html#раздел">To part one list</a>, <a href="part1.html#НЕТ-ТАКОГО">Nonexistent anchor</a>, '
        '<a href="#нет-здесь">Nonexistent here</a>, <a href="img/photo.png"><img src="img/photo.png" alt="Photo" width="40" height="40"></a></p>'
        # Other disclosures: aria-controls, a tab and collapse
        '<button aria-expanded="false" aria-controls="panel1">Open</button>'
        '<div id="panel1" hidden>HIDDEN-ARIA-0001</div>'
        '<div role="tabpanel" class="tabpanel" hidden>HIDDEN-TAB-0001</div>'
        '<div class="collapse">HIDDEN-COLLAPSE-0001</div>'
        '<details><summary>Disclosure without id</summary><div class="info">HIDDEN-MAIN-0001</div></details>'
        '<h2>Links</h2><ul>%s</ul>' % index_links)
    markers.extend(["HIDDEN-ARIA-0001", "HIDDEN-TAB-0001", "HIDDEN-COLLAPSE-0001", "HIDDEN-MAIN-0001"])
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page("Home", "", index_body))

    with open(os.path.join(out, "part1.html"), "w", encoding="utf-8") as f:
        f.write(entries_page("Part one", "M", list(range(1, PER_PAGE + 1)), markers, "part2.html", "P"))
    with open(os.path.join(out, "part2.html"), "w", encoding="utf-8") as f:
        f.write(entries_page("Part two", "P", list(range(PER_PAGE + 1, ENTRIES + 1)), markers, "part1.html", "M"))

    rows = "".join(
        '<tr id="M-A-%04d"><td>A-%04d</td><td>%s</td><td>%s</td></tr>' % (n, n, FILLER, "column " * 12)
        for n in range(1, SOURCES + 1))
    sources_body = ('<h1>Sources</h1><p><a href="#M-A-0100">To source A-0100</a></p>'
                    '<table><tr><th>Code</th><th>Description</th><th>Notes</th></tr>%s</table>' % rows)
    with open(os.path.join(out, "sources.html"), "w", encoding="utf-8") as f:
        f.write(page("Sources", "", sources_body))

    with open(os.path.join(out, "markers.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(markers) + "\n")
    return markers


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "tests/out/site"
    print("Mock site: %s, %d markers" % (target, len(make(target))))
