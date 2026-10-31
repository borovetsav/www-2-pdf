#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clickable-www2pdf: статичный сайт -> один PDF, который ведёт себя как сайт.

Ссылки внутри сайта становятся переходами внутри PDF с явными координатами,
поэтому в просмотрщиках работают «назад» и «вперёд». Внешние ссылки остаются URI.

Запуск: python3 www2pdf.py http://example.com/ -o ~/Desktop/site.pdf
Код совместим с Python 3.9: новый синтаксис здесь не использовать.
"""
import argparse
import hashlib
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

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
JUMP_HEADROOM_PT = 4  # небольшой зазор над целью, чтобы она не прилипала к краю окна
TINY_LINK_PT = 2      # служебные невидимые ссылки меньше этого размера удаляются
MISSING_MARK = "__w2p_missing"  # метка ссылок на id, которого нет на своей же странице
PROBE_IDS = ("__w2p_probe_a", "__w2p_probe_b")  # пробные якоря для измерения координат
MAX_PDF_SIDE_PT = 14400  # предел размера страницы в PDF

# Подготовка страницы к печати. Возвращает данные для закладок и проверок.
PREPARE_JS = r"""
async () => {
  // Обычные раскрывашки
  document.querySelectorAll('details').forEach(d => { d.removeAttribute('name'); d.open = true; });

  // Аккордеоны и вкладки других сайтов
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

  // Шапки не должны наезжать на текст и повторяться на каждом листе
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

  // Ленивые картинки и шрифты
  const images = [...document.images];
  images.forEach(img => { img.loading = 'eager'; });
  await Promise.all(images.map(img => {
    if (img.complete) return null;
    return new Promise(res => {
      img.addEventListener('load', res, {once: true});
      img.addEventListener('error', res, {once: true});
      setTimeout(res, 60000);
    });
  }));
  if (document.fonts && document.fonts.ready) await document.fonts.ready;

  // Закладки: заголовки h1-h3 и раскрывашки с вложенностью как в DOM
  const outline = [];
  let headingLevel = 0, tmpN = 0;
  const anchorOf = el => {
    if (!el.id) el.id = '__w2p_' + (++tmpN);
    return el.id;
  };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const el of document.querySelectorAll('h1,h2,h3,summary')) {
    const title = clean(el.textContent);
    if (!title) continue;
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

  // Грабли №1: Chrome пишет координаты только для якорей, на которые есть ссылка
  const allIds = [...document.querySelectorAll('[id]')].map(e => e.id).filter(Boolean);
  const ids = new Set(allIds);
  const dup = [...new Set(allIds.filter((id, i) => allIds.indexOf(id) !== i))];
  const box = document.createElement('div');
  box.style.cssText = 'position:absolute;left:0;top:0;width:1px;height:1px;overflow:hidden;opacity:0';
  for (const id of ids) {
    const a = document.createElement('a');
    a.href = '#' + encodeURIComponent(id);
    a.style.cssText = 'position:absolute;left:0;top:0;display:block;width:1px;height:1px';
    box.appendChild(a);
  }
  document.body.appendChild(box);

  // Пробные якоря: прямоугольник их ссылки в PDF честный, а координата назначения нет.
  // Сравнивая их, узнаём настоящее преобразование именно для этой страницы.
  [0, 200].forEach((top, i) => {
    const a = document.createElement('a');
    a.id = ['__w2p_probe_a', '__w2p_probe_b'][i];
    a.href = '#' + a.id;
    a.style.cssText = 'position:absolute;left:0;top:' + top + 'px;display:block;width:16px;height:16px';
    box.parentNode.appendChild(a);
  });

  // Ссылки: цели для обхода, подсказки title, подписи к картинкам
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (box.contains(a) || a.id.startsWith('__w2p_probe')) continue;
    const img = a.querySelector('img');
    let caption = '';
    if (img) {
      // Подпись картинки: заголовок ближайшего блока, в котором он есть
      let head = null;
      for (let p = a.parentElement; p && !head; p = p.parentElement) head = p.querySelector('h1,h2,h3,h4,figcaption');
      const headText = head ? clean(head.textContent) : '';
      caption = headText.includes(img.alt) ? headText : clean(img.alt + ' ' + headText);
    }
    links.push({href: a.href, title: a.getAttribute('title') || '', caption});
  }
  // Ссылку на несуществующий id своей же страницы Chrome выбрасывает из PDF.
  // С меткой в адресе она придёт как URI и поведёт на начало раздела, как в браузере.
  const here = location.href.split('#')[0];
  for (const a of document.querySelectorAll('a[href]')) {
    if (box.contains(a) || a.id.startsWith('__w2p_probe') || !a.hash || a.href.split('#')[0] !== here) continue;
    let id = a.hash.slice(1);
    try { id = decodeURIComponent(id); } catch (e) {}
    if (!ids.has(id)) a.href = here + (location.search ? '&' : '?') + '__w2p_missing=1' + a.hash;
  }
  return {
    title: document.title || '',
    outline,
    links,
    duplicateIds: dup,
    closedDetails: document.querySelectorAll('details:not([open])').length,
    details: document.querySelectorAll('details').length,
    brokenImages: images.filter(i => !i.naturalWidth).map(i => i.src),
  };
}
"""

def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------- Автоустановка зависимостей ----------

def venv_python():
    return os.path.join(VENV_DIR, "bin", "python")


def deps_ok(python):
    if not os.path.exists(python):
        return False
    return subprocess.run(
        [python, "-c", "import playwright.sync_api, pypdf"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def python_candidates():
    """Сначала текущий python; если на нём пакеты не встанут, пробуем другие."""
    names = [sys.executable, "python3.13", "python3.12", "python3.11", "python3.10",
             "/usr/bin/python3", "python3"]
    seen, result = set(), []
    for name in names:
        path = shutil.which(name)
        real = os.path.realpath(path) if path else None
        if real and real not in seen:
            seen.add(real)
            result.append(path)
    return result


def ensure_venv():
    """Создаёт venv без sudo и перезапускает скрипт внутри него."""
    if os.path.realpath(sys.prefix) == os.path.realpath(VENV_DIR) and deps_ok(sys.executable):
        return
    if not deps_ok(venv_python()):
        log("Ставлю зависимости в", VENV_DIR, "(нужно один раз)...")
        for base in python_candidates():
            shutil.rmtree(VENV_DIR, ignore_errors=True)
            made = subprocess.run([base, "-m", "venv", VENV_DIR], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if made.returncode != 0:
                log("  ", base, ": не удалось создать venv")
                continue
            installed = subprocess.run(
                [venv_python(), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"] + REQUIREMENTS,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            if installed.returncode == 0 and deps_ok(venv_python()):
                break
            log("  ", base, ": пакеты не установились:", installed.stderr.decode("utf-8", "replace")[-300:])
        else:
            sys.exit("Не удалось установить зависимости. Нужен python3 и доступ к PyPI.")
    os.execv(venv_python(), [venv_python()] + sys.argv)


def launch_browser(playwright, explicit):
    candidates = [explicit] if explicit else BROWSERS
    for path in candidates:
        if path and os.path.exists(path):
            log("Браузер:", path)
            return playwright.chromium.launch(executable_path=path, headless=True)
    if explicit:
        sys.exit("Браузер не найден: " + explicit)
    log("Установленного браузера нет, скачиваю Chromium...")
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
    return playwright.chromium.launch(headless=True)


# ---------- Адреса ----------

def normalize_url(url):
    """Убирает фрагмент; /index.html и / считаются одной страницей."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if posixpath.basename(path).lower() in ("index.html", "index.htm"):
        path = posixpath.dirname(path).rstrip("/") + "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def same_site(url, root):
    a, b = urlsplit(url), urlsplit(root)
    return a.scheme in ("http", "https") and (a.hostname, a.port) == (b.hostname, b.port)


