#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests on tricky sites: each case once crashed the tool or sent a link to the wrong place.

Run: python tests/test_edges.py (needs pypdf and pdfplumber). Exit code 0 if everything passed.
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
FILLER = "<p>filler</p>" * 120


def page(title, body, head=""):
    return ('<!doctype html><meta charset="utf-8"><title>%s</title>%s<body>%s</body>' % (title, head, body)).encode("utf-8")


# The Cyrillic letter in the p%D1%8F.png link and the /pя.png route is deliberate: it tests non-ASCII handling.
SITE = {
    "/": page("Home", """
      <a href="svg.html">svg</a> <a href="http://[bad/">malformed</a> <a href="refresh.html">refresh</a>
      <a href="off.html">external</a> <a href="moved.html">moved</a> <a href="target.html">direct</a>
      <a href="movedfrag.html">moved with anchor</a>
      <a href="target.html#old">old anchor</a> <a href="base/p.html">base</a> <a href="tiny.html">tiny</a>
      <a href="form.html#f">form</a> <a href="pct.html#a%20b">percent</a> <a href="pct.html#a b">space</a> <a href="pct.html#c%20d">encoded</a>
      <a href="scroll.html">scroll</a> <a href="dup.html">duplicates</a> <a href="summary.html">summary</a>
      <a href="gone.png">missing image</a> <a href="ok.png"><img src="ok.png" width="40" height="40" alt="Present"></a>
      <a href="p%D1%8F.png">non-ascii</a> <a href="titles.html">tooltips</a> <a href="shifted.html#deep">offset</a>
      <a href="hidden.html">hidden</a> <a href="widetable.html">table</a> <a href="own.html#__w2p_1">reserved id</a>
      <a href="own.html?__w2p_t=abc">own parameter</a> <a href="selfrefresh.html">self refresh</a> <a href="to404.html">to 404</a>
      <a href="chain0.html">chain</a> <a href="redirfrag.html#own">own anchor</a>
      <img src="map.png" usemap="#m" width="40" height="40"><map name="m"><area shape="rect" coords="0,0,40,40" href="viaarea.html"></map>
    """),
    "/svg.html": page("SVG", '<svg width="200" height="50"><a href="target.html"><text x="5" y="20">link from svg</text></a></svg>'),
    "/refresh.html": page("Refresh", "leaving", '<meta http-equiv="refresh" content="0;url=afterrefresh.html">'),
    "/afterrefresh.html": page("After refresh", "MARK-AFTER-REFRESH"),
    "/target.html": page("Target", FILLER + '<h2 id="x">Heading x</h2><a name="old"></a><h2>Old anchor</h2>' + FILLER),
    "/base/p.html": page("Base", '<a href="#s">to foreign anchor</a> <a href="#absent">no such anchor</a><h2 id="s">Local s</h2>',
                         '<base href="/base/other.html">'),
    "/base/other.html": page("Other base", FILLER + '<h2 id="s">Other s</h2>'),
    "/tiny.html": page("Tiny", '<a href="target.html" style="font-size:12px">i</a> <a href="https://example.org/" style="font-size:12px">l</a>'),
    "/form.html": page("Form", FILLER + '<form id="f"><h2>Form here</h2><input name="id"></form>'),
    "/pct.html": page("Percent", FILLER + '<h2 id="a%20b">Literal heading</h2>' + FILLER + '<h2 id="a b">With space</h2>' + FILLER + '<h2 id="c d">Decoded heading</h2>'),
    "/scroll.html": page("Scroll", '<div style="height:100px;overflow:auto">' + "".join(
        "<p>scroll-line-%d</p>" % n for n in range(60)) + "</div>"),
    "/dup.html": page("Duplicates", FILLER + '<h2 id="d">First</h2>' + FILLER + '<h2 id="d">Second</h2>'),
    "/summary.html": page("Summary", "<h2>Section</h2><details><summary><h3>Nested</h3></summary>text</details>"),
    "/titles.html": page("Tooltips", '<a href="target.html" title="first">one</a> <a href="target.html" title="second">two</a>'),
    "/shifted.html": page("Offset", '<h2 id="deep">Shifted target</h2>' + FILLER,
                          "<style>body{position:relative;min-width:3000px;margin-top:1950px}</style>"),
    "/viaarea.html": page("Via area", "MARK-VIA-AREA"),
    "/hidden.html": page("Hidden",
        '<div style="position:relative"><div id="menu" style="position:absolute;max-height:0;overflow:hidden">MENU-ITEM</div>'
        '<div style="position:absolute;width:1px;height:1px;overflow:hidden">READER-TEXT</div>'
        '<div style="display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;width:200px">'
        + "opening words " * 40 + 'CLAMP-END</div><p>MARK-AFTER-CLAMP</p></div>'),
    "/widetable.html": page("Wide", '<div style="overflow-x:auto"><table><tr>' + "".join(
        '<td style="min-width:200px">COLUMN-%d</td>' % n for n in range(15)) + '</tr></table></div>'
        '<div style="height:60px;overflow:auto;width:300px">' + "word " * 80 + 'BLOCK-END</div>'),
    "/own.html": page("Custom names", '<h2>No id</h2>' + FILLER + '<h2 id="__w2p_1">Reserved heading</h2>'
                      '<h3 id="__w2p_probe_a">Probe</h3>' + FILLER),
    "/selfrefresh.html": page("Self refresh", "MARK-SELF-REFRESH", '<meta http-equiv="refresh" content="0">'),
    "/to404.html": page("To 404", "leaving", '<meta http-equiv="refresh" content="0;url=nothing.html">'),
    "/chain8.html": page("Chain end", "MARK-CHAIN-END"),
    "/ok.png": PNG, "/map.png": PNG, "/pя.png": PNG,
}
for _n in range(8):
    SITE["/chain%d.html" % _n] = page("Chain", "step", '<meta http-equiv="refresh" content="0;url=chain%d.html">' % (_n + 1))
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
        """text is on the first line below the jump point."""
        page = self.plumber.pages[sheet]
        top = float(page.height) - y
        words = [w for w in page.extract_words() if w["top"] >= top - 3]
        if not words:
            return False
        first = min(w["top"] for w in words)
        return any(text in w["text"] for w in words if w["top"] - first < 3) and first - top < 25


