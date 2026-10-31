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
JUMP_HEADROOM_PT = 4  # небольшой зазор над целью, чтобы она не прилипала к краю окна
TINY_LINK_PT = 2      # служебные невидимые ссылки меньше этого размера по обеим осям удаляются
MARK_PREFIX = "__w2p_"  # метки в адресах ссылок, которые снимаются при разборе PDF
MAX_REFRESH_SECONDS = 5  # <meta refresh> с такой задержкой считаем редиректом
DEFAULT_PORTS = {"http": 80, "https": 443}
APPENDIX_TITLE = "Изображения"
PROBE_IDS = ("__w2p_probe_a", "__w2p_probe_b")  # пробные якоря для измерения координат
MAX_PDF_SIDE_PT = 14400  # предел размера страницы в PDF

# Подготовка страницы к печати. Возвращает данные для закладок и проверок.
PREPARE_JS = r"""
async (imageWaitMs) => {
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

  // Прокручиваемые блоки на бумаге обрезаются, поэтому показываем их содержимое целиком.
  // Многоточие и обрезку только по ширине у overflow:hidden оставляем: это часть оформления.
  for (const el of document.querySelectorAll('html, body, body *')) {
    const cs = getComputedStyle(el);
    const values = [cs.overflowX, cs.overflowY];
    if (values.every(v => v === 'visible') || cs.textOverflow === 'ellipsis') continue;
    const tall = el.scrollHeight > el.clientHeight + 1, wide = el.scrollWidth > el.clientWidth + 1;
    const scrollable = values.some(v => v === 'auto' || v === 'scroll');
    if (tall || (wide && scrollable)) {
      el.style.setProperty('overflow', 'visible', 'important');
      el.style.setProperty('height', 'auto', 'important');
      el.style.setProperty('max-height', 'none', 'important');
    }
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
      setTimeout(res, imageWaitMs);
    });
  }));
  if (document.fonts && document.fonts.ready) await document.fonts.ready;

  // Закладки: заголовки h1-h3 и раскрывашки с вложенностью как в DOM
  // Классические якоря <a name> браузер считает целями наравне с id.
  // Значение читаем через getAttribute: у формы el.id может оказаться полем с именем id.
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
    if (id && !dup.includes(id)) return id;
    const own = '__w2p_' + (++tmpN);
    if (!id) { el.setAttribute('id', own); return own; }
    // Повторяющийся id ведёт на первый элемент, поэтому закладке нужен свой якорь
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

  // Грабли №1: Chrome пишет координаты только для якорей, на которые есть ссылка
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
  // Служебное кладём вне потока body: его отступы и ширина не должны влиять на пробы
  document.documentElement.appendChild(box);

  // Пробные якоря: прямоугольник их ссылки в PDF честный, а координата назначения нет.
  // Сравнивая их, узнаём настоящее преобразование именно для этой страницы.
  const probes = [0, 200].map((top, i) => {
    const a = document.createElement('a');
    const id = ['__w2p_probe_a', '__w2p_probe_b'][i];
    a.setAttribute('id', id);
    a.setAttribute('href', '#' + id);
    a.style.cssText = 'position:absolute;left:0;top:' + top + 'px;display:block;width:16px;height:16px';
    document.documentElement.appendChild(a);
    return a;
  });

  // Ссылки: цели для обхода, подсказки title, подписи к картинкам
  // У ссылки внутри SVG свойство href не строка, поэтому читаем атрибут
  const rawHref = a => a.getAttribute('href') || a.getAttribute('xlink:href');
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
      // Подпись картинки: заголовок ближайшего блока, в котором он есть
      let head = null;
      for (let p = a.parentElement; p && !head; p = p.parentElement) head = p.querySelector('h1,h2,h3,h4,figcaption');
      const headText = head ? clean(head.textContent) : '';
      caption = headText.includes(img.alt) ? headText : clean(img.alt + ' ' + headText);
    }
    links.push({href: url ? url.href : rawHref(a), title: a.getAttribute('title') || '', caption,
                area: a.tagName === 'AREA'});
  }

  // Метки в адресе заставляют Chrome отдать ссылку как URI, а мы их потом снимаем
  const addMark = (a, mark) => {
    const url = parse(a);
    url.search = (url.search ? url.search + '&' : '?') + mark;
    a.setAttribute('href', url.href);
  };
  const sans = u => u.split('#')[0];
  const here = sans(location.href), base = sans(document.baseURI);
  const titlesByHref = new Map();
  links.forEach(l => { if (l.title) titlesByHref.set(l.href, (titlesByHref.get(l.href) || new Set()).add(l.title)); });
  linkEls.forEach((a, i) => {
    const url = parse(a);
    if (!url) return;
    // Один адрес с разными title: метка позволит найти title именно этой ссылки
    if ((titlesByHref.get(links[i].href) || new Set()).size > 1) addMark(a, '__w2p_t=' + i);
    if (!url.hash) return;
    let id = url.hash.slice(1), decoded = id;
    try { decoded = decodeURIComponent(id); } catch (e) {}
    const page = sans(url.href);
    // Chrome сравнивает адрес с <base>: чужую страницу он примет за свою.
    // А ссылку на несуществующий id своей страницы он выбрасывает из PDF.
    if ((page === base && base !== here) || (page === here && !ids.has(id) && !ids.has(decoded))) {
      addMark(a, '__w2p_missing=1');
    }
  });
  return {
    title: document.title || '',
    outline,
    links,
    anchorNames,
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
  const match = /^\s*(\d+(?:\.\d+)?)\s*[;,]\s*(?:url\s*=\s*)?\S/i.exec(meta.getAttribute('content') || '');
  return match ? Number(match[1]) : null;
}
"""

