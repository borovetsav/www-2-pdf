#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка самого верификатора: каждая порча PDF или сайта должна дать ненулевой код.

Запуск: python test_mutations.py исходный.pdf http://адрес_макета/ каталог_макета
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
    """Все ссылки как (лист, аннотация, действие). kind: goto или uri."""
    result = []
    for i, page in enumerate(writer.pages):
        for ref in page.get("/Annots") or []:
            a = ref.get_object()
            action = a["/A"].get_object()
            if kind is None or (kind == "goto") == (action["/S"] == "/GoTo"):
                result.append((i, a, action))
    return result


def anchored(writer):
    """Ссылки на якорь: y заметно ниже верха листа."""
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
    d[NameObject("/URI")] = TextStringObject("https://example.com/другое")


def named_dest(writer):
    catalog = writer._root_object
    catalog[NameObject("/Dests")] = DictionaryObject()


def change_contents(writer):
    links = [x for x in anchored(writer) if x[1].get("/Contents")]
    links[0][1][NameObject("/Contents")] = TextStringObject("ПОДМЕНА")


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
    """Плоский список закладок записи: (родитель-раздел или нет, словарь)."""
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
    ("сдвиг ссылки вверх на 30 pt", partial(shift_link, dy=30)),
    ("сдвиг ссылки вверх на 80 pt", partial(shift_link, dy=80)),
    ("сдвиг ссылки вниз на 40 pt", partial(shift_link, dy=-40)),
    ("left не 0", partial(set_dest_item, position=2, value=FloatObject(10))),
    ("zoom не null", partial(set_dest_item, position=4, value=FloatObject(2))),
    ("якорь ведёт на начало листа", to_section_start),
    ("обмен целями у двух ссылок", swap_targets),
    ("ссылка удалена", remove_link),
    ("внешняя стала GoTo", external_to_goto),
    ("внешняя ведёт на другой адрес", change_uri),
    ("именованное назначение", named_dest),
    ("подсказка подменена", change_contents),
    ("подсказка удалена", drop_contents),
    ("два листа людей переставлены", partial(swap_sheets, a=8, b=9)),
    ("два листа источников переставлены", partial(swap_sheets, a=27, b=28)),
    ("пустой лист", blank_sheet),
    ("лист обрезан через CropBox", crop),
    ("закладка человека сдвинута", partial(shift_bookmark, depth=3, dy=60)),
    ("закладка h1 сдвинута к шапке", partial(shift_bookmark, depth=1, dy=30, nth=1)),
    ("закладка раздела сдвинута", partial(shift_bookmark, depth=0, dy=-200, nth=1)),
    ("у закладки раздела left не 0", section_left),
    ("закладки поменялись заголовками", swap_bookmark_titles),
    ("закладка приложения сдвинута", image_section_shift),
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
    """Копия макета с правкой: PDF собран без этого текста, значит, он «потерян»."""
    copy = os.path.join(tmp, name)
    shutil.copytree(site, copy)
    path = os.path.join(copy, "mama.html")
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
            print("ОШИБКА контроль без порчи не прошёл:\n" + control.stdout[-800:])
            return 1
        print("ок   контроль без порчи: код 0")
        for name, mutate in PDF_MUTATIONS:
            writer = PdfWriter(clone_from=source)
            try:
                mutate(writer)
            except Exception as exc:
                print("ОШИБКА порча не применилась: %s (%r)" % (name, exc))
                failures.append(name)
                continue
            path = os.path.join(tmp, "m.pdf")
            writer.write(path)
            caught = run_verify(path, url, markers).returncode != 0
            print("%s %s" % ("ок  " if caught else "ПРОПУЩЕНО", name))
            if not caught:
                failures.append(name)

        # Сайт содержит текст, которого нет в PDF: потерянный текст, в том числе повторяющийся
        paragraph = "<p>Дубль-блок для проверки потери текста</p>"
        site_cases = [
            ("уникальный абзац в details потерян", lambda h: h.replace('<div class="info">', '<div class="info">' + paragraph, 1)),
            ("один из повторяющихся абзацев потерян", lambda h: h.replace('<div class="info"><p>', '<div class="info"><p>Подробный текст для объёма, чтобы вёрстка занимала много листов и якоря попадали в разные места страницы при любом масштабе. </p><p>', 1)),
        ]
        for n, (name, edit) in enumerate(site_cases):
            copy = site_with(tmp, site, "site%d" % n, edit)
            server, copy_url = serve(copy)
            # Адреса страниц в PDF не важны, сравнивается текст и цели по заголовкам
            caught = "текст потерян" in run_verify(source, copy_url).stdout
            server.shutdown()
            print("%s %s" % ("ок  " if caught else "ПРОПУЩЕНО", name))
            if not caught:
                failures.append(name)

        unreachable = run_verify(source, "http://127.0.0.1:9/")
        caught = unreachable.returncode != 0
        print("%s сайт не отвечает" % ("ок  " if caught else "ПРОПУЩЕНО"))
        if not caught:
            failures.append("сайт не отвечает")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ИТОГ: %s" % ("все порчи пойманы" if not failures else "ПРОПУЩЕНО %d: %s" % (len(failures), failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