def extension(url):
    return posixpath.splitext(urlsplit(url).path)[1].lower()


def strip_missing_mark(url):
    parts = urlsplit(url)
    query = "&".join(q for q in parts.query.split("&") if not q.startswith(MISSING_MARK))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def fragment(url):
    return unquote(urlsplit(url).fragment)


def clean_dest_name(name):
    """Имя назначения из PDF -> исходный id: имена бывают percent-encoded и в UTF-8."""
    name = str(name).lstrip("/")
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unquote(name)


# ---------- Печать ----------

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
    if args.long_pages:
        width_pt = paper_size_pt(args.paper)[0]
        margin_pt = args.margin * PT_PER_MM
        layout_px = (width_pt - 2 * margin_pt) / (PT_PER_PX * args.scale)
        page.set_viewport_size({"width": int(layout_px), "height": 900})
        doc_px = page.evaluate("document.documentElement.scrollHeight")
        height_pt = doc_px * PT_PER_PX * args.scale + 2 * margin_pt + 2
        if height_pt > MAX_PDF_SIDE_PT:
            log("  страница длиннее предела PDF, она займёт несколько листов")
            height_pt = MAX_PDF_SIDE_PT
        page.pdf(path=path, **pdf_options(args, width_pt, height_pt))
    else:
        page.pdf(path=path, **pdf_options(args))


