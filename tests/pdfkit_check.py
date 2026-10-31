#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check with PDFKit, the engine Preview uses: links, tooltips, Back and Forward.

Requires pyobjc-framework-Quartz, macOS only. Usage: python pdfkit_check.py file.pdf
"""
import sys

import Quartz
from Foundation import NSURL, NSMakeRect

doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(sys.argv[1]))
if doc is None:
    sys.exit("Could not open the PDF")

goto, uri, other, contents = 0, 0, 0, 0
targets = []
for i in range(doc.pageCount()):
    for a in doc.pageAtIndex_(i).annotations() or []:
        if a.type() != "Link":
            continue
        action = a.action()
        if action is not None and action.type() == "GoTo":
            goto += 1
            targets.append((i, action.destination()))
        elif action is not None and action.type() in ("URL", "URI"):
            uri += 1
        else:
            other += 1
        if a.contents():
            contents += 1
print("Sheets: %d, GoTo: %d, URL: %d, other: %d, with /Contents tooltip: %d" % (doc.pageCount(), goto, uri, other, contents))
outline = doc.outlineRoot()
print("Top-level bookmarks: %d" % (outline.numberOfChildren() if outline else 0))

view = Quartz.PDFView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 1000))
view.setDisplayMode_(Quartz.kPDFDisplaySinglePage)  # makes the current sheet unambiguous
view.setDocument_(doc)
failures = 0


def fail(message):
    global failures
    failures += 1
    print(message)


sample = targets[:: max(1, len(targets) // 40)]
for start, dest in sample:
    page = dest.page()
    if not 0 <= dest.point().y <= page.boundsForBox_(Quartz.kPDFDisplayBoxMediaBox).size.height:
        fail("Target coordinate is off the sheet: from sheet %d, y = %r" % (start + 1, dest.point().y))
    view.goToPage_(doc.pageAtIndex_(start))
    origin = doc.pageAtIndex_(start)
    view.goToDestination_(dest)
    if view.currentPage() != page:
        fail("Wrong sheet: from sheet %d" % (start + 1))
    if not view.canGoBack():
        fail("No history after the jump from sheet %d" % (start + 1))
    view.goBack_(None)
    if view.currentPage() != origin:
        fail("Back did not return to sheet %d" % (start + 1))
    if not view.canGoForward():
        fail("Forward is unavailable on sheet %d" % (start + 1))
    view.goForward_(None)
    if view.currentPage() != page:
        fail("Forward did not return to the target from sheet %d" % (start + 1))
print("Jumps checked: %d" % len(sample))
print("PDFKit: Back and Forward work" if not failures else "PDFKit: %d errors" % failures)
sys.exit(1 if failures else 0)