# Для --long-pages: на бумаге vh считается от высоты листа, и страница никогда не поместится
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

# Для приложения: убирает блоки с незагрузившимися картинками и возвращает их номера
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


# ---------- Автоустановка зависимостей ----------

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
    except OSError:  # испорченный файл вместо python: venv придётся пересоздать
        return False


def python_candidates():
    """Сначала текущий python; если на нём пакеты не встанут, пробуем другие."""
    venv_real = os.path.realpath(VENV_DIR) + os.sep
    base = os.path.join(sys.base_prefix, "bin", "python3")
    names = [base, sys.executable, "python3.13", "python3.12", "python3.11", "python3.10",
             "/usr/bin/python3", "python3"]
    seen, result = set(), []
    for name in names:
        path = shutil.which(name)
        # Интерпретатор самого venv не годится: venv будет удалён перед созданием
        if not path or os.path.abspath(path).startswith(venv_real):
            continue
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            result.append(path)
    return result


def clear_venv_dir():
    """Удаляет только настоящий venv: каталог с чужими файлами трогать нельзя."""
    if os.path.lexists(VENV_DIR) and not os.path.isdir(VENV_DIR):
        sys.exit("%s не каталог, venv там создать нельзя. Укажите другой путь в WWW2PDF_VENV." % VENV_DIR)
    if os.path.isdir(VENV_DIR) and os.listdir(VENV_DIR):
        if not os.path.exists(os.path.join(VENV_DIR, "pyvenv.cfg")):
            sys.exit("%s не пуст и не похож на venv, удалять его не буду. Укажите другой путь в WWW2PDF_VENV." % VENV_DIR)
        shutil.rmtree(VENV_DIR, ignore_errors=True)