def render_site(context, args, tmp_dir):
    """Обход в ширину. Каждая страница сайта даёт один временный PDF."""
    root = normalize_url(args.url)
    queue = deque([root])
    seen = {root}
    sections, skipped, aliases = [], [], {}
    images = {}
    limit_hit = []
    page = context.new_page()
    page.emulate_media(media="screen", color_scheme="light")
    while queue:
        url = queue.popleft()
        if len(sections) >= args.max:
            limit_hit.append(url)
            continue
        try:
            response = page.goto(url, wait_until="load", timeout=args.timeout * 1000)
        except Exception as exc:
            skipped.append((url, "ошибка загрузки: %s" % str(exc).split("\n")[0]))
            continue
        ctype = (response.headers.get("content-type", "") if response else "").lower()
        if response is None or response.status >= 400:
            skipped.append((url, "HTTP %s" % (response.status if response else "?")))
            continue
        if "html" not in ctype:
            skipped.append((url, "не HTML: %s" % ctype))
            continue
        final = normalize_url(page.url)
        aliases[url] = final
        if final != url and any(s["url"] == final for s in sections):
            continue
        info = page.evaluate(PREPARE_JS)
        path = os.path.join(tmp_dir, "part%04d.pdf" % len(sections))
        print_page(page, path, args)
        sections.append({"url": final, "path": path, "info": info})
        log("  [%d] %s: details %d, ссылок %d" % (len(sections), final, info["details"], len(info["links"])))
        for link in info["links"]:
            target = normalize_url(link["href"])
            if not same_site(target, root):
                continue
            ext = extension(target)
            if ext in IMAGE_EXT:
                images.setdefault(target, link["caption"])
            elif ext not in RESOURCE_EXT and target not in seen:
                seen.add(target)
                queue.append(target)
    page.close()
    return sections, skipped, aliases, images, limit_hit


