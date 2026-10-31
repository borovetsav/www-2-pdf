#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the README demo from a made-up site: real site, real PDF, real screenshots.

The video is an animation drawn from the actual PDF: every jump uses the coordinates
stored in its links and bookmarks. It is not a screen recording of a PDF viewer.

Usage: python docs/demo/build_demo.py WORK_DIR   (needs playwright, pypdf, pypdfium2, Pillow, ffmpeg)
"""
import functools
import http.server
import math
import os
import socketserver
import subprocess
import sys
import threading

import pypdfium2
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS = os.path.join(ROOT, "docs")
PORT = 8000
W, H, FPS = 1280, 720, 24
INK, MUTED, ACCENT, GREEN = (30, 34, 45), (110, 118, 135), (67, 97, 238), (47, 138, 76)
DARK, PANEL = (17, 20, 30), (28, 32, 45)
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def font(size, bold=False, mono=False):
    if mono:
        return ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", size, index=1 if bold else 0)
    return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size, index=1 if bold else 0)


# ---------- The made-up site ----------

CSS = """
:root { --bg:#f6f7fb; --fg:#1e222d; --muted:#6e7687; --card:#fff; --line:#dfe3ee; --accent:#4361ee; }
@media (prefers-color-scheme: dark) { :root { --bg:#14161f; --fg:#e8eaf2; --card:#1e2230; --line:#333a4d; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 -apple-system,"Helvetica Neue",Arial,sans-serif; }
.wrap { max-width:940px; margin:0 auto; padding:0 20px; }
header { position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line); }
header .wrap { display:flex; gap:22px; align-items:center; padding-top:12px; padding-bottom:12px; }
.logo { font-weight:700; color:var(--accent); }
nav a { color:var(--fg); text-decoration:none; margin-right:16px; }
h1 { font-size:2rem; margin:26px 0 8px; }
h2 { font-size:1.35rem; margin:34px 0 10px; padding-bottom:5px; border-bottom:1px solid var(--line); }
h3 { font-size:1.05rem; margin:0 0 6px; }
a { color:var(--accent); }
.cards { display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; }
.card, .item { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:12px 0; }
.card { margin:0; }
.badge { display:inline-block; font-size:.72rem; font-weight:600; color:#fff; padding:2px 8px; border-radius:10px; background:#2f8a4c; }
.badge.beta { background:#b8730b; } .badge.old { background:#b33a3a; }
details { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 16px; margin:10px 0; }
summary { font-weight:600; cursor:pointer; }
details[open] summary { color:var(--accent); margin-bottom:6px; }
table { display:block; overflow-x:auto; border-collapse:collapse; width:100%; }
th, td { border:1px solid var(--line); padding:7px 10px; text-align:left; background:var(--card); }
pre { background:#1c2030; color:#e4e7f2; padding:10px 12px; border-radius:8px; overflow:auto; font-size:.86rem; }
img { max-height:150px; border-radius:8px; border:1px solid var(--line); }
"""

NAV = [("index.html", "Home"), ("guides.html", "Guides"), ("reference.html", "Reference"), ("releases.html", "Releases")]
TOPICS = [("Authentication", "auth"), ("Rate limits", "rate-limits"), ("Webhooks", "webhooks"), ("Pagination", "pagination"),
          ("Errors", "errors"), ("Uploads", "uploads"), ("Search", "search"), ("Exports", "exports"), ("Teams", "teams"),
          ("Audit log", "audit-log"), ("Billing", "billing"), ("Sandbox", "sandbox")]


def page(title, active, body):
    nav = "".join('<a href="%s">%s</a>' % item for item in NAV)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>%s · Acme Handbook</title>'
            '<style>%s</style></head><body><header><div class="wrap"><span class="logo">Acme Handbook</span><nav>%s</nav></div>'
            '</header><main class="wrap">%s</main></body></html>' % (title, CSS, nav, body))


def write_site(out):
    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    for name, colors in (("overview", ((67, 97, 238), (120, 80, 220))), ("flow", ((47, 138, 76), (20, 120, 160)))):
        art = Image.new("RGB", (1200, 600))
        pixels = ImageDraw.Draw(art)
        for x in range(1200):
            mix = x / 1199
            pixels.line([(x, 0), (x, 600)], fill=tuple(int(a + (b - a) * mix) for a, b in zip(*colors)))
        for i in range(4):
            pixels.rounded_rectangle([90 + i * 270, 200, 300 + i * 270, 400], 24, fill=(255, 255, 255))
            pixels.text((130 + i * 270, 280), "Step %d" % (i + 1), font=font(38, True), fill=colors[0])
        art.save(os.path.join(out, "img", name + ".jpg"), quality=88)

    cards = "".join('<div class="card"><h3><a href="%s">%s</a></h3>%s</div>' % item for item in (
        ("guides.html", "Guides", "Step-by-step answers to common questions."),
        ("reference.html", "Reference", "Every topic, with limits and examples."),
        ("releases.html", "Releases", "What changed, and when.")))
    home = ('<h1 id="welcome">Acme Handbook</h1><p>Everything the team knows about running Acme, in one place. '
            'Start with the <a href="guides.html#getting-started">getting started guide</a>, or jump straight to '
            '<a href="reference.html#rate-limits">rate limits</a>. More at <a href="https://example.com/">example.com</a>.</p>'
            '<div class="cards">%s</div><h2 id="popular">Popular topics</h2><ul>%s</ul>'
            '<h2 id="glance">Plans at a glance</h2><table><tr><th>Plan</th><th>Requests per minute</th><th>Seats</th><th>Support</th></tr>'
            '<tr><td>Starter</td><td>60</td><td>3</td><td>Community</td></tr><tr><td>Team</td><td>600</td><td>25</td><td>Email</td></tr>'
            '<tr><td>Scale</td><td>6,000</td><td>Unlimited</td><td>Priority</td></tr></table>') % (
        cards, "".join('<li><a href="reference.html#%s">%s</a></li>' % (slug, name) for name, slug in TOPICS[:6]))

    questions = [(name, slug) for name, slug in TOPICS]
    faq = "".join(
        '<details><summary>How do I work with %s?</summary><p>This answer stays hidden on the website until you click. '
        'In the PDF it is always open. See <a href="reference.html#%s">%s in the reference</a> for the limits and a full '
        'example, or the <a href="releases.html#v2">latest release notes</a>.</p></details>' % (name.lower(), slug, name.lower())
        for name, slug in questions)
    guides = ('<h1 id="guides">Guides</h1><h2 id="getting-started">Getting started</h2><p>Create an account, generate a key, '
              'and make your first request. The whole thing takes about five minutes. If a request is rejected, check the '
              '<a href="reference.html#rate-limits">rate limits</a> first.</p>'
              '<pre>curl -H "Authorization: Bearer $KEY" https://api.example.com/v2/ping</pre>'
              '<a href="img/flow.jpg"><img src="img/flow.jpg" alt="Request flow" loading="lazy"></a>'
              '<h2 id="faq">Frequently asked questions</h2>%s') % faq

    items = "".join(
        '<section class="item"><h3 id="%s">%s <span class="badge%s">%s</span></h3><p>How %s behaves in production, '
        'which limits apply, and what to do when something goes wrong. See also <a href="#%s">%s</a>.</p>'
        '<details><summary>Show an example</summary><pre>GET /v2/%s?limit=50\nAuthorization: Bearer $KEY\n\n200 OK</pre>'
        '<p>The default page size is 50, and the maximum is 500.</p></details></section>' % (
            slug, name, *(("", "stable") if i % 3 else (" beta", "beta")), name.lower(),
            TOPICS[(i + 1) % len(TOPICS)][1], TOPICS[(i + 1) % len(TOPICS)][0], slug)
        for i, (name, slug) in enumerate(TOPICS))
    reference = ('<h1 id="reference">Reference</h1><p>Each topic below can be linked to directly.</p>'
                 '<a href="img/overview.jpg"><img src="img/overview.jpg" alt="System overview" loading="lazy"></a>%s') % items

    releases = ('<h1 id="releases">Releases</h1><h2 id="v2">Version 2</h2><div class="item"><span class="badge">current</span> '
                'New <a href="reference.html#search">search</a>, faster <a href="reference.html#exports">exports</a>.</div>'
                '<h2 id="v1">Version 1</h2><div class="item"><span class="badge old">retired</span> The first public release.</div>')
    for name, title, body in (("index", "Home", home), ("guides", "Guides", guides), ("reference", "Reference", reference),
                              ("releases", "Releases", releases)):
        with open(os.path.join(out, name + ".html"), "w", encoding="utf-8") as f:
            f.write(page(title, name, body))


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(folder):
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=folder))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def screenshot_site(work):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, headless=True)
        ctx = browser.new_context(viewport={"width": W, "height": H}, color_scheme="light", device_scale_factor=1)
        tab = ctx.new_page()
        tab.goto("http://127.0.0.1:%d/guides.html#faq" % PORT, wait_until="load")
        tab.screenshot(path=os.path.join(work, "site_guides.png"))
        browser.close()


# ---------- Reading the real PDF ----------

class Doc:
    def __init__(self, path, width_px):
        self.reader = PdfReader(path)
        self.pdfium = pypdfium2.PdfDocument(path)
        self.pt_h = float(self.reader.pages[0].mediabox.height)
        self.pt_w = float(self.reader.pages[0].mediabox.width)
        self.s = width_px / self.pt_w
        self.page_h = int(self.pt_h * self.s)
        self.gap = 16
        self.cache = {}
        self.index = {p.indirect_reference.idnum: i for i, p in enumerate(self.reader.pages)}

    def image(self, i):
        if i not in self.cache:
            self.cache[i] = self.pdfium[i].render(scale=self.s).to_pil().convert("RGB")
        return self.cache[i]

    def doc_y(self, i, y_pt):
        return i * (self.page_h + self.gap) + (self.pt_h - y_pt) * self.s

    def links(self):
        result = []
        for i, p in enumerate(self.reader.pages):
            for ref in p.get("/Annots") or []:
                a = ref.get_object()
                action = a["/A"].get_object()
                x0, y0, x1, y1 = [float(v) for v in a["/Rect"]]
                entry = {"page": i, "rect": (x0, y0, x1, y1)}
                if action["/S"] == "/GoTo":
                    dest = action["/D"]
                    entry.update(to_page=self.index[list.__getitem__(dest, 0).idnum], to_y=float(dest[3]))
                result.append(entry)
        return result

    def outline(self):
        rows = []

        def walk(items, depth):
            for item in items:
                if isinstance(item, list):
                    walk(item, depth + 1)
                else:
                    rows.append((depth, str(item.title), self.reader.get_destination_page_number(item), float(item.top)))
        walk(self.reader.outline, 0)
        return rows


def ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def cursor(frame, x, y, pressed=False):
    points = [(0, 0), (0, 19), (5, 15), (9, 24), (12, 22), (8, 14), (14, 14)]
    draw = ImageDraw.Draw(frame)
    draw.polygon([(x + a + 1, y + b + 1) for a, b in points], fill=(0, 0, 0))
    draw.polygon([(x + a, y + b) for a, b in points], fill=(255, 255, 255), outline=(0, 0, 0))
    if pressed:
        draw.ellipse([x - 16, y - 16, x + 16, y + 16], outline=ACCENT, width=3)


def caption(frame, text, alpha=1.0):
    if alpha <= 0:
        return
    f = font(27, True)
    box = ImageDraw.Draw(frame).textbbox((0, 0), text, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0 = (W - w) // 2 - 26, H - 72
    d.rounded_rectangle([x0, y0, x0 + w + 52, y0 + h + 30], 16, fill=(17, 20, 30, int(238 * alpha)))
    d.text((x0 + 26, y0 + 12), text, font=f, fill=(255, 255, 255, int(255 * alpha)))
    frame.paste(layer, (0, 0), layer)


def key_pill(frame, text, active):
    d = ImageDraw.Draw(frame)
    f = font(30, True)
    d.rounded_rectangle([W - 182, 58, W - 34, 118], 14, fill=ACCENT if active else (255, 255, 255), outline=ACCENT, width=3)
    box = d.textbbox((0, 0), text, font=f)
    d.text((W - 108 - (box[2] - box[0]) / 2, 70), text, font=f, fill=(255, 255, 255) if active else ACCENT)


class Viewer:
    """A plain PDF viewer drawn over the real pages and real bookmarks."""
    SIDE = 280
    TOP = 40

    def __init__(self, doc):
        self.doc = doc
        self.rows = [r for r in doc.outline() if r[0] <= 2][:26]
        self.page_x = self.SIDE + (W - self.SIDE - int(doc.pt_w * doc.s)) // 2

    def row_y(self, n):
        return self.TOP + 36 + n * 24

    def draw(self, scroll, highlight=None, active_row=None):
        frame = Image.new("RGB", (W, H), (74, 79, 92))
        doc = self.doc
        first = max(0, int(scroll // (doc.page_h + doc.gap)))
        for i in range(first, min(first + 3, len(doc.reader.pages))):
            top = i * (doc.page_h + doc.gap) - scroll + self.TOP
            if top < H:
                frame.paste(doc.image(i), (self.page_x, int(top)))
        if highlight:
            page, y_pt, alpha = highlight
            y = doc.doc_y(page, y_pt) - scroll + self.TOP
            layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rectangle(
                [self.page_x, y, self.page_x + doc.pt_w * doc.s, y + 46], fill=ACCENT + (int(60 * alpha),), outline=ACCENT + (int(255 * alpha),), width=2)
            frame.paste(layer, (0, 0), layer)
        d = ImageDraw.Draw(frame)
        d.rectangle([0, 0, self.SIDE, H], fill=(244, 245, 249))
        d.line([self.SIDE, 0, self.SIDE, H], fill=(205, 209, 220))
        d.text((16, self.TOP + 8), "Bookmarks", font=font(15, True), fill=MUTED)
        for n, (depth, title, _, _) in enumerate(self.rows):
            y = self.row_y(n)
            if n == active_row:
                d.rounded_rectangle([8, y - 3, self.SIDE - 8, y + 21], 6, fill=(219, 225, 252))
            text = title if len(title) < 32 - depth * 3 else title[:30 - depth * 3] + "…"
            d.text((16 + depth * 14, y), text, font=font(15, depth == 0), fill=INK)
        d.rectangle([0, 0, W, self.TOP], fill=(232, 234, 240))
        d.line([0, self.TOP, W, self.TOP], fill=(205, 209, 220))
        for n, color in enumerate(((236, 106, 94), (245, 191, 79), (98, 197, 84))):
            d.ellipse([14 + n * 20, 14, 26 + n * 20, 26], fill=color)
        d.text((W // 2 - 50, 11), "handbook.pdf", font=font(16, True), fill=INK)
        return frame


# ---------- Video ----------

def terminal(lines, progress):
    frame = Image.new("RGB", (W, H), DARK)
    d = ImageDraw.Draw(frame)
    d.rounded_rectangle([110, 80, W - 110, H - 110], 16, fill=PANEL)
    for n, color in enumerate(((236, 106, 94), (245, 191, 79), (98, 197, 84))):
        d.ellipse([132 + n * 22, 100, 146 + n * 22, 114], fill=color)
    y = 146
    shown = progress
    for text, color, bold in lines:
        if shown <= 0:
            break
        part = text[:int(shown)]
        d.text((140, y), part, font=font(20, bold, True), fill=color)
        shown -= len(text)
        y += 34
    return frame


def build_video(work, pdf_path, summary):
    doc = Doc(pdf_path, 700)
    view = Viewer(doc)
    links = doc.links()
    outline = doc.outline()
    section = {title.split(" ·")[0]: page for depth, title, page, _ in outline if depth == 0}
    guide_links = [l for l in links if section["Guides"] <= l["page"] < section["Reference"] and l.get("to_page", -1) >= section["Reference"]
                   and l["to_y"] < doc.pt_h - 60 and l["rect"][1] < doc.pt_h - 120]
    click = guide_links[3]
    image_link = next(l for l in links if l["page"] == section["Reference"] and l.get("to_page", -1) >= section.get("Images", 10 ** 6))
    site_shot = Image.open(os.path.join(work, "site_guides.png")).convert("RGB")
    frames = []

    def add(seconds, fn):
        total = int(seconds * FPS)
        for n in range(total):
            frames.append(fn(n / max(1, total - 1), n / FPS))

    # Title
    def title(t, _):
        frame = Image.new("RGB", (W, H), DARK)
        d = ImageDraw.Draw(frame)
        a = ease(t * 3)
        c = tuple(int(v * a) for v in (255, 255, 255))
        d.text((W // 2, 280), "www-2-pdf", font=font(84, True), fill=c, anchor="mm")
        d.text((W // 2, 370), "A static website in one PDF that still behaves like the website",
               font=font(30), fill=tuple(int(v * a) for v in (190, 196, 210)), anchor="mm")
        return frame
    add(2.8, title)

    # The site, disclosures closed
    def site(t, _):
        frame = site_shot.copy()
        caption(frame, "Your site: pages, links, and answers hidden behind clicks", ease(t * 4))
        return frame
    add(3.2, site)

    # One command, real output
    white, green, gray = (235, 238, 245), (120, 220, 150), (150, 158, 175)
    command = "$ python3 www2pdf.py http://localhost:8000/ -o handbook.pdf"
    lines = [(command, white, True), ("", gray, False)] + [(row, green if row.startswith("Done") else gray, False) for row in summary]
    typed = len(command)
    full = sum(len(l[0]) for l in lines)

    def term(t, _):
        progress = typed * ease(t / 0.4) if t < 0.4 else typed + (full - typed) * ease((t - 0.45) / 0.3) if t > 0.45 else typed
        frame = terminal(lines, progress)
        caption(frame, "One command. Dependencies install themselves.", ease((t - 0.1) * 5))
        return frame
    add(5, term)

    # Everything open, with bookmarks
    guides_start = doc.doc_y(section["Guides"], doc.pt_h)
    faq_scroll = doc.doc_y(click["page"], click["rect"][3]) - 300
    def expanded(t, _):
        frame = view.draw(lerp(guides_start, faq_scroll, ease(t * 1.4)))
        caption(frame, "Same look. Everything expanded. Nested bookmarks.", ease(t * 4))
        return frame
    add(4, expanded)

    # Link click
    lx = view.page_x + (click["rect"][0] + click["rect"][2]) / 2 * doc.s
    ly = doc.doc_y(click["page"], click["rect"][1]) - faq_scroll + view.TOP - 8
    target_scroll = doc.doc_y(click["to_page"], click["to_y"]) - 14

    def move_to_link(t, _):
        frame = view.draw(faq_scroll)
        cursor(frame, lerp(760, lx, ease(t)), lerp(620, ly, ease(t)), t > 0.92)
        caption(frame, "Click a link…", ease(t * 4))
        return frame
    add(1.6, move_to_link)

    def jump(t, _):
        frame = view.draw(lerp(faq_scroll, target_scroll, ease(t * 2.2)), (click["to_page"], click["to_y"], ease((t - 0.45) * 3)))
        caption(frame, "…and land on that exact element, inside the file", ease(t * 4))
        cursor(frame, lx, ly)
        return frame
    add(3, jump)

    # Back and forward
    def back(t, _):
        frame = view.draw(lerp(target_scroll, faq_scroll, ease((t - 0.25) * 3)))
        key_pill(frame, "⌘ [", 0.15 < t < 0.4)
        caption(frame, "Back returns you to where you were", ease(t * 5))
        return frame
    add(2.4, back)

    def forward(t, _):
        frame = view.draw(lerp(faq_scroll, target_scroll, ease((t - 0.25) * 3)), (click["to_page"], click["to_y"], ease((t - 0.6) * 3)))
        key_pill(frame, "⌘ ]", 0.15 < t < 0.4)
        caption(frame, "Forward takes you there again", ease(t * 5))
        return frame
    add(2.4, forward)

    # Bookmark
    row_n = next(n for n, r in enumerate(view.rows) if r[0] >= 1 and r[2] >= section["Reference"])
    row = view.rows[row_n]
    book_scroll = doc.doc_y(row[2], row[3]) - 14

    def bookmark(t, _):
        moving = ease(t / 0.4)
        scroll = lerp(target_scroll, book_scroll, ease((t - 0.45) * 3))
        frame = view.draw(scroll, None, row_n if t > 0.4 else None)
        cursor(frame, lerp(lx, 110, moving), lerp(ly, view.row_y(row_n) + 8, moving), 0.38 < t < 0.5)
        caption(frame, "Bookmarks work the same way", ease(t * 5))
        return frame
    add(2.8, bookmark)

    # Image
    img_x = view.page_x + (image_link["rect"][0] + image_link["rect"][2]) / 2 * doc.s
    img_y = doc.doc_y(image_link["page"], (image_link["rect"][1] + image_link["rect"][3]) / 2)
    img_scroll = img_y - 280
    appendix_scroll = doc.doc_y(image_link["to_page"], image_link["to_y"]) - 14

    def image_click(t, _):
        if t < 0.45:
            frame = view.draw(lerp(book_scroll, img_scroll, ease(t / 0.3)))
            cursor(frame, lerp(110, img_x, ease(t / 0.4)), lerp(view.row_y(row_n), 320, ease(t / 0.4)), t > 0.38)
        else:
            frame = view.draw(lerp(img_scroll, appendix_scroll, ease((t - 0.45) * 3)))
        caption(frame, "Images open full size, without a connection", ease(t * 5))
        return frame
    add(3, image_click)

    # End card
    def end(t, _):
        frame = Image.new("RGB", (W, H), DARK)
        d = ImageDraw.Draw(frame)
        a = ease(t * 3)
        fade = lambda c: tuple(int(v * a) for v in c)
        d.text((W // 2, 250), "www-2-pdf", font=font(72, True), fill=fade((255, 255, 255)), anchor="mm")
        d.text((W // 2, 336), "python3 www2pdf.py URL -o site.pdf", font=font(30, False, True), fill=fade((120, 220, 150)), anchor="mm")
        d.text((W // 2, 410), "Free and open source  ·  MIT  ·  Works offline", font=font(26), fill=fade((190, 196, 210)), anchor="mm")
        d.text((W // 2, 470), "github.com/borovetsav/www-2-pdf", font=font(26, True), fill=fade((140, 165, 255)), anchor="mm")
        return frame
    add(3.2, end)

    mp4 = os.path.join(DOCS, "demo.mp4")
    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", "-preset", "slow", "-movflags", "+faststart", mp4], stdin=subprocess.PIPE)
    for frame in frames:
        ffmpeg.stdin.write(frame.tobytes())
    ffmpeg.stdin.close()
    ffmpeg.wait()
    gif = os.path.join(DOCS, "demo.gif")
    subprocess.check_call(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", mp4, "-filter_complex",
         "fps=10,scale=760:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle", gif])
    for n, name in ((0, "frame_title"), (int(FPS * 8), "frame_term"), (int(FPS * 13), "frame_open"), (int(FPS * 17), "frame_jump"), (int(FPS * 19.5), "frame_back"), (int(FPS * 24), "frame_book"), (int(FPS * 26.5), "frame_img"), (len(frames) - 2, "frame_end")):
        frames[min(n, len(frames) - 1)].save(os.path.join(work, name + ".png"))
    return len(frames) / FPS


# ---------- Screenshots ----------

def shadow(img, pad=26):
    out = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), (0, 0, 0, 0))
    glow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).rounded_rectangle([pad, pad + 4, pad + img.width, pad + img.height + 4], 10, fill=(0, 0, 0, 90))
    out = Image.alpha_composite(out, glow.filter(ImageFilter.GaussianBlur(10)))
    out.paste(img, (pad, pad))
    return out


def screenshots(work, pdf_path):
    doc = Doc(pdf_path, 700)
    view = Viewer(doc)
    outline = doc.outline()
    section = {title.split(" ·")[0]: page for depth, title, page, _ in outline if depth == 0}
    view.draw(doc.doc_y(section["Guides"], doc.pt_h) + 330).save(os.path.join(DOCS, "pdf-view.png"))
    closed = Image.open(os.path.join(work, "site_guides.png")).convert("RGB")
    closed.save(os.path.join(DOCS, "site-view.png"))


def main():
    work = os.path.abspath(sys.argv[1])
    site = os.path.join(work, "site")
    os.makedirs(site, exist_ok=True)
    write_site(site)
    server = serve(site)
    pdf = os.path.join(work, "handbook.pdf")
    run = subprocess.run(["python3", os.path.join(ROOT, "www2pdf.py"), "http://localhost:%d/" % PORT, "-o", pdf],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True, check=True)
    keep = ("Done", "Sheets", "Details expanded", "Links with")
    summary = []
    for row in run.stdout.splitlines():
        if row.startswith(keep):
            row = row.replace(pdf, "handbook.pdf")
            # The anchor line is too long for the terminal scene: keep its first two numbers
            summary.append(", ".join(row.split(", ")[:2]) if row.startswith("Links with") else row)
    print(run.stdout)
    screenshot_site(work)
    server.shutdown()
    server.server_close()
    screenshots(work, pdf)
    print("video seconds:", build_video(work, pdf, summary))


if __name__ == "__main__":
    main()