FAILURES = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("ok  " if ok else "FAIL", name, " (%s)" % detail if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main_site(tmp):
    server, base = serve(SITE, REDIRECTS)
    out = os.path.join(tmp, "edges.pdf")
    run = run_tool(base + "/", out, "--timeout", "30")
    check("tool survives malformed links, SVG and meta refresh", run.returncode == 0, run.stdout[-600:])
    if run.returncode:
        return
    doc = Doc(out)
    all_text = "\n".join(doc.text)
    top_titles = [str(i.title) for i in doc.reader.outline if not isinstance(i, list)]
    first = doc.links(0)

    check("foreign page did not enter the PDF through a redirect", "http://example.invalid" not in all_text
          and "off.html" in run.stdout)
    check("external redirect stayed a URI", any("off.html" in l.get("uri", "") for l in first))
    check("redirected page is not duplicated", top_titles.count("Target") == 1, str(top_titles))
    check("meta refresh: the final page is printed", "MARK-AFTER-REFRESH" in all_text and "Refresh" not in top_titles)
    check("SVG link became a jump", any("sheet" in l for l in doc.links(doc.section("SVG"))))
    check("malformed external link did not lose the others", len(first) >= 15, str(len(first)))
    check("<a name>: jump goes to the anchor, not the page start", any(
        l.get("sheet", 0) >= doc.section("Target") and l.get("y", 0) < 800 and doc.at(l["sheet"], l["y"], "Old")
        for l in first))
    check("redirect with an anchor in its address goes to that anchor", any(
        l.get("sheet", 0) >= doc.section("Target") and doc.at(l["sheet"], l["y"], "Heading") for l in first if "y" in l))

    base_links = doc.links(doc.section("Base"))
    other = doc.section("Other base")
    check("<base>: both links are present and go to the other page",
          len(base_links) == 2 and all(l.get("sheet", -1) >= other for l in base_links), str(base_links))

    tiny = doc.links(doc.section("Tiny"))
    check("short links are not removed", len(tiny) == 2, str(tiny))

    check("<form id> with a field name=id: jump is exact", any(
        "y" in l and doc.at(l["sheet"], l["y"], "Form") for l in first))
    # As in a browser: #a%20b finds the literal id first, and only then the decoded one
    check("a fragment matches the literal id first", any("y" in l and doc.at(l["sheet"], l["y"], "Literal") for l in first))
    check("a percent-encoded fragment falls back to the decoded id", any("y" in l and doc.at(l["sheet"], l["y"], "Decoded") for l in first))
    check("ids with a literal %20 and with a space keep their own bookmarks", _bookmark_on(doc, "With space"))

    check("scrollable block is printed in full", all("scroll-line-%d" % n in all_text for n in (0, 30, 59)))
    check("bookmark of the second duplicate id goes to it", "Second" in doc.titles and _bookmark_on(doc, "Second"))
    check("h3 in summary does not add a second bookmark", doc.titles.count("Nested") == 1, str(doc.titles))
    check("link to a missing image stayed a URI and is named in the summary",
          any("gone.png" in l.get("uri", "") for l in first) and "gone.png" in run.stdout)
    # The Cyrillic letter in this bookmark is deliberate: it tests non-ASCII handling.
    check("image caption with a non-ASCII name is not percent-encoded", "pя.png" in doc.titles, str(doc.titles[-5:]))
    check("no blank sheet at the end", doc.text[-1].strip() != "" or bool(doc.reader.pages[-1]["/Resources"].get("/XObject")))
    titles = {str(l["contents"]) for l in doc.links(doc.section("Tooltips")) if l.get("contents")}
    check("different titles on one address are not mixed up", titles == {"first", "second"}, str(titles))
    shifted = doc.section("Offset")
    check("probe anchors work on a wide page with a shifted body", any(
        "y" in l and l["sheet"] >= shifted and 0 < l["y"] < 843 and doc.at(l["sheet"], l["y"], "Shifted") for l in first))
    check("page reached through <area> is crawled and the area is named in the summary",
          "MARK-VIA-AREA" in all_text and "viaarea.html" in run.stdout)
    check("content hidden by overflow:hidden did not appear", "MENU-ITEM" not in all_text and "READER-TEXT" not in all_text
          and "CLAMP-END" not in all_text and "MARK-AFTER-CLAMP" in all_text)
    check("wide table in overflow-x fits in full", all("COLUMN-%d" % n in all_text for n in range(15))
          and "scale reduced" in run.stdout, run.stdout[-300:])
    width_ok = all(w["x1"] <= float(p.width) + 1 for p in doc.plumber.pages[doc.section("Wide"):doc.section("Wide") + 1]
                   for w in p.extract_words())
    check("wide table does not overflow the sheet", width_ok)
    check("scrollable block that fit on screen is not cut off", "BLOCK-END" in all_text)
    check("id like __w2p_1 goes to its own heading", any("y" in l and doc.at(l["sheet"], l["y"], "Reserved") for l in first))
    # own.html?__w2p_t=abc is a separate page of the site, so a second section has this title
    own_sections = [doc.reader.get_destination_page_number(i) for i in doc.reader.outline
                    if not isinstance(i, list) and str(i.title) == "Custom names"]
    check("link with its own __w2p_ parameter stayed a jump and did not crash",
          len(own_sections) == 2 and any(l.get("sheet") == own_sections[1] for l in first), str(own_sections))
    check("self-refreshing page is printed", "MARK-SELF-REFRESH" in all_text, run.stdout[-300:])
    check("meta refresh to a 404 gives no section", "To 404" not in top_titles and "to404.html" in run.stdout)
    check("long meta refresh chain reaches the end", "MARK-CHAIN-END" in all_text)
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
    return item is not None and doc.at(doc.reader.get_destination_page_number(item), float(item.top), title.split()[0])


def hanging_image(tmp):
    """The server accepts the connection and stays silent: the page must still be printed."""
    import socket
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    hang = "http://127.0.0.1:%d/hang.png" % silent.getsockname()[1]
    server, base = serve({"/": page("Hanging", 'MARK-TEXT <img src="%s"> <a href="next.html">next</a>' % hang),
                          "/next.html": page("Next", "MARK-NEXT")})
    out = os.path.join(tmp, "hang.pdf")
    run = run_tool(base + "/", out, "--timeout", "4")
    ok = run.returncode == 0 and os.path.exists(out)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages) if ok else ""
    check("hanging image does not drop the page or stop the crawl", "MARK-TEXT" in text and "MARK-NEXT" in text, run.stdout[-400:])
    silent.close()
    server.shutdown()