def render_images_appendix(context, args, tmp_dir, images, root):
    """Картинки, на которые ссылается сайт, идут приложением: клик работает офлайн."""
    if not images:
        return None
    width_pt, height_pt = paper_size_pt(args.paper)
    margin_pt = args.margin * PT_PER_MM
    px_per_pt = 1 / (PT_PER_PX * args.scale)
    max_w = int((width_pt - 2 * margin_pt) * px_per_pt) - 2
    max_h = int((height_pt - 2 * margin_pt) * px_per_pt) - 70
    blocks = []
    for n, (url, caption) in enumerate(images.items()):
        text = caption or posixpath.basename(urlsplit(url).path)
        blocks.append(
            '<section style="break-after:page;break-inside:avoid"><h3 id="w2p-img-%d" style="margin:0 0 8px;font-size:16px">%s</h3>'
            '<img src="%s" style="display:block;max-width:%dpx;max-height:%dpx"></section>'
            % (n, escape_html(text), quote(url, safe=":/?&=%#"), max_w, max_h)
        )
    html = ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Изображения</title></head>'
            '<body style="margin:0;background:#fff;color:#22201c;font:16px/1.4 -apple-system,Arial,sans-serif">'
            + "".join(blocks) + "</body></html>")
    page = context.new_page()
    page.emulate_media(media="screen", color_scheme="light")
    # Адрес сайта нужен как база страницы, чтобы картинки грузились с него же
    page.route(root, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
    page.goto(root, wait_until="load", timeout=args.timeout * 1000)
    info = page.evaluate(PREPARE_JS)
    path = os.path.join(tmp_dir, "images.pdf")
    page.pdf(path=path, **pdf_options(args))
    page.close()
    return {"path": path, "info": info, "ids": {u: "w2p-img-%d" % n for n, u in enumerate(images)}}


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Склейка и ссылки ----------

def measure_transform(reader, margin_pt, scale):
    """Грабли №2: координаты якорей Chrome не совпадают с настоящими на листе.

    Он пишет их так, будто scale = 1; учёт верхнего поля зависит от версии (Chrome 156
    поля не добавляет), а страницу шире листа Chrome ещё и ужимает сам. Поэтому поправка
    измеряется для каждой страницы: y_верно от верха = shift + y_из_PDF от верха * factor.
    """
    try:
        first = reader.pages[0]
        height = float(first.mediabox.height)
        dests = {clean_dest_name(k): v for k, v in reader.named_destinations.items()}
        real = {}
        for ref in first.get("/Annots") or []:
            annot = ref.get_object()
            action = annot.get("/A")
            name = annot.get("/Dest") or (action.get_object().get("/D") if action is not None else None)
            if name is not None and clean_dest_name(name) in PROBE_IDS:
                real[clean_dest_name(name)] = height - max(float(annot["/Rect"][1]), float(annot["/Rect"][3]))
        raw_a = height - float(dests[PROBE_IDS[0]].top)
        raw_b = height - float(dests[PROBE_IDS[1]].top)
        factor = (real[PROBE_IDS[1]] - real[PROBE_IDS[0]]) / (raw_b - raw_a)
        shift = real[PROBE_IDS[0]] - raw_a * factor
        if factor <= 0:
            raise ValueError(factor)
    except (KeyError, ZeroDivisionError, TypeError, ValueError, IndexError) as exc:
        # Значения для Chrome 156: пробы могут не сработать, если сайт сам переопределяет вёрстку
        log("  измерить координаты не удалось (%r), беру значения по умолчанию" % (exc,))
        return {"shift": margin_pt, "factor": scale}
    return {"shift": shift, "factor": factor}


def read_anchors(reader, start, transform):
    """Именованные назначения раздела -> {id: (номер листа в итоговом PDF, y)}."""
    anchors = {}
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
        anchors[clean_dest_name(name)] = (start + index, min(height, y + JUMP_HEADROOM_PT))
    return anchors


def build_pdf(sections, appendix, args, aliases):
    """Склеивает разделы, строит закладки и заново создаёт все ссылки."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, DictionaryObject, Fit, FloatObject, NameObject,
                               NullObject, NumberObject, TextStringObject)

    margin_pt = args.margin * PT_PER_MM
    writer = PdfWriter()
    parts = list(sections)
    if appendix:
        parts.append({"url": None, "path": appendix["path"], "info": appendix["info"]})
    for part in parts:
        part["reader"] = PdfReader(part["path"])
        part["start"] = len(writer.pages)
        part["transform"] = measure_transform(part["reader"], margin_pt, args.scale)
        if part["url"] and abs(part["transform"]["factor"] - args.scale) > 0.005:
            log("  %s шире листа, Chrome ужал её: масштаб %.3f вместо %g" % (part["url"], part["transform"]["factor"], args.scale))
        part["anchors"] = read_anchors(part["reader"], part["start"], part["transform"])
        # Аннотации создаём сами ниже: так ни одна ссылка не потеряется при склейке (грабли №10)
        writer.append(part["reader"], import_outline=False, excluded_fields=["/B", "/Annots"])

    by_url = {s["url"]: s for s in sections}
    root = normalize_url(args.url)
    stats = {
        "goto_anchor": 0, "goto_section": 0, "goto_same_page": 0, "goto_image": 0, "uri_external": 0,
        "uri_own_kept": 0, "hidden_removed": 0, "other": 0,
    }
    own_kept, title_conflicts = [], 0

    def page_top(index):
        return float(writer.pages[index].mediabox.height)

    def goto(index, y):
        return DictionaryObject({
            NameObject("/S"): NameObject("/GoTo"),
            NameObject("/D"): ArrayObject([
                writer.pages[index].indirect_reference, NameObject("/XYZ"),
                NumberObject(0), FloatObject(round(y, 3)), NullObject(),
            ]),
        })

    def resolve_uri(uri, source):
        """URI -> (action, тип статистики)."""
        if not same_site(uri, root):
            return None, "uri_external"
        target = normalize_url(uri)
        target = aliases.get(target, target)
        if appendix and target in appendix["ids"]:
            a = appendix_part["anchors"].get(appendix["ids"][target])
            if a:
                return goto(a[0], a[1]), "goto_image"
        part = by_url.get(target)
        if part is None:
            own_kept.append((source["url"], uri))
            return None, "uri_own_kept"
        frag = fragment(uri)
        if frag:
            a = part["anchors"].get(frag)
            if a:
                return goto(a[0], a[1]), "goto_anchor"
        return goto(part["start"], page_top(part["start"])), "goto_section"

    appendix_part = parts[-1] if appendix else None
    for part in parts:
        titles = {}
        for link in part["info"]["links"]:
            if link["title"]:
                # Ссылки внутри страницы приходят без адреса, только с именем якоря
                keys = [link["href"]]
                if part["url"] and normalize_url(link["href"]) == part["url"] and fragment(link["href"]):
                    keys.append("#" + fragment(link["href"]))
                for key in keys:
                    if titles.get(key, link["title"]) != link["title"]:
                        title_conflicts += 1
                    titles.setdefault(key, link["title"])
        for offset, src_page in enumerate(part["reader"].pages):
            out_page = writer.pages[part["start"] + offset]
            fresh = []
            for ref in src_page.get("/Annots") or []:
                annot = ref.get_object()
                if annot.get("/Subtype") != "/Link":
                    continue
                rect = [float(v) for v in annot["/Rect"]]
                probe = annot.get("/Dest")
                if probe is None and annot.get("/A") is not None:
                    probe = annot["/A"].get_object().get("/D")
                if probe is not None and clean_dest_name(probe) in PROBE_IDS:
                    continue
                if abs(rect[2] - rect[0]) < TINY_LINK_PT or abs(rect[3] - rect[1]) < TINY_LINK_PT:
                    stats["hidden_removed"] += 1
                    continue
                action_src = annot.get("/A")
                action_src = action_src.get_object() if action_src is not None else None
                uri = None
                name = annot.get("/Dest")
                if action_src is not None and action_src.get("/S") == "/URI":
                    uri = strip_missing_mark(str(action_src["/URI"]))
                elif action_src is not None and action_src.get("/S") == "/GoTo":
                    name = action_src.get("/D")
                action, kind = None, "other"
                if uri is not None:
                    action, kind = resolve_uri(uri, part)
                    if action is None:
                        action = DictionaryObject({NameObject("/S"): NameObject("/URI"),
                                                   NameObject("/URI"): TextStringObject(uri)})
                elif name is not None and not isinstance(name, ArrayObject):
                    a = part["anchors"].get(clean_dest_name(name))
                    if a:
                        action, kind = goto(a[0], a[1]), "goto_same_page"
                    else:
                        action, kind = goto(part["start"], page_top(part["start"])), "goto_section"
                if action is None:
                    stats["other"] += 1
                    continue
                stats[kind] += 1
                new = DictionaryObject({
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Link"),
                    NameObject("/Rect"): ArrayObject([FloatObject(round(v, 3)) for v in rect]),
                    NameObject("/Border"): ArrayObject([NumberObject(0)] * 3),
                    NameObject("/A"): action,
                })
                title = titles.get(uri) if uri else titles.get("#" + clean_dest_name(name))
                if title:
                    new[NameObject("/Contents")] = TextStringObject(title)
                fresh.append(writer._add_object(new))
            if fresh:
                out_page[NameObject("/Annots")] = ArrayObject(fresh)
            elif "/Annots" in out_page:
                del out_page["/Annots"]

    add_outline(writer, parts, appendix_part, Fit)

    # Грабли №3: ничего именованного не остаётся, id разных разделов не конфликтуют
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
    writer.add_metadata({"/Title": site_title or args.url, "/Creator": "clickable-www2pdf"})
    dedupe_images(writer)
    output = os.path.expanduser(args.output)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    writer.write(output)
    # Логические ссылки считаем по DOM: многострочная ссылка даёт в PDF несколько прямоугольников
    anchored = {"total": 0, "exact": 0, "missing": []}
    for part in sections:
        for link in part["info"]["links"]:
            href = link["href"]
            frag = fragment(href)
            target = aliases.get(normalize_url(href), normalize_url(href))
            if frag and same_site(href, root) and target in by_url:
                anchored["total"] += 1
                if frag in by_url[target]["anchors"]:
                    anchored["exact"] += 1
                else:
                    anchored["missing"].append((part["url"], unquote(href)))
    return {"pages": len(writer.pages), "links": stats, "anchored": anchored, "own_kept": own_kept,
            "title_conflicts": title_conflicts, "output": output,
            "transforms": {p["url"] or "images": p["transform"] for p in parts}}


def dedupe_images(writer):
    """Одна картинка в разделе и в приложении хранится один раз.

    Штатная дедупликация pypdf не срабатывает: у копий из разных временных PDF
    разные ссылки на цветовой профиль, хотя данные JPEG совпадают побайтно.
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
    except TypeError:  # старые версии pypdf
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)