def ensure_venv():
    """Создаёт venv без sudo и перезапускает скрипт внутри него."""
    if os.path.realpath(sys.prefix) == os.path.realpath(VENV_DIR) and deps_ok(sys.executable):
        return
    if not deps_ok(venv_python()):
        log("Ставлю зависимости в", VENV_DIR, "(нужно один раз)...")
        for base in python_candidates():
            clear_venv_dir()
            made = subprocess.run([base, "-m", "venv", VENV_DIR], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if made.returncode != 0:
                log("  ", base, ": не удалось создать venv:", made.stderr.decode("utf-8", "replace")[-300:])
                continue
            installed = subprocess.run(
                [venv_python(), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"] + REQUIREMENTS,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            if installed.returncode == 0 and deps_ok(venv_python()):
                break
            log("  ", base, ": пакеты не установились:", installed.stderr.decode("utf-8", "replace")[-300:])
        else:
            sys.exit("Не удалось установить зависимости, причины выше. Нужны python3, право записи в %s и доступ к PyPI." % VENV_DIR)
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
    scheme, netloc = parts.scheme.lower(), parts.netloc.lower()
    default = ":%d" % DEFAULT_PORTS.get(scheme, 0)
    if netloc.endswith(default):
        netloc = netloc[:-len(default)]
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def same_site(url, root):
    """Некорректный адрес считается чужим: одна кривая ссылка не должна ронять запуск."""
    try:
        a, b = urlsplit(url), urlsplit(root)
        if a.scheme not in ("http", "https"):
            return False
        return (a.hostname, a.port or DEFAULT_PORTS[a.scheme]) == (b.hostname, b.port or DEFAULT_PORTS.get(b.scheme))
    except ValueError:
        return False


def extension(url):
    return posixpath.splitext(urlsplit(url).path)[1].lower()


def strip_marks(url):
    """Снимает служебные метки. Возвращает (адрес, номер ссылки для поиска title или None)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url, None
    query = "&".join(q for q in parts.query.split("&") if not q.startswith(MARK_PREFIX))
    number = parse_qs(parts.query).get(MARK_PREFIX + "t")
    return (urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment)),
            int(number[0]) if number else None)


def clean_dest_name(name):
    """Имя назначения из PDF -> исходный id. Кириллица приходит как UTF-8, прочитанный как latin-1."""
    name = str(name).lstrip("/")
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def find_anchor(anchors, frag):
    """Как в браузере: сначала точное совпадение id, потом декодированный фрагмент."""
    return anchors.get(frag) or anchors.get(unquote(frag))


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
    if not args.long_pages:
        page.pdf(path=path, **pdf_options(args))
        return
    from pypdf import PdfReader
    width_pt = paper_size_pt(args.paper)[0]
    margin_pt = args.margin * PT_PER_MM
    layout_px = (width_pt - 2 * margin_pt) / (PT_PER_PX * args.scale)
    page.set_viewport_size({"width": int(layout_px), "height": 900})
    page.evaluate(FREEZE_VH_JS)
    doc_px = page.evaluate("document.documentElement.scrollHeight")
    height_pt = doc_px * PT_PER_PX * args.scale + 2 * margin_pt + 2
    if height_pt > MAX_PDF_SIDE_PT:
        log("  страница длиннее предела PDF, она займёт несколько листов")
        height_pt = MAX_PDF_SIDE_PT
    page.pdf(path=path, **pdf_options(args, width_pt, height_pt))
    sheets = len(PdfReader(path).pages)
    if sheets > 1 and height_pt < MAX_PDF_SIDE_PT:
        # Вёрстка на бумаге бывает выше, чем на экране: повторяем с запасом
        height_pt = min(height_pt * sheets, MAX_PDF_SIDE_PT)
        page.pdf(path=path, **pdf_options(args, width_pt, height_pt))
    page.set_viewport_size({"width": 1280, "height": 900})


def open_url(page, candidates, timeout_ms):
    """Открывает первый рабочий адрес. Возвращает (ответ, дождались ли load, причина неудачи)."""
    response, why = None, "нет адреса"
    for url in candidates:
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            response, why = None, "ошибка загрузки: %s" % str(exc).split("\n")[0]
            continue
        if response is not None and response.status < 400:
            return response, wait_load(page, timeout_ms), None
        why = "HTTP %s" % (response.status if response else "?")
    return None, False, why


def wait_load(page, timeout_ms):
    """Зависшая картинка не должна выбрасывать страницу: печатаем то, что успело загрузиться."""
    try:
        page.wait_for_load_state("load", timeout=timeout_ms)
        return True
    except Exception:
        return False


def follow_client_redirects(page, timeout_ms):
    """Дожидается редиректов через <meta refresh> с небольшой задержкой."""
    for _ in range(5):
        try:
            delay = page.evaluate(META_REFRESH_JS)
        except Exception:  # страница как раз уходит на новый адрес
            wait_load(page, timeout_ms)
            continue
        if delay is None or delay > MAX_REFRESH_SECONDS:
            return
        current = page.url
        try:
            page.wait_for_url(lambda u: u != current, timeout=(delay + 3) * 1000, wait_until="domcontentloaded")
        except Exception:  # обновляет сама себя или адрес неверный
            return
        wait_load(page, timeout_ms)


def prepare_and_print(page, path, args, timeout_ms, loaded):
    """JS-редирект может сработать в любой момент, поэтому при сбое повторяем на новой странице."""
    image_wait = min(timeout_ms, 60000) if loaded else 1000
    last_error = None
    for _ in range(3):
        try:
            info = page.evaluate(PREPARE_JS, image_wait)
            print_page(page, path, args)
            return info, None
        except Exception as exc:
            last_error = str(exc).split("\n")[0]
            wait_load(page, timeout_ms)
    return None, last_error


def render_site(context, args, tmp_dir):
    """Обход в ширину. Каждая страница сайта даёт один временный PDF."""
    root = normalize_url(args.url)
    queue = deque([(root, args.url.split("#")[0])])
    seen = {root}
    sections, skipped, aliases, alias_fragments = [], [], {}, {}
    images, slow, areas = {}, [], []
    limit_hit = []
    timeout_ms = args.timeout * 1000

    def visit(page, url, raw):
        # Сервер может отдавать только /dir/index.html или только /dir/
        attempts = [raw] + [u for u in (url, url + "index.html" if url.endswith("/") else None) if u and u != raw]
        response, loaded, why = open_url(page, attempts, timeout_ms)
        if response is None:
            skipped.append((url, why))
            return
        ctype = response.headers.get("content-type", "").lower()
        if "html" not in ctype:
            skipped.append((url, "не HTML: %s" % ctype))
            return
        follow_client_redirects(page, timeout_ms)
        final = normalize_url(page.url)
        if not same_site(final, root):
            skipped.append((url, "редирект на другой сайт: %s" % page.url))
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
            skipped.append((final, "не удалось напечатать: %s" % error))
            return
        sections.append({"url": final, "path": path, "info": info})
        log("  [%d] %s: details %d, ссылок %d" % (len(sections), final, info["details"], len(info["links"])))
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
        # Своя вкладка на каждую страницу: сбой навигации не должен ломать следующие
        page = context.new_page()
        try:
            page.emulate_media(media="screen", color_scheme="light")
            visit(page, url, raw)
        finally:
            page.close()
    return {"sections": sections, "skipped": skipped, "aliases": aliases, "alias_fragments": alias_fragments,
            "images": images, "limit_hit": limit_hit, "slow": slow, "areas": areas}


def render_images_appendix(context, args, tmp_dir, images, root):
    """Картинки, на которые ссылается сайт, идут приложением: клик работает офлайн."""
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
    # Разрыв перед каждым блоком, кроме первого: так в конце не появится пустой лист
    html = ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Изображения</title>'
            '<style>section+section{break-before:page}section{break-inside:avoid}</style></head>'
            '<body style="margin:0;background:#fff;color:#22201c;font:16px/1.4 -apple-system,Arial,sans-serif">'
            + "".join(blocks) + "</body></html>")
    ids = {u: "w2p-img-%d" % n for n, u in enumerate(images)}
    page = context.new_page()
    page.emulate_media(media="screen", color_scheme="light")
    timeout_ms = args.timeout * 1000
    try:
        # Адрес сайта нужен как база страницы, чтобы картинки грузились с него же
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
        # Сбой приложения не должен терять готовые разделы: ссылки останутся URI
        log("Приложение с картинками не собралось: %s" % str(exc).split("\n")[0])
        return {"path": None, "broken": list(ids)}
    finally:
        page.close()
    return {"path": path, "info": info, "ids": ids, "broken": broken}


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
        dests = {clean_dest_name(k): v for k, v in reader.named_destinations.items()}
        real, raw = {}, {}
        for page in reader.pages:
            height = float(page.mediabox.height)
            for ref in page.get("/Annots") or []:
                annot = ref.get_object()
                action = annot.get("/A")
                name = annot.get("/Dest") or (action.get_object().get("/D") if action is not None else None)
                if isinstance(name, str) and clean_dest_name(name) in PROBE_IDS:
                    key = clean_dest_name(name)
                    real[key] = height - max(float(annot["/Rect"][1]), float(annot["/Rect"][3]))
                    raw[key] = height - float(dests[key].top)
        factor = (real[PROBE_IDS[1]] - real[PROBE_IDS[0]]) / (raw[PROBE_IDS[1]] - raw[PROBE_IDS[0]])
        shift = real[PROBE_IDS[0]] - raw[PROBE_IDS[0]] * factor
        if factor <= 0:
            raise ValueError(factor)
    except (KeyError, ZeroDivisionError, TypeError, ValueError, IndexError) as exc:
        # Значения для Chrome 156: пробы могут не сработать, если сайт сам переопределяет вёрстку
        log("  измерить координаты не удалось (%r), переходы могут быть неточными" % (exc,))
        return {"shift": margin_pt, "factor": scale, "measured": False}
    return {"shift": shift, "factor": factor, "measured": True}


def read_anchors(reader, start, transform, anchor_names):
    """Назначения раздела -> (по id, по имени в PDF): значения (номер листа, y).

    Chrome называет назначение сырым фрагментом ссылки. Служебная ссылка на каждый id
    закодирована известным способом, поэтому id и имя сопоставляются точно: на странице
    могут быть и id="a b", и id="a%20b".
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
    """Склеивает разделы, строит закладки и заново создаёт все ссылки."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (ArrayObject, DictionaryObject, Fit, FloatObject, NameObject,
                               NullObject, NumberObject, TextStringObject)

    sections, aliases, alias_fragments = crawl["sections"], crawl["aliases"], crawl["alias_fragments"]
    margin_pt = args.margin * PT_PER_MM
    writer = PdfWriter()
    parts = list(sections)
    if appendix:
        parts.append({"url": None, "path": appendix["path"], "info": appendix["info"]})
    clipped = []
    for part in parts:
        part["reader"] = PdfReader(part["path"])
        part["start"] = len(writer.pages)
        part["transform"] = measure_transform(part["reader"], margin_pt, args.scale)
        ratio = part["transform"]["factor"] / args.scale
        if part["url"] and ratio < 0.995:
            log("  %s шире листа, Chrome ужал её: масштаб %.3f вместо %g" % (part["url"], part["transform"]["factor"], args.scale))
            # Сильнее чем вдвое Chrome не ужимает: остальное уходит за край листа
            if ratio <= 0.505:
                clipped.append(part["url"])
        part["anchors"], part["names"] = read_anchors(part["reader"], part["start"], part["transform"],
                                                      part["info"]["anchorNames"])
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
                NumberObject(0), FloatObject(int(y * 1000) / 1000), NullObject(),
            ]),
        })

    def resolve_target(uri):
        """Адрес на своём сайте -> (адрес раздела, фрагмент) с учётом редиректов."""
        target = normalize_url(uri)
        frag = urlsplit(uri).fragment or alias_fragments.get(target, "")
        return aliases.get(target, target), frag

    def resolve_uri(uri, source):
        """URI -> (action, тип статистики)."""
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
                return goto(a[0], a[1]), "goto_anchor"
        return goto(part["start"], page_top(part["start"])), "goto_section"

    appendix_part = parts[-1] if appendix else None
    for part in parts:
        titles = {}
        for link in part["info"]["links"]:
            if link["title"]:
                # Ссылки внутри страницы приходят без адреса, только с именем якоря
                keys = [link["href"]]
                if part["url"] and same_site(link["href"], root) and normalize_url(link["href"]) == part["url"]:
                    frag = urlsplit(link["href"]).fragment
                    if frag:
                        keys.append("#" + frag)
                        keys.append("#" + unquote(frag))
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
                if isinstance(probe, str) and clean_dest_name(probe) in PROBE_IDS:
                    continue
                if abs(rect[2] - rect[0]) < TINY_LINK_PT and abs(rect[3] - rect[1]) < TINY_LINK_PT:
                    stats["hidden_removed"] += 1
                    continue
                action_src = annot.get("/A")
                action_src = action_src.get_object() if action_src is not None else None
                uri, link_number = None, None
                name = annot.get("/Dest")
                if action_src is not None and action_src.get("/S") == "/URI":
                    uri, link_number = strip_marks(str(action_src["/URI"]))
                elif action_src is not None and action_src.get("/S") == "/GoTo":
                    name = action_src.get("/D")
                action, kind = None, "other"
                if uri is not None:
                    action, kind = resolve_uri(uri, part)
                    if action is None:
                        action = DictionaryObject({NameObject("/S"): NameObject("/URI"),
                                                   NameObject("/URI"): TextStringObject(uri)})
                elif name is not None and not isinstance(name, ArrayObject):
                    a = part["names"].get(clean_dest_name(name))
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
                if link_number is not None:
                    title = part["info"]["links"][link_number]["title"]
                else:
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
    # Сбой посреди записи не должен портить прежний файл
    temp_output = output + ".tmp"
    writer.write(temp_output)
    os.replace(temp_output, output)
    # Логические ссылки считаем по DOM: многострочная ссылка даёт в PDF несколько прямоугольников
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
            "title_conflicts": title_conflicts, "output": output, "clipped": clipped,
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
    if args.margin < 0 or 2 * args.margin * PT_PER_MM >= min(paper_size_pt(args.paper)):
        p.error("--margin должен быть не меньше 0 и меньше половины листа")
    if args.max < 1:
        p.error("--max должен быть не меньше 1")
    if args.timeout < 1:
        p.error("--timeout должен быть не меньше 1")
    if urlsplit(args.url).scheme not in ("http", "https"):
        p.error("адрес должен начинаться с http:// или https://")
    # Ошибку в пути лучше узнать сразу, а не после всего обхода
    for path in filter(None, (args.output, args.report)):
        path = os.path.expanduser(path)
        folder = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            p.error("не удалось создать каталог %s: %s" % (folder, exc))
        if os.path.isdir(path) or not os.access(folder, os.W_OK):
            p.error("нельзя записать файл: %s" % path)
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
                sys.exit("Не удалось загрузить ни одной страницы: %s" % (crawl["skipped"],))
            appendix, broken_images = None, []
            if crawl["images"] and not args.no_images:
                log("Приложение с картинками: %d" % len(crawl["images"]))
                appendix = render_images_appendix(context, args, tmp_dir, crawl["images"], normalize_url(args.url))
                broken_images = appendix["broken"]
                if not appendix["path"]:
                    appendix = None
            browser.close()
        result = build_pdf(crawl, appendix, args)
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
    if anchored["inexact"]:
        print("  ВНИМАНИЕ: %d переходов могут быть неточными, измерить координаты не удалось" % anchored["inexact"])
    if links["hidden_removed"]:
        print("  служебных ссылок удалено: %d" % links["hidden_removed"])
    grouped = {}
    for src, href in anchored["missing"]:
        grouped.setdefault(href, []).append(src)
    for href in sorted(grouped):
        print("  нет цели: %s (ссылок: %d)" % (href, len(grouped[href])))
    for src, href in result["own_kept"]:
        print("  остаётся ссылкой в интернет: %s -> %s" % (src, href))
    for url in crawl["limit_hit"]:
        print("  не вошла из-за --max: %s" % url)
    for url, why in crawl["skipped"]:
        print("  пропущено: %s (%s)" % (url, why))
    for url in crawl["slow"]:
        print("  загрузилась не полностью, напечатано что успело: %s" % url)
    for url in result["clipped"]:
        print("  ВНИМАНИЕ: %s намного шире листа, часть может быть обрезана; уменьшите --scale" % url)
    for src, href in crawl["areas"]:
        print("  область карты-картинки не кликается в PDF: %s -> %s" % (src, href))
    for url in broken_images:
        print("  картинка из ссылки не загрузилась, ссылка осталась в интернет: %s" % url)
    for s in sections:
        if s["info"]["duplicateIds"]:
            print("  повторяющиеся id на %s: %s (переход идёт на первый)" % (s["url"], ", ".join(s["info"]["duplicateIds"])))
        for src in s["info"]["brokenImages"]:
            print("  картинка не загрузилась: %s" % src)
    if args.report:
        report = dict(result, sections=[s["url"] for s in sections], skipped=crawl["skipped"], limit_hit=crawl["limit_hit"],
                      details=sum(s["info"]["details"] for s in sections),
                      closed_details=sum(s["info"]["closedDetails"] for s in sections))
        with open(os.path.expanduser(args.report), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
