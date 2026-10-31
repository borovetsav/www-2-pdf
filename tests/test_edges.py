#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Регрессионные тесты на каверзные сайты: каждый случай когда-то ронял инструмент или вёл мимо.

Запуск: python tests/test_edges.py (нужны pypdf и pdfplumber). Код возврата 0, если всё прошло.
"""
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

import pdfplumber
from pypdf import PdfReader

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "www2pdf.py")
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360606060000000050001a5f645400000000049454e44ae426082")
FILLER = "<p>наполнитель</p>" * 120


def page(title, body, head=""):
    return ('<!doctype html><meta charset="utf-8"><title>%s</title>%s<body>%s</body>' % (title, head, body)).encode("utf-8")


SITE = {
    "/": page("Главная", """
      <a href="svg.html">svg</a> <a href="http://[bad/">кривая</a> <a href="refresh.html">обновление</a>
      <a href="off.html">наружу</a> <a href="moved.html">переезд</a> <a href="target.html">прямо</a>
      <a href="movedfrag.html">переезд с якорем</a>
      <a href="target.html#old">старый якорь</a> <a href="base/p.html">база</a> <a href="tiny.html">мелкие</a>
      <a href="form.html#f">форма</a> <a href="pct.html#a%20b">процент</a> <a href="pct.html#a b">пробел</a>
      <a href="scroll.html">прокрутка</a> <a href="dup.html">повторы</a> <a href="summary.html">summary</a>
      <a href="gone.png">нет картинки</a> <a href="ok.png"><img src="ok.png" width="40" height="40" alt="Есть"></a>
      <a href="p%D1%8F.png">кириллица</a> <a href="titles.html">подсказки</a> <a href="shifted.html#deep">сдвиг</a>
      <a href="hidden.html">скрытое</a> <a href="widetable.html">таблица</a> <a href="own.html#__w2p_1">служебный id</a>
      <a href="own.html?__w2p_t=abc">свой параметр</a> <a href="selfrefresh.html">сама себя</a> <a href="to404.html">на 404</a>
      <a href="chain0.html">цепочка</a> <a href="redirfrag.html#own">свой якорь</a>
      <img src="map.png" usemap="#m" width="40" height="40"><map name="m"><area shape="rect" coords="0,0,40,40" href="viaarea.html"></map>
    """),
    "/svg.html": page("SVG", '<svg width="200" height="50"><a href="target.html"><text x="5" y="20">ссылка из svg</text></a></svg>'),
    "/refresh.html": page("Обновление", "уходим", '<meta http-equiv="refresh" content="0;url=afterrefresh.html">'),
    "/afterrefresh.html": page("После обновления", "МЕТКА-ПОСЛЕ-ОБНОВЛЕНИЯ"),
    "/target.html": page("Цель", FILLER + '<h2 id="x">Заголовок x</h2><a name="old"></a><h2>Старый якорь</h2>' + FILLER),
    "/base/p.html": page("База", '<a href="#s">на чужой якорь</a> <a href="#absent">нет такого</a><h2 id="s">Свой s</h2>',
                         '<base href="/base/other.html">'),
    "/base/other.html": page("База другая", FILLER + '<h2 id="s">Другой s</h2>'),
    "/tiny.html": page("Мелкие", '<a href="target.html" style="font-size:12px">i</a> <a href="https://example.org/" style="font-size:12px">l</a>'),
    "/form.html": page("Форма", FILLER + '<form id="f"><h2>Форма здесь</h2><input name="id"></form>'),
    "/pct.html": page("Процент", FILLER + '<h2 id="a%20b">Буквальный</h2>' + FILLER + '<h2 id="a b">С пробелом</h2>'),
    "/scroll.html": page("Прокрутка", '<div style="height:100px;overflow:auto">' + "".join(
        "<p>строка-%d</p>" % n for n in range(60)) + "</div>"),
    "/dup.html": page("Повторы", FILLER + '<h2 id="d">Первый</h2>' + FILLER + '<h2 id="d">Второй</h2>'),
    "/summary.html": page("Summary", "<h2>Раздел</h2><details><summary><h3>Вложенный</h3></summary>текст</details>"),
    "/titles.html": page("Подсказки", '<a href="target.html" title="первая">раз</a> <a href="target.html" title="вторая">два</a>'),
    "/shifted.html": page("Сдвиг", '<h2 id="deep">Сдвинутая цель</h2>' + FILLER,
                          "<style>body{position:relative;min-width:3000px;margin-top:1950px}</style>"),
    "/viaarea.html": page("Через область", "МЕТКА-ЧЕРЕЗ-ОБЛАСТЬ"),
    "/hidden.html": page("Скрытое",
        '<div style="position:relative"><div id="menu" style="position:absolute;max-height:0;overflow:hidden">ПУНКТ-МЕНЮ</div>'
        '<div style="position:absolute;width:1px;height:1px;overflow:hidden">ТЕКСТ-ЧТЕЦА</div>'
        '<div style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;width:200px">'
        + "начало слова " * 40 + 'КОНЕЦ-ОБРЕЗКИ</div><p>МЕТКА-ПОСЛЕ-ОБРЕЗКИ</p></div>'),
    "/widetable.html": page("Широкая", '<div style="overflow-x:auto"><table><tr>' + "".join(
        '<td style="min-width:200px">СТОЛБЕЦ-%d</td>' % n for n in range(15)) + '</tr></table></div>'
        '<div style="height:60px;overflow:auto;width:300px">' + "слово " * 80 + 'КОНЕЦ-БЛОКА</div>'),
    "/own.html": page("Свои имена", '<h2>Без id</h2>' + FILLER + '<h2 id="__w2p_1">Свой служебный</h2>'
                      '<h3 id="__w2p_probe_a">Проба</h3>' + FILLER),
    "/selfrefresh.html": page("Сама себя", "МЕТКА-САМА-СЕБЯ", '<meta http-equiv="refresh" content="0">'),
    "/to404.html": page("На 404", "уходим", '<meta http-equiv="refresh" content="0;url=nothing.html">'),
    "/chain8.html": page("Конец цепочки", "МЕТКА-КОНЕЦ-ЦЕПОЧКИ"),
    "/ok.png": PNG, "/map.png": PNG, "/pя.png": PNG,
}
for _n in range(8):
    SITE["/chain%d.html" % _n] = page("Цепочка", "шаг", '<meta http-equiv="refresh" content="0;url=chain%d.html">' % (_n + 1))
REDIRECTS = {"/redirfrag.html": "/target.html#x", "/off.html": "http://example.invalid/page", "/moved.html": "/target.html", "/movedfrag.html": "/target.html#x"}


class Handler(http.server.BaseHTTPRequestHandler):
    routes, redirects = {}, {}

    def do_GET(self):
        from urllib.parse import unquote, urlsplit
        path = unquote(urlsplit(self.path).path)
        if path in self.redirects:
            self.send_response(301)
            self.send_header("Location", self.redirects[path])
            self.end_headers()
        elif path in self.routes:
            body = self.routes[path]
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if path.endswith(".png") else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(routes, redirects=None):
    handler = type("H", (Handler,), {"routes": routes, "redirects": redirects or {}})
    server = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def run_tool(url, out, *extra, env=None):
    return subprocess.run([sys.executable, TOOL, url, "-o", out] + list(extra), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=500, env=env, universal_newlines=True)


class Doc:
    def __init__(self, path):
        self.reader = PdfReader(path)
        self.plumber = pdfplumber.open(path)
        self.index = {p.indirect_reference.idnum: i for i, p in enumerate(self.reader.pages)}
        self.text = [p.extract_text() or "" for p in self.reader.pages]
        self.titles = []
        self._walk(self.reader.outline)

    def _walk(self, items):
        for item in items:
            if isinstance(item, list):
                self._walk(item)
            else:
                self.titles.append(str(item.title))

    def section(self, title):
        return next(self.reader.get_destination_page_number(i) for i in self.reader.outline
                    if not isinstance(i, list) and str(i.title) == title)

    def links(self, sheet):
        result = []
        for ref in self.reader.pages[sheet].get("/Annots") or []:
            a = ref.get_object()
            action = a["/A"].get_object()
            if action["/S"] == "/URI":
                result.append({"uri": str(action["/URI"]), "contents": a.get("/Contents")})
            else:
                dest = action["/D"]
                result.append({"sheet": self.index[list.__getitem__(dest, 0).idnum], "y": float(dest[3]),
                               "contents": a.get("/Contents")})
        return result

    def at(self, sheet, y, text):
        """text стоит в первой строке ниже точки перехода."""
        page = self.plumber.pages[sheet]
        top = float(page.height) - y
        words = [w for w in page.extract_words() if w["top"] >= top - 3]
        if not words:
            return False
        first = min(w["top"] for w in words)
        return any(text in w["text"] for w in words if w["top"] - first < 3) and first - top < 25


FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ок  " if ok else "ОШИБКА", name, " (%s)" % detail if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main_site(tmp):
    server, base = serve(SITE, REDIRECTS)
    out = os.path.join(tmp, "edges.pdf")
    run = run_tool(base + "/", out, "--timeout", "30")
    check("инструмент не падает на кривых ссылках, SVG и meta refresh", run.returncode == 0, run.stdout[-600:])
    if run.returncode:
        return
    doc = Doc(out)
    all_text = "\n".join(doc.text)
    top_titles = [str(i.title) for i in doc.reader.outline if not isinstance(i, list)]
    first = doc.links(0)

    check("чужая страница не попала в PDF через редирект", "http://example.invalid" not in all_text
          and "off.html" in run.stdout)
    check("редирект наружу остался URI", any("off.html" in l.get("uri", "") for l in first))
    check("страница с редиректом не задвоена", top_titles.count("Цель") == 1, str(top_titles))
    check("meta refresh: напечатана конечная страница", "МЕТКА-ПОСЛЕ-ОБНОВЛЕНИЯ" in all_text and "Обновление" not in top_titles)
    check("ссылка из SVG стала переходом", any("sheet" in l for l in doc.links(doc.section("SVG"))))
    check("кривая внешняя ссылка не потеряла остальные", len(first) >= 15, str(len(first)))
    check("<a name>: переход на якорь, а не на начало", any(
        l.get("sheet", 0) >= doc.section("Цель") and l.get("y", 0) < 800 and doc.at(l["sheet"], l["y"], "Старый")
        for l in first))
    check("редирект с якорем в адресе ведёт на этот якорь", any(
        l.get("sheet", 0) >= doc.section("Цель") and doc.at(l["sheet"], l["y"], "Заголовок") for l in first if "y" in l))

    base_links = doc.links(doc.section("База"))
    other = doc.section("База другая")
    check("<base>: обе ссылки на месте и ведут на чужую страницу",
          len(base_links) == 2 and all(l.get("sheet", -1) >= other for l in base_links), str(base_links))

    tiny = doc.links(doc.section("Мелкие"))
    check("короткие ссылки не удалены", len(tiny) == 2, str(tiny))

    check("<form id> с полем name=id: переход точный", any(
        "y" in l and doc.at(l["sheet"], l["y"], "Форма") for l in first))
    check("id с буквальным %20 и id с пробелом различаются",
          any("y" in l and doc.at(l["sheet"], l["y"], "Буквальный") for l in first)
          and any("y" in l and doc.at(l["sheet"], l["y"], "С") for l in first))

    check("прокручиваемый блок напечатан целиком", all("строка-%d" % n in all_text for n in (0, 30, 59)))
    check("закладка второго повторяющегося id идёт на него", "Второй" in doc.titles and _bookmark_on(doc, "Второй"))
    check("h3 в summary не даёт второй закладки", doc.titles.count("Вложенный") == 1, str(doc.titles))
    check("ссылка на отсутствующую картинку осталась URI и названа в сводке",
          any("gone.png" in l.get("uri", "") for l in first) and "gone.png" in run.stdout)
    check("подпись картинки с кириллицей не в percent-коде", "pя.png" in doc.titles, str(doc.titles[-5:]))
    check("нет пустого листа в конце", doc.text[-1].strip() != "" or bool(doc.reader.pages[-1]["/Resources"].get("/XObject")))
    titles = {str(l["contents"]) for l in doc.links(doc.section("Подсказки")) if l.get("contents")}
    check("разные title на один адрес не путаются", titles == {"первая", "вторая"}, str(titles))
    shifted = doc.section("Сдвиг")
    check("пробы работают на широкой странице со сдвинутым body", any(
        "y" in l and l["sheet"] >= shifted and 0 < l["y"] < 843 and doc.at(l["sheet"], l["y"], "Сдвинутая") for l in first))
    check("страница через <area> обойдена и область названа в сводке",
          "МЕТКА-ЧЕРЕЗ-ОБЛАСТЬ" in all_text and "viaarea.html" in run.stdout)
    check("прятавшее overflow:hidden не проявилось", "ПУНКТ-МЕНЮ" not in all_text and "ТЕКСТ-ЧТЕЦА" not in all_text
          and "КОНЕЦ-ОБРЕЗКИ" not in all_text and "МЕТКА-ПОСЛЕ-ОБРЕЗКИ" in all_text)
    check("широкая таблица в overflow-x уместилась целиком", all("СТОЛБЕЦ-%d" % n in all_text for n in range(15))
          and "масштаб уменьшен" in run.stdout, run.stdout[-300:])
    width_ok = all(w["x1"] <= float(p.width) + 1 for p in doc.plumber.pages[doc.section("Широкая"):doc.section("Широкая") + 1]
                   for w in p.extract_words())
    check("широкая таблица не вылезла за лист", width_ok)
    check("прокручиваемый блок, который на экране умещался, не обрезан", "КОНЕЦ-БЛОКА" in all_text)
    check("id вида __w2p_1 ведёт на свой заголовок", any("y" in l and doc.at(l["sheet"], l["y"], "Свой") for l in first))
    # own.html?__w2p_t=abc это отдельная страница сайта, поэтому раздел с таким заголовком второй
    own_sections = [doc.reader.get_destination_page_number(i) for i in doc.reader.outline
                    if not isinstance(i, list) and str(i.title) == "Свои имена"]
    check("ссылка со своим параметром __w2p_ осталась переходом и не упала",
          len(own_sections) == 2 and any(l.get("sheet") == own_sections[1] for l in first), str(own_sections))
    check("страница, обновляющая сама себя, напечатана", "МЕТКА-САМА-СЕБЯ" in all_text, run.stdout[-300:])
    check("meta refresh на 404 не даёт раздела", "На 404" not in top_titles and "to404.html" in run.stdout)
    check("длинная цепочка meta refresh дошла до конца", "МЕТКА-КОНЕЦ-ЦЕПОЧКИ" in all_text)
    server.shutdown()


def _bookmark_on(doc, title):
    def find(items):
        for item in items:
            if isinstance(item, list):
                found = find(item)
                if found:
                    return found
            elif str(item.title) == title:
                return item
    item = find(doc.reader.outline)
    return item is not None and doc.at(doc.reader.get_destination_page_number(item), float(item.top), title)


def hanging_image(tmp):
    """Сервер принимает соединение и молчит: страница всё равно должна напечататься."""
    import socket
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    hang = "http://127.0.0.1:%d/hang.png" % silent.getsockname()[1]
    server, base = serve({"/": page("Зависшая", 'МЕТКА-ТЕКСТ <img src="%s"> <a href="next.html">дальше</a>' % hang),
                          "/next.html": page("Следующая", "МЕТКА-ДАЛЬШЕ")})
    out = os.path.join(tmp, "hang.pdf")
    run = run_tool(base + "/", out, "--timeout", "4")
    ok = run.returncode == 0 and os.path.exists(out)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages) if ok else ""
    check("зависшая картинка не выбрасывает страницу и обход", "МЕТКА-ТЕКСТ" in text and "МЕТКА-ДАЛЬШЕ" in text, run.stdout[-400:])
    silent.close()
    server.shutdown()


def hanging_font(tmp):
    import socket
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    css = "<style>@font-face{font-family:H;src:url(http://127.0.0.1:%d/f.woff2)}h1{font-family:H,Arial}</style>" % silent.getsockname()[1]
    server, base = serve({"/": page("Шрифт", "<h1>МЕТКА-ШРИФТ</h1>", css)})
    out = os.path.join(tmp, "font.pdf")
    try:
        run = run_tool(base + "/", out, "--timeout", "4")
        check("зависший шрифт не вешает запуск", run.returncode == 0 and os.path.exists(out), run.stdout[-300:])
    except subprocess.TimeoutExpired:
        check("зависший шрифт не вешает запуск", False, "завис")
    silent.close()
    server.shutdown()


def index_only(tmp):
    """Сервер отдаёт только /d/index.html, каталог /d/ даёт 404."""
    server, base = serve({"/d/index.html": page("Индекс", '<a href="other.html">дальше</a>'),
                          "/d/other.html": page("Другая", "МЕТКА-ДРУГАЯ")})
    out = os.path.join(tmp, "index.pdf")
    run = run_tool(base + "/d/index.html", out, "--timeout", "20")
    ok = run.returncode == 0 and os.path.exists(out)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages) if ok else ""
    check("явный /index.html работает, когда каталог не отдаётся", "МЕТКА-ДРУГАЯ" in text, run.stdout[-400:])
    server.shutdown()


def venv_guard(tmp):
    """Чужой каталог в WWW2PDF_VENV нельзя стирать, испорченный venv надо лечить."""
    folder = os.path.join(tmp, "notvenv")
    os.makedirs(folder)
    keep = os.path.join(folder, "keep.txt")
    open(keep, "w").close()
    env = dict(os.environ, WWW2PDF_VENV=folder)
    run = subprocess.run(["/usr/bin/python3", TOOL, "http://127.0.0.1:9/", "-o", os.path.join(tmp, "x.pdf")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, universal_newlines=True)
    check("чужой каталог в WWW2PDF_VENV не стёрт", os.path.exists(keep) and run.returncode != 0, run.stdout[-300:])

    notes = os.path.join(tmp, "uservenv")
    subprocess.run(["/usr/bin/python3", "-m", "venv", "--without-pip", notes], check=True)
    keep = os.path.join(notes, "my_notes.txt")
    open(keep, "w").close()
    env = dict(os.environ, WWW2PDF_VENV=notes)
    run = subprocess.run(["/usr/bin/python3", TOOL, "http://127.0.0.1:9/", "-o", os.path.join(tmp, "x.pdf")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, universal_newlines=True)
    check("файлы пользователя в чужом venv не стёрты", os.path.exists(keep), run.stdout[-300:])

    broken = os.path.join(tmp, "brokenvenv")
    os.makedirs(os.path.join(broken, "bin"))
    open(os.path.join(broken, "pyvenv.cfg"), "w").close()
    bad = os.path.join(broken, "bin", "python")
    with open(bad, "w") as f:
        f.write("x")
    os.chmod(bad, 0o755)
    env = dict(os.environ, WWW2PDF_VENV=broken, PIP_INDEX_URL="http://127.0.0.1:9/simple", PIP_RETRIES="0")
    run = subprocess.run(["/usr/bin/python3", TOOL, "http://127.0.0.1:9/", "-o", os.path.join(tmp, "x.pdf")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, universal_newlines=True, timeout=300)
    check("испорченный python в venv не даёт traceback", "Traceback" not in run.stdout, run.stdout[-300:])


def bad_options(tmp):
    for extra, what in ((["--margin", "-1"], "--margin"), (["--margin", "200"], "--margin"), (["--margin", "105"], "--margin"), (["--max", "0"], "--max"),
                        (["--timeout", "0"], "--timeout")):
        run = run_tool("http://127.0.0.1:9/", os.path.join(tmp, "x.pdf"), *extra)
        check("понятная ошибка: %s" % " ".join(extra), run.returncode == 2 and what in run.stdout, run.stdout[-200:])
    run = run_tool("http://[bad/", os.path.join(tmp, "x.pdf"))
    check("понятная ошибка: неверный адрес", run.returncode == 2 and "Traceback" not in run.stdout, run.stdout[-200:])
    run = run_tool("http://127.0.0.1:9/", tmp)
    check("понятная ошибка: -o указывает на каталог", run.returncode == 2 and "Traceback" not in run.stdout, run.stdout[-200:])


def long_pages_vh(tmp):
    server, base = serve({"/": page("Экран", '<div style="min-height:100vh">МЕТКА-ЭКРАН</div>' + FILLER)})
    out = os.path.join(tmp, "vh.pdf")
    run = run_tool(base + "/", out, "--long-pages", "--timeout", "20")
    sheets = len(PdfReader(out).pages) if run.returncode == 0 else -1
    check("--long-pages: страница с 100vh занимает один лист", sheets == 1, "листов %d" % sheets)
    server.shutdown()


def units():
    source = open(TOOL, encoding="utf-8").read()
    namespace = {"__name__": "w2p"}
    exec(compile(source, TOOL, "exec"), namespace)
    same_site, normalize = namespace["same_site"], namespace["normalize_url"]
    check("явный :80 это тот же сайт", same_site("http://a.test/x", "http://a.test:80/"))
    check("другой порт это чужой сайт", not same_site("http://a.test:81/x", "http://a.test/"))
    check("кривой адрес не роняет разбор", not same_site("http://[bad/", "http://a.test/"))
    check("/index.html и / одна страница", normalize("http://a.test:80/index.html#x") == "http://a.test/")


def main():
    tmp = tempfile.mkdtemp(prefix="www2pdf-edges-")
    try:
        units()
        bad_options(tmp)
        venv_guard(tmp)
        main_site(tmp)
        hanging_image(tmp)
        hanging_font(tmp)
        index_only(tmp)
        long_pages_vh(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ИТОГ: %s" % ("все случаи прошли" if not FAILURES else "ОШИБОК %d: %s" % (len(FAILURES), FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