def add_outline(writer, parts, appendix_part, Fit):
    for part in parts:
        is_appendix = part is appendix_part
        title = "Изображения" if is_appendix else (part["info"]["title"] or part["url"])
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


# ---------- Точка входа ----------

def parse_args(argv):
    p = argparse.ArgumentParser(description="Статичный сайт -> один PDF с переходами внутри файла.")
    p.add_argument("url", help="адрес главной страницы")
    p.add_argument("-o", "--output", required=True, help="куда сохранить PDF")
    p.add_argument("--scale", type=float, default=0.75, help="масштаб печати, 0.1-2 (по умолчанию 0.75)")
    p.add_argument("--paper", choices=["A4", "Letter"], default="A4", help="формат листа")
    p.add_argument("--margin", type=float, default=8, help="поля в мм (по умолчанию 8)")
    p.add_argument("--max", type=int, default=500, help="предел числа страниц сайта")
    p.add_argument("--long-pages", action="store_true", help="одна длинная страница PDF на страницу сайта")
    p.add_argument("--no-images", action="store_true", help="не добавлять приложение с картинками")
    p.add_argument("--chrome", help="путь к браузеру на движке Chromium")
    p.add_argument("--timeout", type=int, default=120, help="таймаут загрузки страницы, секунд")
    p.add_argument("--report", help="сохранить отчёт в JSON")
    args = p.parse_args(argv)
    if not 0.1 <= args.scale <= 2:
        p.error("--scale должен быть от 0.1 до 2")
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
            sections, skipped, aliases, images, limit_hit = render_site(context, args, tmp_dir)
            if not sections:
                sys.exit("Не удалось загрузить ни одной страницы: %s" % (skipped,))
            appendix = None
            if images and not args.no_images:
                log("Приложение с картинками: %d" % len(images))
                appendix = render_images_appendix(context, args, tmp_dir, images, normalize_url(args.url))
            browser.close()
        result = build_pdf(sections, appendix, args, aliases)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    links = result["links"]
    print("Готово: %s" % result["output"])
    print("Листов: %d, размер: %.1f МБ" % (result["pages"], os.path.getsize(result["output"]) / 1e6))
    print("Разделы: %s" % ", ".join(s["url"] for s in sections))
    print("Раскрыто details: %d, осталось закрытыми: %d" % (
        sum(s["info"]["details"] for s in sections), sum(s["info"]["closedDetails"] for s in sections)))
    anchored = result["anchored"]
    print("Ссылок с якорем на свой сайт: %d, из них точно на элемент: %d, цели нет на сайте: %d (ведут на начало раздела)"
          % (anchored["total"], anchored["exact"], len(anchored["missing"])))
    print("Прямоугольников ссылок в PDF: на якорь %d, внутри страницы %d, на начало раздела %d, на картинки %d, "
          "внешних %d" % (links["goto_anchor"], links["goto_same_page"], links["goto_section"], links["goto_image"],
                          links["uri_external"]))
    grouped = {}
    for src, href in anchored["missing"]:
        grouped.setdefault(href, []).append(src)
    for href in sorted(grouped):
        print("  нет цели: %s (ссылок: %d)" % (href, len(grouped[href])))
    for src, href in result["own_kept"]:
        print("  остаётся ссылкой в интернет: %s -> %s" % (src, href))
    for url in limit_hit:
        print("  не вошла из-за --max: %s" % url)
    for url, why in skipped:
        print("  пропущено: %s (%s)" % (url, why))
    for s in sections:
        if s["info"]["duplicateIds"]:
            print("  повторяющиеся id на %s: %s (переход идёт на первый)" % (s["url"], ", ".join(s["info"]["duplicateIds"])))
        for src in s["info"]["brokenImages"]:
            print("  картинка не загрузилась: %s" % src)
    if args.report:
        report = dict(result, sections=[s["url"] for s in sections], skipped=skipped, limit_hit=limit_hit,
                      details=sum(s["info"]["details"] for s in sections),
                      closed_details=sum(s["info"]["closedDetails"] for s in sections))
        with open(os.path.expanduser(args.report), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
