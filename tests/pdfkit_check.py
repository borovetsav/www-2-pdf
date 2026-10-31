#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка движком PDFKit, тем же, что в Preview: ссылки, подсказки, «назад» и «вперёд».

Нужен pyobjc-framework-Quartz, только macOS. Запуск: python pdfkit_check.py файл.pdf
"""
import sys

import Quartz
from Foundation import NSURL, NSMakeRect

doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(sys.argv[1]))
if doc is None:
    sys.exit("Не удалось открыть PDF")

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
print("Листов %d, GoTo %d, URL %d, прочих %d, с подсказкой /Contents %d" % (doc.pageCount(), goto, uri, other, contents))
outline = doc.outlineRoot()
print("Закладок верхнего уровня: %d" % (outline.numberOfChildren() if outline else 0))

view = Quartz.PDFView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 1000))
view.setDisplayMode_(Quartz.kPDFDisplaySinglePage)  # так текущий лист определяется однозначно
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
        fail("Координата цели вне листа: с листа %d, y = %r" % (start + 1, dest.point().y))
    view.goToPage_(doc.pageAtIndex_(start))
    origin = doc.pageAtIndex_(start)
    view.goToDestination_(dest)
    if view.currentPage() != page:
        fail("Мимо листа: с листа %d" % (start + 1))
    if not view.canGoBack():
        fail("Нет истории после перехода с листа %d" % (start + 1))
    view.goBack_(None)
    if view.currentPage() != origin:
        fail("«Назад» не вернул на лист %d" % (start + 1))
    if not view.canGoForward():
        fail("Нет «вперёд» на листе %d" % (start + 1))
    view.goForward_(None)
    if view.currentPage() != page:
        fail("«Вперёд» не вернул к цели с листа %d" % (start + 1))
print("Проверено переходов: %d" % len(sample))
print("PDFKit: назад и вперёд работают" if not failures else "PDFKit: ошибок %d" % failures)
sys.exit(1 if failures else 0)
