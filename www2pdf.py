#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""www-2-pdf: static site -> a single PDF that behaves like the site.

Links within the site become jumps within the PDF with explicit coordinates,
so back and forward work in PDF viewers. External links stay URIs.

Usage: python3 www2pdf.py http://example.com/ -o ~/Desktop/site.pdf
The code is Python 3.9 compatible: do not use newer syntax here.
"""
import argparse
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

VENV_DIR = os.environ.get("WWW2PDF_VENV") or os.path.expanduser("~/.www2pdf")
REQUIREMENTS = ["playwright>=1.45", "pypdf>=5.0"]
BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Yandex.app/Contents/MacOS/Yandex",
]
RESOURCE_EXT = {
    ".css", ".js", ".mjs", ".map", ".json", ".xml", ".txt", ".csv", ".ico", ".pdf",
    ".zip", ".gz", ".tar", ".rar", ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt",
    ".pptx", ".mp3", ".mp4", ".webm", ".ogg", ".avi", ".mov", ".woff", ".woff2",
    ".ttf", ".otf", ".eot",
}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}
PT_PER_MM = 72 / 25.4
PT_PER_PX = 0.75
JUMP_HEADROOM_PT = 4  # small gap above the target so it does not stick to the window edge
MIN_PRINTABLE_PT = 144  # with less printable area on the sheet the layout stops making sense
MIN_FIT_SCALE = 0.1   # Chrome cannot go lower
VENV_ENTRIES = {"bin", "include", "lib", "lib64", "pyvenv.cfg", ".gitignore"}  # everything python -m venv creates
MAX_REFRESH_SECONDS = 5  # a <meta refresh> with this delay or less counts as a redirect
MAX_REFRESH_HOPS = 20
DEFAULT_PORTS = {"http": 80, "https": 443}
APPENDIX_TITLE = "Images"
MAX_PDF_SIDE_PT = 14400  # PDF page size limit

# Prepares the page for printing. Returns data for bookmarks and checks.
PREPARE_JS = r"""
async (imageWaitMs) => {
  // Regular disclosures
  document.querySelectorAll('details').forEach(d => { d.removeAttribute('name'); d.open = true; });

  // Accordions and tabs used by other sites
  const show = el => {
    el.removeAttribute('hidden');
    el.removeAttribute('inert');
    el.setAttribute('aria-hidden', 'false');
    el.classList.remove('hidden', 'is-hidden', 'collapsed');
    el.classList.add('show', 'active', 'in', 'open');
    el.style.setProperty('display', getComputedStyle(el).display === 'none' ? 'block' : getComputedStyle(el).display, 'important');
    el.style.setProperty('visibility', 'visible', 'important');
    el.style.setProperty('height', 'auto', 'important');
    el.style.setProperty('max-height', 'none', 'important');
    el.style.setProperty('opacity', '1', 'important');
    el.style.setProperty('overflow', 'visible', 'important');
  };
  document.querySelectorAll('[aria-controls]').forEach(btn => {
    if (btn.hasAttribute('aria-expanded')) btn.setAttribute('aria-expanded', 'true');
    for (const id of (btn.getAttribute('aria-controls') || '').split(/\s+/)) {
      const panel = id && document.getElementById(id);
      if (panel) show(panel);
    }
  });
  document.querySelectorAll('[role=tabpanel], .collapse, .accordion-collapse, .accordion-content, .tab-pane').forEach(show);

  // Site headers must not overlap the text or repeat on every sheet
  for (const el of document.querySelectorAll('body *')) {
    const p = getComputedStyle(el).position;
    if (p === 'fixed' || p === 'sticky') el.style.setProperty('position', 'static', 'important');
  }

  const st = document.createElement('style');
  st.textContent = [
    'pre{white-space:pre-wrap!important}',
    'table{display:table!important;overflow:visible!important}',
    'h1,h2,h3,h4,summary{break-after:avoid}',
    'summary,tr,img{break-inside:avoid}',
  ].join('');
  document.head.appendChild(st);

  // Lazy images and fonts
  const images = [...document.images];
  images.forEach(img => { img.loading = 'eager'; });
  await Promise.all(images.map(img => {
    if (img.complete) return null;
    return new Promise(res => {
      img.addEventListener('load', res, {once: true});
      img.addEventListener('error', res, {once: true});
      setTimeout(res, imageWaitMs);
    });
  }));
  // A stuck font must not hang the run
  if (document.fonts && document.fonts.ready) {
    await Promise.race([document.fonts.ready, new Promise(res => setTimeout(res, imageWaitMs))]);
  }

  // Scrollable blocks get cut off on paper, so show their full content.
  // The layout width on the sheet differs, so expand all of them, not just those overflowing now.
  // Leave overflow:hidden alone: it is styling (round avatars, menus, screen reader text).
  for (const el of document.querySelectorAll('body, body *')) {
    const cs = getComputedStyle(el);
    if ([cs.overflowX, cs.overflowY].some(v => v === 'auto' || v === 'scroll')) {
      el.style.setProperty('overflow', 'visible', 'important');
      el.style.setProperty('height', 'auto', 'important');
      el.style.setProperty('max-height', 'none', 'important');
    }
  }

  // Internal names must not collide with anything already on the page
  let prefix = '__w2p_';
  while (document.documentElement.outerHTML.includes(prefix)) prefix += 'x';

  // Bookmarks: h1-h3 headings and disclosures, nested as in the DOM
  // The browser treats classic <a name> anchors as targets just like ids.
  // Read the value with getAttribute: on a form, el.id may be a field named id.
  const idOf = el => el.getAttribute('id');
  const knownIds = new Set([...document.querySelectorAll('[id]')].map(idOf));
  document.querySelectorAll('a[name]').forEach(a => {
    const name = a.getAttribute('name');
    if (name && !idOf(a) && !knownIds.has(name)) { a.setAttribute('id', name); knownIds.add(name); }
  });
  const allIds = [...document.querySelectorAll('[id]')].map(idOf).filter(Boolean);
  const dup = [...new Set(allIds.filter((id, i) => allIds.indexOf(id) !== i))];

  const outline = [];
  let headingLevel = 0, tmpN = 0;
  const anchorOf = el => {
    const id = idOf(el);
    // An id whose encoded form is another id is unreachable through its service link
    const shadowed = id && encodeURIComponent(id) !== id && allIds.includes(encodeURIComponent(id));
    if (id && !dup.includes(id) && !shadowed) return id;
    const own = prefix + (++tmpN);
    if (!id) { el.setAttribute('id', own); return own; }
    // A duplicate id leads to the first element, so the bookmark needs its own anchor
    const mark = document.createElement('span');
    mark.setAttribute('id', own);
    el.insertBefore(mark, el.firstChild);
    return own;
  };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const el of document.querySelectorAll('h1,h2,h3,summary')) {
    const title = clean(el.textContent);
    if (!title) continue;
    if (el.tagName !== 'SUMMARY' && el.closest('summary')) continue;
    if (el.tagName !== 'SUMMARY') {
      headingLevel = Number(el.tagName[1]);
      outline.push({level: headingLevel, title, anchor: anchorOf(el)});
      continue;
    }
    let depth = -1;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (p.tagName === 'DETAILS') depth++;
      else if (p.tagName === 'LI') {
        const own = p.querySelector(':scope > details');
        if (own && !own.contains(el)) depth++;
      }
    }
    outline.push({level: headingLevel + 1 + Math.max(depth, 0), title, anchor: anchorOf(el)});
  }

  // Pitfall: Chrome writes coordinates only for anchors that some link points to
  const ids = new Set([...document.querySelectorAll('[id]')].map(idOf).filter(Boolean));
  const anchorNames = {};
  const box = document.createElement('div');
  box.style.cssText = 'position:absolute;left:0;top:0;width:1px;height:1px;overflow:hidden;opacity:0';
  for (const id of ids) {
    const a = document.createElement('a');
    a.setAttribute('href', '#' + encodeURIComponent(id));
    anchorNames[id] = encodeURIComponent(id);
    a.style.cssText = 'position:absolute;left:0;top:0;display:block;width:1px;height:1px';
    box.appendChild(a);
  }
  // Internal elements go outside the body flow: its margins and width must not affect the probes
  document.documentElement.appendChild(box);

  // Probe anchors: their link rectangle in the PDF is accurate, but the destination coordinate is not.
  // Comparing the two gives the real transform for this particular page.
  const probes = [0, 200].map((top, i) => {
    const a = document.createElement('a');
    const id = prefix + ['probe_a', 'probe_b'][i];
    a.setAttribute('id', id);
    a.setAttribute('href', '#' + id);
    a.style.cssText = 'position:absolute;left:0;top:' + top + 'px;display:block;width:16px;height:16px';
    document.documentElement.appendChild(a);
    return a;
  });

  // Links: crawl targets, title tooltips, image captions
  // A link inside SVG has a non-string href property, so read the attribute. An empty href is still a link.
  const rawHref = a => a.hasAttribute('href') ? a.getAttribute('href') : a.getAttribute('xlink:href');
  const parse = a => {
    try { return new URL(rawHref(a), document.baseURI); } catch (e) { return null; }
  };
  const linkEls = [...document.querySelectorAll('a, area')].filter(a =>
    rawHref(a) !== null && !box.contains(a) && !probes.includes(a));
  const links = [];
  for (const a of linkEls) {
    const url = parse(a);
    const img = a.querySelector('img');
    let caption = '';
    if (img) {
      // Image caption: the heading of the nearest block that has one
      let head = null;
      for (let p = a.parentElement; p && !head; p = p.parentElement) head = p.querySelector('h1,h2,h3,h4,figcaption');
      const headText = head ? clean(head.textContent) : '';
      caption = headText.includes(img.alt) ? headText : clean(img.alt + ' ' + headText);
    }
    links.push({href: url ? url.href : rawHref(a), title: a.getAttribute('title') || '', caption,
                area: a.tagName === 'AREA'});
  }

  // Number in the URL: Chrome emits such a link as a URI and does not match it against page anchors
  // or <base>, and the number lets us find the exact URL and title of this very link.
  linkEls.forEach((a, i) => {
    const url = parse(a);
    if (!url || !/^https?:$/.test(url.protocol)) return;
    url.search = (url.search ? url.search + '&' : '?') + prefix + i + '=1';
    a.setAttribute('href', url.href);
  });
  return {
    title: document.title || '',
    outline,
    links,
    anchorNames,
    prefix,
    duplicateIds: dup,
    closedDetails: document.querySelectorAll('details:not([open])').length,
    details: document.querySelectorAll('details').length,
    brokenImages: images.filter(i => !i.naturalWidth).map(i => i.src),
  };
}
"""

META_REFRESH_JS = r"""
() => {
  const meta = document.querySelector('meta[http-equiv="refresh" i]');
  if (!meta) return null;
  const match = /^\s*(\d+(?:\.\d+)?)\s*(?:[;,]\s*(?:url\s*=\s*)?['"]?([^'"\s]+))?/i.exec(meta.getAttribute('content') || '');
  if (!match) return null;
  // A refresh with no URL or pointing at the page itself leads nowhere
  const target = match[2] ? new URL(match[2], document.baseURI).href.split('#')[0] : location.href.split('#')[0];
  return {delay: Number(match[1]), moves: target !== location.href.split('#')[0]};
}
"""

# For --long-pages: on paper vh is relative to the sheet height, so the page would never fit
FREEZE_VH_JS = r"""
() => {
  const h = window.innerHeight;
  for (const el of document.querySelectorAll('html, body, body *')) {
    const cs = getComputedStyle(el);
    if (parseFloat(cs.minHeight) === h) el.style.setProperty('min-height', h + 'px', 'important');
    if (parseFloat(cs.height) === h) el.style.setProperty('height', h + 'px', 'important');
  }
}
"""

# For the images appendix: removes blocks whose images failed to load and returns their ids
DROP_BROKEN_IMAGES_JS = r"""
() => {
  const dropped = [];
  for (const section of [...document.querySelectorAll('section')]) {
    const img = section.querySelector('img');
    if (!img.complete || !img.naturalWidth) {
      dropped.push(section.querySelector('h3').getAttribute('id'));
      section.remove();
    } else {
      img.loading = 'eager';
    }
  }
  return dropped;
}
"""


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------- Automatic dependency install ----------

def venv_python():
    return os.path.join(VENV_DIR, "bin", "python")


def deps_ok(python):
    if not os.path.exists(python):
        return False
    try:
        return subprocess.run(
            [python, "-c", "import playwright.sync_api, pypdf"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
    except OSError:  # a broken file instead of python: the venv has to be recreated
        return False


def python_candidates():
    """The current python first; if the packages will not install on it, try others."""
    venv_real = os.path.realpath(VENV_DIR) + os.sep
    base = os.path.join(sys.base_prefix, "bin", "python3")
    names = [base, sys.executable, "python3.13", "python3.12", "python3.11", "python3.10",
             "/usr/bin/python3", "python3"]
    seen, result = set(), []
    for name in names:
        path = shutil.which(name)
        # The venv's own interpreter will not do: the venv is deleted before being created
        if not path or os.path.abspath(path).startswith(venv_real):
            continue
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            result.append(path)
    return result


def clear_venv_dir():
    """Removes only what looks like a clean venv: user files must not be touched."""
    if os.path.lexists(VENV_DIR) and not os.path.isdir(VENV_DIR):
        sys.exit("%s is not a directory, cannot create a venv there. Set another path in WWW2PDF_VENV." % VENV_DIR)
    if os.path.isdir(VENV_DIR) and os.listdir(VENV_DIR):
        extra = set(os.listdir(VENV_DIR)) - VENV_ENTRIES
        if extra or not os.path.exists(os.path.join(VENV_DIR, "pyvenv.cfg")):
            sys.exit("%s contains unrelated files, will not delete the directory. Set another path in WWW2PDF_VENV." % VENV_DIR)
        shutil.rmtree(VENV_DIR, ignore_errors=True)


def ensure_venv():
    """Creates a venv without sudo and restarts the script inside it."""
    if os.path.realpath(sys.prefix) == os.path.realpath(VENV_DIR) and deps_ok(sys.executable):
        return
    if not deps_ok(venv_python()):
        log("Installing dependencies into", VENV_DIR, "(needed only once)...")
        for base in python_candidates():
            clear_venv_dir()
            made = subprocess.run([base, "-m", "venv", VENV_DIR], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if made.returncode != 0:
                log("  ", base, ": could not create venv:", made.stderr.decode("utf-8", "replace")[-300:])
                continue
            installed = subprocess.run(
                [venv_python(), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"] + REQUIREMENTS,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            if installed.returncode == 0 and deps_ok(venv_python()):
                break
            log("  ", base, ": packages failed to install:", installed.stderr.decode("utf-8", "replace")[-300:])
        else:
            sys.exit("Could not install dependencies, see the reasons above. Requires python3, write access to %s and access to PyPI." % VENV_DIR)
    os.execv(venv_python(), [venv_python()] + sys.argv)


def launch_browser(playwright, explicit):
    candidates = [explicit] if explicit else BROWSERS
    for path in candidates:
        if path and os.path.exists(path):
            log("Browser:", path)
            return playwright.chromium.launch(executable_path=path, headless=True)
    if explicit:
        sys.exit("Browser not found: " + explicit)
    log("No installed browser found, downloading Chromium...")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    return playwright.chromium.launch(headless=True)


# ---------- URLs ----------

def normalize_url(url):
    """Removes the fragment; /index.html and / count as the same page."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if posixpath.basename(path).lower() in ("index.html", "index.htm"):
        path = posixpath.dirname(path).rstrip("/") + "/"
    scheme, netloc = parts.scheme.lower(), parts.netloc.lower()
    default = ":%d" % DEFAULT_PORTS.get(scheme, 0)
    if netloc.endswith(default):
        netloc = netloc[:-len(default)]
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def same_site(url, root):
    """An invalid URL counts as external: one malformed link must not crash the run."""
    try:
        a, b = urlsplit(url), urlsplit(root)
        if a.scheme not in ("http", "https"):
            return False
        return (a.hostname, a.port or DEFAULT_PORTS[a.scheme]) == (b.hostname, b.port or DEFAULT_PORTS.get(b.scheme))
    except ValueError:
        return False


def extension(url):
    return posixpath.splitext(urlsplit(url).path)[1].lower()


def link_number(uri, prefix):
    """Link number from the internal marker in the URL, or None."""
    match = re.search(r"[?&]" + re.escape(prefix) + r"(\d+)=1", uri)
    return int(match.group(1)) if match else None


def clean_dest_name(name):
    """PDF destination name -> original id. Non-ASCII text arrives as UTF-8 read as latin-1."""
    name = str(name).lstrip("/")
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def find_anchor(anchors, frag):
    """Same as a browser: exact id match first, then the decoded fragment."""
    return anchors.get(frag) or anchors.get(unquote(frag))


# ---------- Printing ----------

def pdf_options(args, width_pt=None, height_pt=None, scale=None):
    margin = "%gmm" % args.margin
    options = {
        "print_background": True,
        "scale": scale if scale is not None else args.scale,
        "margin": {"top": margin, "right": margin, "bottom": margin, "left": margin},
    }
    if width_pt:
        options.update(width="%.3fin" % (width_pt / 72), height="%.3fin" % (height_pt / 72))
    else:
        options["format"] = args.paper
    return options


def paper_size_pt(paper):
    return {"A4": (595.92, 842.88), "Letter": (612.0, 792.0)}[paper]


def print_page(page, path, args):
    """Prints the page. Returns the scale, reduced for a page wider than the sheet so that nothing is cut off."""
    from pypdf import PdfReader
    width_pt = paper_size_pt(args.paper)[0]
    margin_pt = args.margin * PT_PER_MM
    printable = width_pt - 2 * margin_pt
    scale = args.scale
    layout_px = printable / (PT_PER_PX * scale)
    page.set_viewport_size({"width": int(layout_px), "height": 900})
    content_px = page.evaluate("document.documentElement.scrollWidth")
    if content_px > layout_px + 1:
        scale = max(MIN_FIT_SCALE, int(printable / (PT_PER_PX * content_px) * 1000) / 1000)
    if not args.long_pages:
        page.set_viewport_size({"width": 1280, "height": 900})
        page.pdf(path=path, **pdf_options(args, scale=scale))
        return scale
    page.set_viewport_size({"width": int(printable / (PT_PER_PX * scale)), "height": 900})
    page.evaluate(FREEZE_VH_JS)
    doc_px = page.evaluate("document.documentElement.scrollHeight")
    height_pt = doc_px * PT_PER_PX * scale + 2 * margin_pt + 2
    if height_pt > MAX_PDF_SIDE_PT:
        log("  page exceeds the PDF size limit, it will take several sheets")
        height_pt = MAX_PDF_SIDE_PT
    page.pdf(path=path, **pdf_options(args, width_pt, height_pt, scale))
    sheets = len(PdfReader(path).pages)
    if sheets > 1 and height_pt < MAX_PDF_SIDE_PT:
        # The layout can be taller on paper than on screen: retry with extra room
        height_pt = min(height_pt * sheets, MAX_PDF_SIDE_PT)
        page.pdf(path=path, **pdf_options(args, width_pt, height_pt, scale))
    page.set_viewport_size({"width": 1280, "height": 900})
    return scale


def open_url(page, candidates, timeout_ms):
    """Opens the first working URL. Returns (response, whether load was reached, failure reason)."""
    response, why = None, None
    for url in candidates:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            response, why = None, why or "load error: %s" % str(exc).split("\n")[0]
            continue
        if response is not None and response.status < 400:
            return response, wait_load(page, timeout_ms), None
        why = why or "HTTP %s" % (response.status if response else "?")
    return None, False, why


def wait_load(page, timeout_ms):
    """A stuck image must not drop the page: print whatever has loaded."""
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
        return True
    except Exception:
        return False


def follow_client_redirects(page, timeout_ms):
    """Follows <meta refresh> redirects with a short delay. Disables other refreshes."""
    for _ in range(MAX_REFRESH_HOPS):
        try:
            refresh = page.evaluate(META_REFRESH_JS)
        except Exception:  # the page is navigating to a new URL right now
            wait_load(page, timeout_ms)
            continue
        if refresh is None:
            return
        if refresh["delay"] > MAX_REFRESH_SECONDS or not refresh["moves"]:
            break
        current = page.url
        try:
            page.wait_for_url(lambda u: u != current, timeout=(refresh["delay"] + 3) * 1000, wait_until="domcontentloaded")
        except Exception:
            break
        wait_load(page, timeout_ms)
    freeze_refresh(page, timeout_ms)


def freeze_refresh(page, timeout_ms):
    """A page that refreshes itself would break printing: load it without that tag."""
    def strip(route):
        response = route.fetch()
        body = re.sub(r"<meta[^>]*http-equiv\s*=\s*[\"']?refresh[^>]*>", "", response.text(), flags=re.I)
        route.fulfill(response=response, body=body)
    try:
        page.route(page.url.split("#")[0], strip)
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        wait_load(page, timeout_ms)
    except Exception as exc:
        log("  could not disable page refresh: %s" % str(exc).split("\n")[0])


def prepare_and_print(page, path, args, timeout_ms, loaded):
    """A JS redirect can fire at any moment, so on failure retry on the new page."""
    image_wait = min(timeout_ms, 60000) if loaded else 1000
    last_error = None
    for _ in range(3):
        try:
            info = page.evaluate(PREPARE_JS, image_wait)
            info["scale"] = print_page(page, path, args)
            return info, None
        except Exception as exc:
            last_error = str(exc).split("\n")[0]
            wait_load(page, timeout_ms)
    return None, last_error


def render_site(context, args, tmp_dir):
    """Breadth-first crawl. Each site page produces one temporary PDF."""
    root = normalize_url(args.url)
    queue = deque([(root, args.url.split("#")[0])])
    seen = {root}
    sections, skipped, aliases, alias_fragments = [], [], {}, {}
    images, slow, areas = {}, [], []
    limit_hit = []
    timeout_ms = args.timeout * 1000

    def visit(page, url, raw):
        statuses = {}

        def remember_status(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                statuses["last"] = response.status
        page.on("response", remember_status)
        # A server may serve only /dir/index.html or only /dir/
        attempts = [raw] + [u for u in (url, url + "index.html" if url.endswith("/") else None) if u and u != raw]
        response, loaded, why = open_url(page, attempts, timeout_ms)
        if response is None:
            skipped.append((url, why))
            return
        ctype = response.headers.get("content-type", "").lower()
        if "html" not in ctype:
            skipped.append((url, "not HTML: %s" % ctype))
            return
        follow_client_redirects(page, timeout_ms)
        if statuses.get("last", 200) >= 400:
            skipped.append((url, "HTTP %s after redirect: %s" % (statuses["last"], page.url)))
            return
        final = normalize_url(page.url)
        if not same_site(final, root):
            skipped.append((url, "redirect to another site: %s" % page.url))
            return
        aliases[url] = final
        final_frag = urlsplit(page.url).fragment
        if final_frag:
            alias_fragments[url] = final_frag
        seen.add(final)
        if any(s["url"] == final for s in sections):
            return
        if not loaded:
            slow.append(final)
        path = os.path.join(tmp_dir, "part%04d.pdf" % len(sections))
        info, error = prepare_and_print(page, path, args, timeout_ms, loaded)
        if info is None:
            skipped.append((final, "could not print: %s" % error))
            return
        sections.append({"url": final, "path": path, "info": info})
        log("  [%d] %s: details %d, links %d" % (len(sections), final, info["details"], len(info["links"])))
        for link in info["links"]:
            href = link["href"]
            if not same_site(href, root):
                continue
            target = normalize_url(href)
            if link["area"]:
                areas.append((final, href))
            ext = extension(target)
            if ext in IMAGE_EXT:
                captions = images.setdefault(target, [])
                if link["caption"] and link["caption"] not in captions:
                    captions.append(link["caption"])
            elif ext not in RESOURCE_EXT and target not in seen:
                seen.add(target)
                queue.append((target, href.split("#")[0]))

    while queue:
        url, raw = queue.popleft()
        if len(sections) >= args.max:
            limit_hit.append(url)
            continue
        # A separate tab per page: a navigation failure must not break the following ones
        page = context.new_page()
        try:
            page.emulate_media(media="screen", color_scheme="light")
            visit(page, url, raw)
        finally:
            page.close()
    return {"sections": sections, "skipped": skipped, "aliases": aliases, "alias_fragments": alias_fragments,
            "images": images, "limit_hit": limit_hit, "slow": slow, "areas": areas}


def render_images_appendix(context, args, tmp_dir, images, root):
    """Images the site links to go into the images appendix, so a click works offline."""
    width_pt, height_pt = paper_size_pt(args.paper)
    margin_pt = args.margin * PT_PER_MM
    px_per_pt = 1 / (PT_PER_PX * args.scale)
    max_w = int((width_pt - 2 * margin_pt) * px_per_pt) - 2
    max_h = int((height_pt - 2 * margin_pt) * px_per_pt) - 120
    blocks = []
    for n, (url, captions) in enumerate(images.items()):
        text = " · ".join(captions) or unquote(posixpath.basename(urlsplit(url).path))
        blocks.append(
            '<section><h3 id="w2p-img-%d" style="margin:0 0 8px;font-size:16px">%s</h3>'
            '<img src="%s" style="display:block;max-width:%dpx;max-height:%dpx"></section>'
            % (n, escape_html(text), quote(url, safe=":/?&=%#"), max_w, max_h)
        )
    # A break before every block except the first: this avoids a blank sheet at the end
    html = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Images</title>'
            '<style>section+section{break-before:page}section{break-inside:avoid}</style></head>'
            '<body style="margin:0;background:#fff;color:#22201c;font:16px/1.4 -apple-system,Arial,sans-serif">'
            + "".join(blocks) + "</body></html>")
    ids = {u: "w2p-img-%d" % n for n, u in enumerate(images)}
    page = context.new_page()
    page.emulate_media(media="screen", color_scheme="light")
    timeout_ms = args.timeout * 1000
    try:
        # The site URL serves as the page base so that images load from it
        page.route(root, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
        page.goto(root, wait_until="domcontentloaded", timeout=timeout_ms)
        loaded = wait_load(page, timeout_ms)
        dropped = set(page.evaluate(DROP_BROKEN_IMAGES_JS))
        broken = [u for u, anchor in ids.items() if anchor in dropped]
        if len(broken) == len(ids):
            return {"path": None, "broken": broken}
        info = page.evaluate(PREPARE_JS, min(timeout_ms, 60000) if loaded else 1000)
        path = os.path.join(tmp_dir, "images.pdf")
        page.pdf(path=path, **pdf_options(args))
    except Exception as exc:
        # An appendix failure must not lose finished sections: the links stay URIs
        log("Could not build the images appendix: %s" % str(exc).split("\n")[0])
        return {"path": None, "broken": list(ids)}
    finally:
        page.close()
    return {"path": path, "info": info, "ids": ids, "broken": broken}


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Merging and links ----------

def measure_transform(reader, margin_pt, scale, prefix):
    """Pitfall: Chrome's anchor coordinates do not match the real ones on the sheet.

    It writes them as if scale = 1; whether the top margin is included depends on the version (Chrome 156
    does not add it), and Chrome also shrinks a page wider than the sheet by itself. So the correction
    is measured per page: true y from top = shift + PDF y from top * factor.
    """
    probe_a, probe_b = prefix + "probe_a", prefix + "probe_b"
    try:
        dests = {clean_dest_name(k): v for k, v in reader.named_destinations.items()}
        real, raw = {}, {}
        for page in reader.pages:
            height = float(page.mediabox.height)
            for ref in page.get("/Annots") or []:
                annot = ref.get_object()
                name = annot_dest_name(annot)
                if name in (probe_a, probe_b):
                    real[name] = height - max(float(annot["/Rect"][1]), float(annot["/Rect"][3]))
                    raw[name] = height - float(dests[name].top)
        factor = (real[probe_b] - real[probe_a]) / (raw[probe_b] - raw[probe_a])
        shift = real[probe_a] - raw[probe_a] * factor
        if factor <= 0:
            raise ValueError(factor)
    except (KeyError, ZeroDivisionError, TypeError, ValueError, IndexError) as exc:
        # Values for Chrome 156: probes may fail if the site overrides the layout itself
        log("  could not measure coordinates (%r), jumps may be inaccurate" % (exc,))
        return {"shift": margin_pt, "factor": scale, "measured": False}
    return {"shift": shift, "factor": factor, "measured": True}


def annot_dest_name(annot):
    """Destination name of a link annotation, or None if the destination is not named."""
    action = annot.get("/A")
    name = annot.get("/Dest")
    if name is None and action is not None:
        action = action.get_object()
        if action.get("/S") == "/GoTo":
            name = action.get("/D")
    return clean_dest_name(name) if isinstance(name, str) else None


def read_anchors(reader, start, transform, anchor_names):
    """Section destinations -> (by id, by PDF name): values are (sheet number, y).

    Chrome names a destination after the raw link fragment. The internal link to each id
    is encoded in a known way, so ids and names are matched exactly: a page
    can have both id="a b" and id="a%20b".
    """
    names = {}
    for name, dest in reader.named_destinations.items():
        try:
            index = reader.get_destination_page_number(dest)
            raw = float(dest.top)
        except (TypeError, ValueError, AttributeError):
            continue
        if index is None or index < 0:
            continue
        height = float(reader.pages[index].mediabox.height)
        y = height - (transform["shift"] + (height - raw) * transform["factor"])
        names[clean_dest_name(name)] = (start + index, max(0, min(height, y + JUMP_HEADROOM_PT)))
    return {i: names[n] for i, n in anchor_names.items() if n in names}, names


def build_pdf(crawl, appendix, args):
    """Merges the sections, builds bookmarks and recreates all links."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, DictionaryObject, Fit, FloatObject, NameObject,
                               NullObject, NumberObject, TextStringObject)

    sections, aliases, alias_fragments = crawl["sections"], crawl["aliases"], crawl["alias_fragments"]
    margin_pt = args.margin * PT_PER_MM
    writer = PdfWriter()
    parts = list(sections)
    if appendix:
        parts.append({"url": None, "path": appendix["path"], "info": appendix["info"]})
    clipped, shrunk = [], []
    for part in parts:
        part["reader"] = PdfReader(part["path"])
        part["start"] = len(writer.pages)
        scale = part["info"].get("scale", args.scale)
        part["transform"] = measure_transform(part["reader"], margin_pt, scale, part["info"]["prefix"])
        if part["url"] and part["transform"]["factor"] < args.scale - 0.005:
            shrunk.append((part["url"], part["transform"]["factor"]))
        # Even at the minimum scale a page may not fit
        if part["url"] and part["transform"]["factor"] < scale - 0.005:
            clipped.append(part["url"])
        part["anchors"], part["names"] = read_anchors(part["reader"], part["start"], part["transform"],
                                                      part["info"]["anchorNames"])
        # Pitfall: merging can lose links, so annotations are recreated by hand below
        writer.append(part["reader"], import_outline=False, excluded_fields=["/B", "/Annots"])

    by_url = {s["url"]: s for s in sections}
    root = normalize_url(args.url)
    stats = {
        "goto_anchor": 0, "goto_section": 0, "goto_same_page": 0, "goto_image": 0, "uri_external": 0,
        "uri_own_kept": 0, "hidden_removed": 0, "other": 0,
    }
    own_kept = []

    def page_top(index):
        return float(writer.pages[index].mediabox.height)

    def goto(index, y):
        return DictionaryObject({
            NameObject("/S"): NameObject("/GoTo"),
            NameObject("/D"): ArrayObject([
                writer.pages[index].indirect_reference, NameObject("/XYZ"),
                NumberObject(0), FloatObject(int(y * 1000) / 1000), NullObject(),
            ]),
        })

    def resolve_target(uri):
        """URL on this site -> (section URL, fragment). A fragment from a redirect wins, same as in a browser."""
        target = normalize_url(uri)
        frag = alias_fragments.get(target) or urlsplit(uri).fragment
        return aliases.get(target, target), frag

    def resolve_uri(uri, source):
        """URI -> (action, stats kind)."""
        if not same_site(uri, root):
            return None, "uri_external"
        target, frag = resolve_target(uri)
        if appendix and target in appendix["ids"]:
            a = appendix_part["anchors"].get(appendix["ids"][target])
            if a:
                return goto(a[0], a[1]), "goto_image"
        part = by_url.get(target)
        if part is None:
            own_kept.append((source["url"], uri))
            return None, "uri_own_kept"
        if frag:
            a = find_anchor(part["anchors"], frag)
            if a:
                return goto(a[0], a[1]), "goto_same_page" if part is source else "goto_anchor"
        return goto(part["start"], page_top(part["start"])), "goto_section"

    appendix_part = parts[-1] if appendix else None
    for part in parts:
        links = part["info"]["links"]
        # Tooltips for non-http links: mailto and the like arrive without the internal marker
        other_titles = {l["href"]: l["title"] for l in links if l["title"]}
        for offset, src_page in enumerate(part["reader"].pages):
            out_page = writer.pages[part["start"] + offset]
            fresh = []
            seen_annots = set()
            for ref in src_page.get("/Annots") or []:
                annot = ref.get_object()
                if annot.get("/Subtype") != "/Link":
                    continue
                rect = [float(v) for v in annot["/Rect"]]
                # Real http links always arrive as URIs, only internal ones have named destinations
                if annot_dest_name(annot) is not None:
                    stats["hidden_removed"] += 1
                    continue
                action_src = annot.get("/A")
                action_src = action_src.get_object() if action_src is not None else None
                if action_src is None or action_src.get("/S") != "/URI":
                    stats["other"] += 1
                    continue
                uri = str(action_src["/URI"])
                number = link_number(uri, part["info"]["prefix"])
                if number is not None:
                    uri, title = links[number]["href"], links[number]["title"]
                else:
                    title = other_titles.get(uri)
                # Chrome sometimes writes the same link on a sheet twice
                annot_key = (tuple(round(v, 1) for v in rect), uri)
                if annot_key in seen_annots:
                    continue
                seen_annots.add(annot_key)
                action, kind = resolve_uri(uri, part)
                if action is None:
                    action = DictionaryObject({NameObject("/S"): NameObject("/URI"),
                                               NameObject("/URI"): TextStringObject(uri)})
                stats[kind] += 1
                new = DictionaryObject({
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Link"),
                    NameObject("/Rect"): ArrayObject([FloatObject(round(v, 3)) for v in rect]),
                    NameObject("/Border"): ArrayObject([NumberObject(0)] * 3),
                    NameObject("/A"): action,
                })
                if title:
                    new[NameObject("/Contents")] = TextStringObject(title)
                fresh.append(writer._add_object(new))
            if fresh:
                out_page[NameObject("/Annots")] = ArrayObject(fresh)
            elif "/Annots" in out_page:
                del out_page["/Annots"]

    add_outline(writer, parts, appendix_part, Fit)

    # Pitfall: nothing named may remain, so that ids from different sections do not conflict
    catalog = writer._root_object
    names = catalog.get("/Names")
    if names is not None:
        names = names.get_object()
        if "/Dests" in names:
            del names["/Dests"]
        if not names:
            del catalog["/Names"]
    if "/Dests" in catalog:
        del catalog["/Dests"]
    catalog[NameObject("/PageMode")] = NameObject("/UseOutlines")
    site_title = sections[0]["info"]["title"] if sections else ""
    writer.add_metadata({"/Title": site_title or args.url, "/Creator": "www-2-pdf"})
    dedupe_images(writer)
    output = os.path.expanduser(args.output)
    # A failure in the middle of writing must not corrupt the previous file
    temp_output = output + ".tmp"
    writer.write(temp_output)
    os.replace(temp_output, output)
    # Count logical links from the DOM: a multi-line link yields several rectangles in the PDF
    anchored = {"total": 0, "exact": 0, "missing": [], "inexact": 0}
    for part in sections:
        for link in part["info"]["links"]:
            href = link["href"]
            if link["area"] or not same_site(href, root):
                continue
            target, frag = resolve_target(href)
            if frag and target in by_url:
                anchored["total"] += 1
                if not find_anchor(by_url[target]["anchors"], frag):
                    anchored["missing"].append((part["url"], unquote(href)))
                elif by_url[target]["transform"]["measured"]:
                    anchored["exact"] += 1
                else:
                    anchored["inexact"] += 1
    return {"pages": len(writer.pages), "links": stats, "anchored": anchored, "own_kept": own_kept,
            "output": output, "clipped": clipped, "shrunk": shrunk,
            "transforms": {p["url"] or "images": p["transform"] for p in parts}}


def dedupe_images(writer):
    """An image used in both a section and the images appendix is stored once.

    pypdf's built-in deduplication does not catch this: copies from different temporary PDFs
    reference different color profiles even though their JPEG data is byte-identical.
    """
    from pypdf.generic import NameObject
    known = {}
    seen_resources = set()

    def walk(resources):
        resources = resources.get_object() if resources is not None else None
        if resources is None or id(resources) in seen_resources:
            return
        seen_resources.add(id(resources))
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return
        xobjects = xobjects.get_object()
        for key in list(xobjects.keys()):
            ref = xobjects.raw_get(key)
            obj = ref.get_object()
            if obj.get("/Subtype") == "/Form":
                walk(obj.get("/Resources"))
            elif obj.get("/Subtype") == "/Image" and "/SMask" not in obj:
                data = obj.get_data() if obj.get("/Filter") is None else obj._data
                fingerprint = (hashlib.sha1(data).hexdigest(), int(obj["/Width"]), int(obj["/Height"]), str(obj.get("/Filter")))
                xobjects[NameObject(key)] = known.setdefault(fingerprint, ref)

    for page in writer.pages:
        walk(page.get("/Resources"))
    try:
        writer.compress_identical_objects(remove_duplicates=True, remove_unreferenced=True)
    except TypeError:  # older pypdf versions
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)


def add_outline(writer, parts, appendix_part, Fit):
    for part in parts:
        is_appendix = part is appendix_part
        title = APPENDIX_TITLE if is_appendix else (part["info"]["title"] or part["url"])
        height = float(writer.pages[part["start"]].mediabox.height)
        top = writer.add_outline_item(title, part["start"], fit=Fit.xyz(left=0, top=height, zoom=None),
                                      is_open=not is_appendix)
        stack = [(0, top)]
        for item in part["info"]["outline"]:
            anchor = part["anchors"].get(item["anchor"])
            if not anchor:
                continue
            while stack[-1][0] >= item["level"]:
                stack.pop()
            node = writer.add_outline_item(item["title"], anchor[0], parent=stack[-1][1],
                                           fit=Fit.xyz(left=0, top=anchor[1], zoom=None),
                                           is_open=item["level"] <= 1)
            stack.append((item["level"], node))


# ---------- Entry point ----------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Static site -> a single PDF with jumps inside the file.")
    p.add_argument("url", help="URL of the home page")
    p.add_argument("-o", "--output", required=True, help="where to save the PDF")
    p.add_argument("--scale", type=float, default=0.75, help="print scale, 0.1-2 (default 0.75)")
    p.add_argument("--paper", choices=["A4", "Letter"], default="A4", help="sheet format")
    p.add_argument("--margin", type=float, default=8, help="margins in mm (default 8)")
    p.add_argument("--max", type=int, default=500, help="maximum number of site pages")
    p.add_argument("--long-pages", action="store_true", help="one long PDF page per site page")
    p.add_argument("--no-images", action="store_true", help="do not add the images appendix")
    p.add_argument("--chrome", help="path to a Chromium-based browser")
    p.add_argument("--timeout", type=int, default=120, help="page load timeout, in seconds")
    p.add_argument("--report", help="save a report as JSON")
    args = p.parse_args(argv)
    if not 0.1 <= args.scale <= 2:
        p.error("--scale must be between 0.1 and 2")
    if args.margin < 0 or min(paper_size_pt(args.paper)) - 2 * args.margin * PT_PER_MM < MIN_PRINTABLE_PT:
        p.error("--margin must be at least 0 and leave at least 5 cm printable on the sheet")
    if args.max < 1:
        p.error("--max must be at least 1")
    if args.timeout < 1:
        p.error("--timeout must be at least 1")
    try:
        if urlsplit(args.url).scheme not in ("http", "https") or not urlsplit(args.url).hostname:
            p.error("the URL must start with http:// or https://")
    except ValueError as exc:
        p.error("invalid URL %s: %s" % (args.url, exc))
    # Better to learn about a bad path right away than after the whole crawl
    for path in filter(None, (args.output, args.report)):
        path = os.path.expanduser(path)
        folder = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            p.error("could not create directory %s: %s" % (folder, exc))
        if os.path.isdir(path) or not os.access(folder, os.W_OK):
            p.error("cannot write file: %s" % path)
    return args


def main():
    args = parse_args(sys.argv[1:])
    ensure_venv()
    from playwright.sync_api import sync_playwright

    tmp_dir = tempfile.mkdtemp(prefix="www2pdf-")
    try:
        with sync_playwright() as pw:
            browser = launch_browser(pw, args.chrome)
            context = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
            crawl = render_site(context, args, tmp_dir)
            sections = crawl["sections"]
            if not sections:
                sys.exit("Could not load any page: %s" % (crawl["skipped"],))
            appendix, broken_images = None, []
            if crawl["images"] and not args.no_images:
                log("Images appendix: %d" % len(crawl["images"]))
                appendix = render_images_appendix(context, args, tmp_dir, crawl["images"], normalize_url(args.url))
                broken_images = appendix["broken"]
                if not appendix["path"]:
                    appendix = None
            browser.close()
        result = build_pdf(crawl, appendix, args)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    links = result["links"]
    print("Done: %s" % result["output"])
    print("Sheets: %d, size: %.1f MB" % (result["pages"], os.path.getsize(result["output"]) / 1e6))
    print("Sections: %s" % ", ".join(s["url"] for s in sections))
    print("Details expanded: %d, left closed: %d" % (
        sum(s["info"]["details"] for s in sections), sum(s["info"]["closedDetails"] for s in sections)))
    anchored = result["anchored"]
    print("Links with an anchor on this site: %d, exactly on the element: %d, no target on the site: %d (they go to the section start)"
          % (anchored["total"], anchored["exact"], len(anchored["missing"])))
    print("Link rectangles in the PDF: to an anchor %d, within the page %d, to a section start %d, to images %d, "
          "external %d" % (links["goto_anchor"], links["goto_same_page"], links["goto_section"], links["goto_image"],
                          links["uri_external"]))
    if anchored["inexact"]:
        print("  WARNING: %d jumps may be inaccurate, could not measure coordinates" % anchored["inexact"])
    if links["hidden_removed"]:
        print("  internal links removed: %d" % links["hidden_removed"])
    grouped = {}
    for src, href in anchored["missing"]:
        grouped.setdefault(href, []).append(src)
    for href in sorted(grouped):
        print("  no target: %s (links: %d)" % (href, len(grouped[href])))
    for src, href in result["own_kept"]:
        print("  stays a link to the internet: %s -> %s" % (src, href))
    for url in crawl["limit_hit"]:
        print("  left out because of --max: %s" % url)
    for url, why in crawl["skipped"]:
        print("  skipped: %s (%s)" % (url, why))
    for url in crawl["slow"]:
        print("  did not load completely, printed what was ready: %s" % url)
    for url, factor in result["shrunk"]:
        print("  page wider than the sheet, scale reduced to %.3f, text is smaller: %s" % (factor, url))
    for url in result["clipped"]:
        print("  WARNING: %s still does not fit the width, part of it may be cut off" % url)
    for src, href in crawl["areas"]:
        print("  image map area is not clickable in the PDF: %s -> %s" % (src, href))
    for url in broken_images:
        print("  linked image failed to load, the link stays pointed at the internet: %s" % url)
    for s in sections:
        if s["info"]["duplicateIds"]:
            print("  duplicate ids on %s: %s (the jump goes to the first one)" % (s["url"], ", ".join(s["info"]["duplicateIds"])))
        for src in s["info"]["brokenImages"]:
            print("  image failed to load: %s" % src)
    if args.report:
        report = dict(result, sections=[s["url"] for s in sections], skipped=crawl["skipped"], limit_hit=crawl["limit_hit"],
                      details=sum(s["info"]["details"] for s in sections),
                      closed_details=sum(s["info"]["closedDetails"] for s in sections))
        with open(os.path.expanduser(args.report), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