def hanging_font(tmp):
    import socket
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    css = "<style>@font-face{font-family:H;src:url(http://127.0.0.1:%d/f.woff2)}h1{font-family:H,Arial}</style>" % silent.getsockname()[1]
    server, base = serve({"/": page("Font", "<h1>MARK-FONT</h1>", css)})
    out = os.path.join(tmp, "font.pdf")
    try:
        run = run_tool(base + "/", out, "--timeout", "4")
        check("hanging font does not hang the run", run.returncode == 0 and os.path.exists(out), run.stdout[-300:])
    except subprocess.TimeoutExpired:
        check("hanging font does not hang the run", False, "hung")
    silent.close()
    server.shutdown()


class EndlessArticles(http.server.BaseHTTPRequestHandler):
    """A site like a large blog: every article links to more articles, hubs and the home page."""
    fetched = []

    def do_GET(self):
        path = self.path.split("?")[0]
        self.fetched.append(path)
        if path == "/ru/post/1":
            self.send_response(301)
            self.send_header("Location", "/ru/articles/1/")
            self.end_headers()
            return
        number = re.match(r"/ru/articles/(\d+)/", path)
        n = int(number.group(1)) if number else 1
        body = page(path, '<h1>Page %s</h1><a href="/ru/articles/%d/">next</a> <a href="/ru/articles/%d/">other</a> '
                          '<a href="/ru/hubs/x/">hub</a> <a href="/">home</a> <a href="https://example.org/">outside</a> '
                          '<a href="/ru/articles/%d/comments/">comments</a> <a href="/blog/b.html">blog b</a>'
                    % (path, n + 1, n * 2 + 7, n))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def crawl_limits(tmp):
    """One article must never pull in the whole site: requests are counted on the server."""
    def run(start, *extra):
        handler = type("H", (EndlessArticles,), {"fetched": []})
        server = Server(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        out = os.path.join(tmp, "scope.pdf")
        result = run_tool(base + start, out, "--timeout", "20", *extra)
        server.shutdown()
        pages = sorted(set(handler.fetched) - {"/favicon.ico"})
        return result, pages, [str(i.title) for i in PdfReader(out).outline if not isinstance(i, list)] if result.returncode == 0 else []

    result, pages, sections = run("/ru/articles/1/")
    check("an article stays inside itself and its subpages", result.returncode == 0 and pages == [
        "/ru/articles/1/", "/ru/articles/1/comments/"], str(pages))
    check("the summary says that other links were not followed", "--whole-site" in result.stdout, result.stdout[-400:])
    result, pages, _ = run("/ru/articles/1")
    check("an article without a trailing slash does not take its siblings", result.returncode == 0 and all(
        p.startswith("/ru/articles/1") and not p.startswith("/ru/articles/10") for p in pages), str(pages))
    result, pages, _ = run("/ru/post/1")
    check("the scope follows a redirect of the start address", result.returncode == 0 and pages == [
        "/ru/articles/1/", "/ru/articles/1/comments/", "/ru/post/1"], str(pages))
    result, pages, _ = run("/blog/a.html")
    check("a page with an extension covers its folder and no more", result.returncode == 0 and all(
        p.startswith("/blog/") for p in pages) and "/blog/b.html" in pages, str(pages))
    result, pages, sections = run("/", "--max", "5")
    check("the home page crawls the site but obeys --max", result.returncode == 0 and len(sections) == 5
          and "raise --max" in result.stdout, str(sections))
    check("--max also limits requests", len(pages) <= 6, str(pages))
    result, pages, sections = run("/ru/articles/1/", "--whole-site", "--max", "4")
    check("--whole-site leaves the start folder, still within --max", result.returncode == 0 and len(sections) == 4 and any(
        not p.startswith("/ru/articles/") for p in pages), str(pages))


def index_only(tmp):
    """The server serves only /d/index.html; the /d/ directory returns 404."""
    server, base = serve({"/d/index.html": page("Index", '<a href="other.html">next</a>'),
                          "/d/other.html": page("Other", "MARK-OTHER")})
    out = os.path.join(tmp, "index.pdf")
    run = run_tool(base + "/d/index.html", out, "--timeout", "20")
    ok = run.returncode == 0 and os.path.exists(out)
    text = "\n".join(p.extract_text() or "" for p in PdfReader(out).pages) if ok else ""
    check("explicit /index.html works when the directory is not served", "MARK-OTHER" in text, run.stdout[-400:])
    server.shutdown()


def venv_guard(tmp):
    """A foreign directory in WWW2PDF_VENV must not be erased; a broken venv must be repaired."""
    folder = os.path.join(tmp, "notvenv")
    os.makedirs(folder)
    keep = os.path.join(folder, "keep.txt")
    open(keep, "w").close()
    env = dict(os.environ, WWW2PDF_VENV=folder)
    run = subprocess.run(["/usr/bin/python3", TOOL, "http://127.0.0.1:9/", "-o", os.path.join(tmp, "x.pdf")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, universal_newlines=True)
    check("foreign directory in WWW2PDF_VENV is not erased", os.path.exists(keep) and run.returncode != 0, run.stdout[-300:])

    notes = os.path.join(tmp, "uservenv")
    subprocess.run(["/usr/bin/python3", "-m", "venv", "--without-pip", notes], check=True)
    keep = os.path.join(notes, "my_notes.txt")
    open(keep, "w").close()
    env = dict(os.environ, WWW2PDF_VENV=notes)
    run = subprocess.run(["/usr/bin/python3", TOOL, "http://127.0.0.1:9/", "-o", os.path.join(tmp, "x.pdf")],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, universal_newlines=True)
    check("user files in a foreign venv are not erased", os.path.exists(keep), run.stdout[-300:])

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
    check("broken python in the venv gives no traceback", "Traceback" not in run.stdout, run.stdout[-300:])


def bad_options(tmp):
    for extra, what in ((["--margin", "-1"], "--margin"), (["--margin", "200"], "--margin"), (["--margin", "105"], "--margin"), (["--max", "0"], "--max"),
                        (["--timeout", "0"], "--timeout")):
        run = run_tool("http://127.0.0.1:9/", os.path.join(tmp, "x.pdf"), *extra)
        check("clear error: %s" % " ".join(extra), run.returncode == 2 and what in run.stdout, run.stdout[-200:])
    run = run_tool("http://[bad/", os.path.join(tmp, "x.pdf"))
    check("clear error: invalid address", run.returncode == 2 and "Traceback" not in run.stdout, run.stdout[-200:])
    run = run_tool("http://127.0.0.1:9/", tmp)
    check("clear error: -o points to a directory", run.returncode == 2 and "Traceback" not in run.stdout, run.stdout[-200:])


def long_pages_vh(tmp):
    server, base = serve({"/": page("Screen", '<div style="min-height:100vh">MARK-SCREEN</div>' + FILLER)})
    out = os.path.join(tmp, "vh.pdf")
    run = run_tool(base + "/", out, "--long-pages", "--timeout", "20")
    sheets = len(PdfReader(out).pages) if run.returncode == 0 else -1
    check("--long-pages: a page with 100vh takes one sheet", sheets == 1, "sheets %d" % sheets)
    server.shutdown()


def units():
    source = open(TOOL, encoding="utf-8").read()
    namespace = {"__name__": "w2p"}
    exec(compile(source, TOOL, "exec"), namespace)
    same_site, normalize = namespace["same_site"], namespace["normalize_url"]
    check("explicit :80 is the same site", same_site("http://a.test/x", "http://a.test:80/"))
    check("another port is a foreign site", not same_site("http://a.test:81/x", "http://a.test/"))
    check("malformed address does not crash parsing", not same_site("http://[bad/", "http://a.test/"))
    check("/index.html and / are one page", normalize("http://a.test:80/index.html#x") == "http://a.test/")


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
        crawl_limits(tmp)
        long_pages_vh(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT: %s" % ("all cases passed" if not FAILURES else "%d FAILED: %s" % (len(FAILURES), FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
