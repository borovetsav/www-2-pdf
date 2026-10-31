#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests the verifier itself: every mutation of the PDF or the site must produce a non-zero exit code.

Usage: python test_mutations.py source.pdf http://mock_site_address/ mock_site_dir
"""
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from functools import partial

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NullObject, NumberObject, RectangleObject, TextStringObject

HERE = os.path.dirname(os.path.abspath(__file__))
VERIFY = os.path.join(HERE, "verify.py")


def annots(writer, kind=None):
    """All links as (sheet, annotation, action). kind: goto or uri."""
    result = []
    for i, page in enumerate(writer.pages):
        for ref in page.get("/Annots") or []:
            a = ref.get_object()
            action = a["/A"].get_object()
            if kind is None or (kind == "goto") == (action["/S"] == "/GoTo"):
                result.append((i, a, action))
    return result


def anchored(writer):
    """Links to an anchor: y is well below the top of the sheet."""
    return [(i, a, d) for i, a, d in annots(writer, "goto") if float(d["/D"][3]) < 780 and float(a["/Rect"][2]) - float(a["/Rect"][0]) > 5]


def shift_link(writer, dy):
    _, _, d = anchored(writer)[10]
    d["/D"][3] = FloatObject(float(d["/D"][3]) + dy)


def set_dest_item(writer, position, value):
    _, _, d = anchored(writer)[10]
    d["/D"][position] = value


def to_section_start(writer):
    _, _, d = anchored(writer)[10]
    d["/D"][3] = FloatObject(float(writer.pages[0].mediabox.height))


def swap_targets(writer):
    links = [x for x in anchored(writer) if x[1].get("/Contents")]
    first, second = links[0][2], links[1][2]
    first_dest, second_dest = first["/D"], second["/D"]
    first[NameObject("/D")], second[NameObject("/D")] = second_dest, first_dest


def remove_link(writer):
    i, a, _ = anchored(writer)[10]
    page = writer.pages[i]
    page[NameObject("/Annots")] = ArrayObject([r for r in page["/Annots"] if r.get_object() is not a])


def external_to_goto(writer):
    _, a, _ = annots(writer, "uri")[0]
    a[NameObject("/A")] = DictionaryObject({
        NameObject("/S"): NameObject("/GoTo"),
        NameObject("/D"): ArrayObject([writer.pages[0].indirect_reference, NameObject("/XYZ"), NumberObject(0), FloatObject(800), NullObject()]),
    })


def change_uri(writer):
    _, _, d = annots(writer, "uri")[0]
    d[NameObject("/URI")] = TextStringObject("https://example.com/other")


def named_dest(writer):
    catalog = writer._root_object
    catalog[NameObject("/Dests")] = DictionaryObject()


def change_contents(writer):
    links = [x for x in anchored(writer) if x[1].get("/Contents")]
    links[0][1][NameObject("/Contents")] = TextStringObject("TAMPERED")


def drop_contents(writer):
    links = [x for x in anchored(writer) if x[1].get("/Contents")]
    del links[0][1]["/Contents"]


def swap_sheets(writer, a, b):
    kids = writer._pages.get_object()["/Kids"]
    kids[a], kids[b] = kids[b], kids[a]


def blank_sheet(writer):
    writer.insert_blank_page(index=3)


def crop(writer):
    writer.pages[2].cropbox = RectangleObject([0, 400, 500, 800])


def outline_items(writer):
    """Flat list of bookmarks: (parent section or not, dictionary)."""
    result = []

    def walk(node, depth):
        while node is not None:
            item = node.get_object()
            result.append((depth, item))
            if "/First" in item:
                walk(item["/First"], depth + 1)
            node = item.get("/Next")
    walk(writer._root_object["/Outlines"].get_object()["/First"], 0)
    return result


def dest_of(item):
    return item["/Dest"] if "/Dest" in item else item["/A"].get_object()["/D"]


def shift_bookmark(writer, depth, dy, nth=3):
    items = [i for d, i in outline_items(writer) if d == depth]
    dest = dest_of(items[nth % len(items)])
    dest[3] = FloatObject(float(dest[3]) + dy)


def section_left(writer):
    items = [i for d, i in outline_items(writer) if d == 0]
    dest_of(items[1])[2] = FloatObject(10)


def swap_bookmark_titles(writer):
    items = [i for d, i in outline_items(writer) if d >= 2]
    a, b = items[5], items[6]
    a[NameObject("/Title")], b[NameObject("/Title")] = b["/Title"], a["/Title"]


def image_section_shift(writer):
    items = [i for d, i in outline_items(writer) if d == 0]
    dest = dest_of(items[-1])
    dest[3] = FloatObject(1)


PDF_MUTATIONS = [
    ("link shifted up by 30 pt", partial(shift_link, dy=30)),
    ("link shifted up by 80 pt", partial(shift_link, dy=80)),
    ("link shifted down by 40 pt", partial(shift_link, dy=-40)),
    ("left is not 0", partial(set_dest_item, position=2, value=FloatObject(10))),
    ("zoom is not null", partial(set_dest_item, position=4, value=FloatObject(2))),
    ("anchor points to the top of the sheet", to_section_start),
    ("two links swapped targets", swap_targets),
    ("link removed", remove_link),
    ("external link became GoTo", external_to_goto),
    ("external link points to another address", change_uri),
    ("named destination", named_dest),
    ("tooltip replaced", change_contents),
    ("tooltip removed", drop_contents),
    ("two entry sheets swapped", partial(swap_sheets, a=8, b=9)),
    ("two sources sheets swapped", partial(swap_sheets, a=27, b=28)),
    ("blank sheet", blank_sheet),
    ("sheet cropped via CropBox", crop),
    ("entry bookmark shifted", partial(shift_bookmark, depth=3, dy=60)),
    ("h1 bookmark shifted towards the site header", partial(shift_bookmark, depth=1, dy=30, nth=1)),
    ("section bookmark shifted", partial(shift_bookmark, depth=0, dy=-200, nth=1)),
    ("section bookmark left is not 0", section_left),
    ("bookmarks swapped titles", swap_bookmark_titles),
    ("images appendix bookmark shifted", image_section_shift),
]


def run_verify(pdf, url, markers=None):
    cmd = [sys.executable, VERIFY, pdf, url]
    if markers:
        cmd += ["--markers", markers]
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(folder):
    handler = partial(QuietHandler, directory=folder)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


def site_with(tmp, site, name, edit):
    """Copy of the mock site with an edit: the PDF was built without this text, so it counts as lost."""
    copy = os.path.join(tmp, name)
    shutil.copytree(site, copy)
    path = os.path.join(copy, "part1.html")
    with open(path, encoding="utf-8") as f:
        html = f.read()
    changed = edit(html)
    assert changed != html
    with open(path, "w", encoding="utf-8") as f:
        f.write(changed)
    return copy


def main():
    source, url, site = sys.argv[1:4]
    markers = os.path.join(site, "markers.txt")
    tmp = tempfile.mkdtemp(prefix="www2pdf-mut-")
    failures = []
    try:
        control = run_verify(source, url, markers)
        if control.returncode != 0:
            print("FAIL control run without mutation did not pass:\n" + control.stdout[-800:])
            return 1
        print("ok   control run without mutation: exit code 0")
        for name, mutate in PDF_MUTATIONS:
            writer = PdfWriter(clone_from=source)
            try:
                mutate(writer)
            except Exception as exc:
                print("FAIL mutation did not apply: %s (%r)" % (name, exc))
                failures.append(name)
                continue
            path = os.path.join(tmp, "m.pdf")
            writer.write(path)
            caught = run_verify(path, url, markers).returncode != 0
            print("%s %s" % ("ok  " if caught else "MISSED", name))
            if not caught:
                failures.append(name)

        # The site contains text missing from the PDF: lost text, including repeated text
        paragraph = "<p>Duplicate block for the text loss check</p>"
        site_cases = [
            ("unique paragraph in details lost", lambda h: h.replace('<div class="info">', '<div class="info">' + paragraph, 1)),
            ("one of the repeated paragraphs lost", lambda h: h.replace('<div class="info"><p>', '<div class="info"><p>Detailed filler text that keeps the layout long enough so that anchors land in different places on the sheet at any scale. </p><p>', 1)),
        ]
        for n, (name, edit) in enumerate(site_cases):
            copy = site_with(tmp, site, "site%d" % n, edit)
            server, copy_url = serve(copy)
            # Page addresses in the PDF do not matter: text and heading targets are compared
            caught = "text lost" in run_verify(source, copy_url).stdout
            server.shutdown()
            print("%s %s" % ("ok  " if caught else "MISSED", name))
            if not caught:
                failures.append(name)

        unreachable = run_verify(source, "http://127.0.0.1:9/")
        caught = unreachable.returncode != 0
        print("%s site does not respond" % ("ok  " if caught else "MISSED"))
        if not caught:
            failures.append("site does not respond")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT: %s" % ("all mutations caught" if not failures else "MISSED %d: %s" % (len(failures), failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
