#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Верификатор PDF. Ожидания берёт из самого сайта, а не из www2pdf.py.

Для каждой ссылки в PDF: текст под ней сверяется с видимым текстом ссылки на сайте,
порядок ссылок с одинаковым текстом должен совпадать с сайтом, а ожидаемая запись
(ФИО из summary, код и заголовок источника) должна стоять у самой точки перехода.
Также проверяются форма назначений, весь текст страниц (в том числе скрытый), порядок листов,
закладки поэлементно, подсказки /Contents, пустые и обрезанные листы, выход текста за лист.

Не проверяется: цвет текста (белый на белом пройдёт). Внешний вид смотрят глазами.

Запуск: python verify.py файл.pdf http://сайт/ [--markers markers.txt] [--json отчёт.json]
Код возврата 0 только если нет ни одного расхождения.
"""
import argparse
import hashlib
import json
import posixpath
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from urllib.parse import unquote, urljoin, urlsplit

import pdfplumber
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.generic import ArrayObject, NullObject

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}
TOP_TOLERANCE = 3  # допуск в pt: глиф может начинаться чуть выше точки перехода
LINE_TOLERANCE = 6  # символы ближе этого по вертикали считаем одной строкой
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
MIN_ORDER_NODE = 5  # короче этого узел может случайно оказаться частью другого текста
NON_TEXT_TAGS = {"script", "style", "noscript", "template", "head", "title"}


def norm(text):
    """Сравниваем без пробелов и дефисов: pdfplumber отдаёт дефис из шрифтов Chrome как U+E088."""
    text = unicodedata.normalize("NFKC", text or "")  # лигатуры шрифта: ﬁ -> fi
    return re.sub(r"[\s\-‐-―­]+", "", text)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace"), r.headers.get("content-type", "")


def page_key(url):
    parts = urlsplit(url)
    path = parts.path or "/"
    if posixpath.basename(path).lower() in ("index.html", "index.htm"):
        path = posixpath.dirname(path).rstrip("/") + "/"
    return parts.scheme, parts.netloc, path, parts.query


def destination_form(item):
    """Закладка должна быть вида /XYZ 0 y null."""
    return str(item["/Type"]) == "/XYZ" and float(item.left) == 0 and isinstance(item.zoom, (NullObject, type(None)))


def expected_text(el):
    """Строка, которая должна стоять у точки перехода на этот элемент."""
    summary = el if el.name == "summary" else el.find("summary")
    if summary is not None:
        bold = summary.find("b")
        return norm((bold or summary).get_text())[:40]
    return norm(el.get_text())[:40]


def visible_strings(root):
    """Все текстовые узлы в порядке документа, включая скрытые блоки."""
    result = []
    for node in root.find_all(string=True):
        if type(node).__name__ != "NavigableString":
            continue
        if any(p.name in NON_TEXT_TAGS for p in node.parents):
            continue
        text = norm(str(node))
        if text:
            result.append((text, node))
    return result


def expected_bookmarks(soup):
    """Заголовки h1–h3 и summary в порядке документа, как в оглавлении PDF."""
    titles = []
    for el in soup.find_all(["h1", "h2", "h3", "summary"]):
        if el.name != "summary" and el.find_parent("summary"):
            continue
        title = clean(el.get_text())
        if title:
            titles.append(title)
    return titles


class Site:
    """Обход сайта стандартными средствами, независимо от браузера и от www2pdf.py."""

    def __init__(self, root):
        self.root = root
        self.pages = {}
        self.image_heads = defaultdict(set)
        queue, seen = [root], {page_key(root)}
        while queue:
            url = queue.pop(0)
            try:
                html, ctype = fetch(url)
            except urllib.error.HTTPError:  # битая ссылка на сайте, это не ошибка проверки
                continue
            except OSError as exc:
                raise SystemExit("ОШИБКА: сайт не ответил, проверка невозможна: %s (%s)" % (url, exc))
            if "html" not in ctype:
                continue
            soup = BeautifulSoup(html, "html.parser")
            key = page_key(url)
            ids = {}
            for el in soup.find_all(id=True):
                ids.setdefault(el["id"], el)  # при повторах браузер идёт на первый
            for el in soup.find_all("a", attrs={"name": True}):
                ids.setdefault(el["name"], el)
            links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                links.append({"href": href, "text": norm(a.get_text()), "title": a.get("title"),
                              "own": self.is_own(href)})
                if a.find("img") is not None and self.is_own(href):
                    head = next((h for h in (p.find(["h1", "h2", "h3", "h4", "figcaption"]) for p in a.parents) if h), None)
                    if head is not None:
                        self.image_heads[href].add(norm(head.get_text()))
                if self.is_own(href) and posixpath.splitext(urlsplit(href).path)[1].lower() not in IMAGE_EXT:
                    target = page_key(href)
                    if target not in seen:
                        seen.add(target)
                        queue.append(href.split("#")[0])
            self.pages[key] = {"url": url, "soup": soup, "ids": ids, "links": links,
                               "title": soup.title.get_text().strip() if soup.title else ""}
        if not self.pages:
            raise SystemExit("ОШИБКА: на сайте не найдено ни одной страницы, проверка невозможна: %s" % root)

    def is_own(self, href):
        a, b = urlsplit(href), urlsplit(self.root)
        return a.scheme in ("http", "https") and (a.hostname, a.port) == (b.hostname, b.port)


class Pdf:
    def __init__(self, path):
        self.reader = PdfReader(path)
        self.plumber = pdfplumber.open(path)
        self.page_index = {p.indirect_reference.idnum: i for i, p in enumerate(self.reader.pages)}
        self.chars = {}
        self.shas = {}
        # Допуски рассчитаны на кегль около 9 pt, на крупном масштабе всё пропорционально больше
        sizes = sorted(c["size"] for p in self.plumber.pages[:5] for c in p.chars[:2000]) or [9]
        self.zoom = max(1.0, sizes[len(sizes) // 2] / 9)

    def page_chars(self, index):
        if index not in self.chars:
            self.chars[index] = self.plumber.pages[index].chars
        return self.chars[index]

    def page_images(self, index):
        xobjects = self.reader.pages[index]["/Resources"].get("/XObject")
        images = [v.get_object() for v in (xobjects.get_object().values() if xobjects else [])]
        return [o for o in images if o.get("/Subtype") == "/Image"]

    def file_sha(self, href):
        if href not in self.shas:
            with urllib.request.urlopen(href, timeout=60) as r:
                self.shas[href] = hashlib.sha1(r.read()).hexdigest()
        return self.shas[href]

    def page_has_image(self, index, href):
        images = self.page_images(index)
        if not images:
            return False
        if not href.lower().endswith((".jpg", ".jpeg")):
            return True
        return any(hashlib.sha1(o._data).hexdigest() == self.file_sha(href) for o in images)

    def height(self, index):
        return float(self.reader.pages[index].mediabox.height)

    def first_line(self, index):
        """Первая строка листа: на первом листе раздела это обычно шапка сайта."""
        chars = [c for c in self.page_chars(index) if norm(c["text"])]
        if not chars:
            return ""
        top = min(c["top"] for c in chars)
        return norm("".join(c["text"] for c in chars if c["top"] - top <= LINE_TOLERANCE * self.zoom))

    def landing(self, index, y, want, window, tight, heading=False, header=""):
        """Проверяет, что want стоит сразу у точки перехода. Возвращает (разрыв в pt, причина ошибки)."""
        top = self.height(index) - y
        window, tight = window * self.zoom, tight * self.zoom
        chars = [c for c in self.page_chars(index) if top - TOP_TOLERANCE <= c["top"] <= top + window]
        pieces = [norm(c["text"]) for c in chars]
        joined = "".join(pieces)
        at = joined.find(want)
        if at < 0:
            return None, "нет ожидаемого текста %r" % want
        position, start = 0, None
        for char, piece in zip(chars, pieces):
            if position >= at and piece:
                start = char
                break
            position += len(piece)
        gap = max(0, start["top"] - top)
        if gap > tight:
            return gap, "запись на %.1f pt ниже точки перехода" % gap
        first_top = min(c["top"] for c, piece in zip(chars, pieces) if piece)
        if start["top"] - first_top > LINE_TOLERANCE * self.zoom:
            return gap, "над записью стоит другой текст"
        if heading:
            line = norm("".join(c["text"] for c in chars if abs(c["top"] - start["top"]) <= LINE_TOLERANCE * self.zoom))
            if header and line == header and line != want:
                return gap, "переход встал на шапку сайта, а не на заголовок"
            if joined.count(want) > 1:
                return gap, "такой же текст ниже: переход встал не на заголовок"
        return gap, None

    def text_under(self, index, rect):
        """Слово считаем «под ссылкой» по центру символа: запятая вылезает за прямоугольник."""
        height = self.height(index)
        x0, y0, x1, y1 = rect
        top, bottom = height - max(y0, y1), height - min(y0, y1)
        x0, x1 = min(x0, x1), max(x0, x1)
        return norm("".join(
            c["text"] for c in self.page_chars(index)
            if x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom))

    def annotations(self, index):
        result = []
        for ref in self.reader.pages[index].get("/Annots") or []:
            a = ref.get_object()
            if a.get("/Subtype") == "/Link":
                result.append(a)
        return result

    def explicit_target(self, dest):
        """Только [page_ref /XYZ 0 y null] с y в пределах листа -> (номер листа, y)."""
        if not isinstance(dest, ArrayObject) or len(dest) != 5:
            return None
        idnum = getattr(list.__getitem__(dest, 0), "idnum", None)
        if idnum not in self.page_index or str(dest[1]) != "/XYZ":
            return None
        if float(dest[2]) != 0 or not isinstance(dest[4], NullObject):
            return None
        index, y = self.page_index[idnum], float(dest[3])
        if not 0 <= y <= self.height(index):
            return None
        return index, y


def walk_outline(items, out):
    for item in items:
        if isinstance(item, list):
            walk_outline(item, out)
        else:
            out.append(item)


def top_level_sections(pdf):
    """Разделы PDF по закладкам верхнего уровня: (заголовок, первый лист, вложенные закладки)."""
    sections = []
    items = pdf.reader.outline
    for i, item in enumerate(items):
        if isinstance(item, list):
            continue
        children = []
        if i + 1 < len(items) and isinstance(items[i + 1], list):
            walk_outline(items[i + 1], children)
        sections.append((str(item.title), pdf.reader.get_destination_page_number(item), item, children))
    return sections


def collect_pdf_links(pdf, first, last):
    """Прямоугольники ссылок раздела в порядке документа."""
    links = []
    for index in range(first, last):
        for a in pdf.annotations(index):
            action = a["/A"].get_object() if "/A" in a else None
            rect = [float(v) for v in a["/Rect"]]
            entry = {"page": index, "rect": rect, "text": pdf.text_under(index, rect),
                     "contents": str(a["/Contents"]) if "/Contents" in a else None}
            if "/Dest" in a:
                entry.update(kind="dest", raw=str(a["/Dest"]))
            elif action is None:
                entry.update(kind="noaction")
            elif action.get("/S") == "/URI":
                entry.update(kind="uri", uri=str(action["/URI"]))
            elif action.get("/S") == "/GoTo":
                entry.update(kind="goto", target=pdf.explicit_target(action["/D"]), raw=str(action["/D"]))
            else:
                entry.update(kind="other")
            links.append(entry)
    return links


def check_link(pdf, site, link, cand, args, section_by_title, counts, missing_list, gaps):
    """Проверяет, что ссылка PDF ведёт туда же, куда её пара на сайте. Возвращает причину ошибки."""
    href = cand["href"]
    if not cand["own"]:
        if link["kind"] == "uri" and unquote(link["uri"]) == unquote(href):
            counts["external_uri"] += 1
            return None
        return "внешняя ссылка изменилась"
    if link["kind"] != "goto" or not link.get("target"):
        return "не GoTo вида [лист /XYZ 0 y null]"
    index, y = link["target"]
    if posixpath.splitext(urlsplit(href).path)[1].lower() in IMAGE_EXT:
        # Перешли на лист приложения, где лежит именно этот файл (JPEG хранится побайтно)
        if link["text"] != "" or not pdf.page_has_image(index, href):
            return "на листе нет этой картинки"
        if pdf.height(index) - y > args.window * pdf.zoom:
            return "переход не к верху листа с картинкой"
        counts["image_goto"] += 1
        return None
    target_key = page_key(href)
    if target_key not in section_by_title:
        return "нет раздела для цели"
    first, last = section_by_title[target_key]
    frag = urlsplit(href).fragment
    target_page = site.pages[target_key]
    element = target_page["ids"].get(frag) or target_page["ids"].get(unquote(frag))
    if frag and element is not None:
        want = expected_text(element)
        if not first <= index < last:
            return "цель в чужом разделе"
        gap, why = pdf.landing(index, y, want, args.window, args.tight, element.name in HEADINGS, pdf.first_line(first))
        if why:
            return why
        gaps.append(gap)
        counts["anchor_exact"] += 1
        return None
    if index != first or not pdf.height(index) - 1 <= y <= pdf.height(index):
        return "ожидалось начало раздела"
    if frag:
        counts["anchor_missing_to_section"] += 1
        missing_list.append("%s#%s" % (urlsplit(href).path.lstrip("/"), frag))
    else:
        counts["section_start"] += 1
    return None


def check_links(pdf, site, args, section_by_title, report, problems):
    counts = Counter()
    misses, unexplained, wrong_contents, missing_list, gaps = [], [], [], [], []
    tiny = 0
    for key, sp in site.pages.items():
        first, last = section_by_title[key]
        pdf_links = collect_pdf_links(pdf, first, last)
        pending = defaultdict(list)
        for link in sp["links"]:
            pending[link["text"]].append(link)
        report["site_links_total"] = report.get("site_links_total", 0) + len(sp["links"])
        i = 0
        while i < len(pdf_links):
            link = pdf_links[i]
            rect = link["rect"]
            if abs(rect[2] - rect[0]) < 2 and abs(rect[3] - rect[1]) < 2:
                tiny += 1
            text = link["text"]
            used = 1
            # Ссылка на несколько строк даёт несколько прямоугольников с одной целью
            while not pending.get(text) and i + used < len(pdf_links):
                nxt = pdf_links[i + used]
                if nxt.get("raw") != link.get("raw") or nxt.get("uri") != link.get("uri"):
                    break
                text += nxt["text"]
                used += 1
            if not pending.get(text):
                unexplained.append((sp["url"], link["page"] + 1, link["text"], link.get("raw") or link.get("uri")))
                i += 1
                continue
            # Пара строго первая по порядку: ссылки с одинаковым текстом не могут поменяться целями
            cand = pending[text].pop(0)
            why = check_link(pdf, site, link, cand, args, section_by_title, counts, missing_list, gaps)
            if why:
                misses.append({"from": sp["url"], "page": link["page"] + 1, "text": text, "href": cand["href"],
                               "why": why, "goto": str(link.get("target"))})
            rects = pdf_links[i:i + used]
            i += used
            # Ссылка с картинкой внутри текста даёт ещё и пустой прямоугольник с той же целью
            while i < len(pdf_links) and pdf_links[i]["text"] == "" and pdf_links[i].get("raw") == link.get("raw"):
                rects.append(pdf_links[i])
                i += 1
            if any((cand.get("title") or None) != (r.get("contents") or None) for r in rects):
                wrong_contents.append((sp["url"], link["page"] + 1, text, cand.get("title"), [r.get("contents") for r in rects]))
            elif cand.get("title"):
                counts["title_in_contents"] += 1
        for lst in pending.values():
            for lost in lst:
                problems.append("ссылка с сайта не найдена в PDF: %s -> %s (%r)" % (sp["url"], lost["href"], lost["text"]))
    report["links"] = dict(counts)
    report["missing_anchors_on_site"] = sorted(set(missing_list))
    report["missing_anchor_links"] = len(missing_list)
    report["misses"] = len(misses)
    report["max_gap_to_target_pt"] = round(max(gaps), 1) if gaps else None
    if misses:
        problems.append("переходы мимо цели: %d, примеры: %s" % (len(misses), json.dumps(misses[:8], ensure_ascii=False)))
    if unexplained:
        problems.append("ссылки PDF без пары на сайте: %d, примеры: %s" % (len(unexplained), unexplained[:8]))
    if wrong_contents:
        problems.append("подсказка /Contents не совпадает с title: %d, примеры: %s" % (len(wrong_contents), wrong_contents[:5]))
    if tiny:
        problems.append("остались служебные крошечные ссылки: %d" % tiny)


class SectionText:
    """Текст раздела одной строкой и для каждого символа его лист и положение по x."""

    def __init__(self, pdf, first, last):
        pieces, self.sheets, self.xs = [], [], []
        for i in range(first, last):
            for c in pdf.page_chars(i):
                piece = norm(c["text"])
                pieces.append(piece)
                self.sheets.extend([i] * len(piece))
                self.xs.extend([c["x0"]] * len(piece))
        self.text = "".join(pieces)


def consume(stream, text):
    """Убирает из потока одно вхождение text. Возвращает новый поток или None.

    Строка таблицы на два листа разорвана в потоке текстом соседней ячейки, поэтому
    допускаем до четырёх кусков длиной от 8 символов.
    """
    if text in stream:
        return stream.replace(text, "\0", 1)
    rest, pieces = text, 0
    while rest:
        size = len(rest)
        while size >= 8 and rest[:size] not in stream:
            size -= 1
        if size < 8 or pieces == 4:
            return None
        stream = stream.replace(rest[:size], "\0", 1)
        rest = rest[size:]
        pieces += 1
    return stream


def check_text(pdf, site, args, section_by_title, report, problems):
    """Весь текст страниц, включая скрытый, должен быть в своём разделе PDF столько же раз, сколько на сайте.

    Каждый узел забирает своё вхождение из потока, начиная с длинных: так короткий узел не
    засчитается за счёт длинного, в который он входит. Порядок здесь не сравнивается:
    колонки Chrome печатает лист за листом вперемешку. Узлы короче 8 символов не проверяются.
    """
    total = lost_total = 0
    details_total = details_ok = 0
    examples, order_errors = [], []
    all_text = []
    for key, sp in site.pages.items():
        first, last = section_by_title[key]
        section = SectionText(pdf, first, last)
        stream = section.text
        all_text.append(stream)
        body = sp["soup"].body or sp["soup"]
        strings = visible_strings(body)
        total += len(strings)
        lost_nodes = set()
        for text, node in sorted(strings, key=lambda item: -len(item[0])):
            if len(text) < 8:
                continue
            updated = consume(stream, text)
            if updated is None:
                lost_total += 1
                lost_nodes.add(id(node))
                if len(examples) < 5:
                    examples.append((sp["url"], text[:80]))
            else:
                stream = updated
        for d in sp["soup"].find_all("details"):
            details_total += 1
            details_ok += not any(id(n) in lost_nodes for _, n in visible_strings(d))
        # Порядок листов: узлы левой колонки и полной ширины не могут идти назад по листам.
        # Правую колонку не смотрим: она начинается снова с первого листа сетки.
        half = float(pdf.plumber.pages[first].width) / 2
        counts = Counter(text for text, _ in strings)
        latest = first
        for text, _ in strings:
            if len(text) < MIN_ORDER_NODE or counts[text] > 1 or section.text.count(text) != 1:
                continue
            at = section.text.find(text)
            if section.xs[at] >= half:
                continue
            if section.sheets[at] < latest:
                order_errors.append((sp["url"], section.sheets[at] + 1, text[:40]))
            latest = max(latest, section.sheets[at])
    report["order_errors"] = len(order_errors)
    if order_errors:
        problems.append("листы не по порядку: %d, примеры: %s" % (len(order_errors), order_errors[:5]))
    report["text_nodes_site"] = total
    report["text_nodes_lost"] = lost_total
    report["details_site"] = details_total
    report["details_in_pdf"] = details_ok
    if lost_total:
        problems.append("текст потерян: %d, примеры: %s" % (lost_total, examples))
    if args.markers:
        joined = "".join(all_text)
        markers = [m.strip() for m in open(args.markers, encoding="utf-8") if m.strip()]
        missing = [m for m in markers if norm(m) not in joined]
        report["markers"] = len(markers)
        report["markers_found"] = len(markers) - len(missing)
        if missing:
            problems.append("не найдены маркеры: %s" % missing[:10])


def check_bookmarks(pdf, site, sections, args, section_by_title, report, problems):
    bad, total = [], 0
    for key, sp in site.pages.items():
        first, _ = section_by_title[key]
        entry = next(s for s in sections if s[0] == sp["title"] and s[1] == first)
        section_item, children = entry[2], entry[3]
        total += 1 + len(children)
        check_section_bookmark(pdf, section_item, first, bad)
        want_titles = expected_bookmarks(sp["soup"])
        got_titles = [clean(str(c.title)) for c in children]
        if [norm(t) for t in want_titles] != [norm(t) for t in got_titles]:
            bad.append((sp["title"], "список закладок не совпадает с сайтом: %d и %d" % (len(got_titles), len(want_titles))))
            continue
        previous = None
        headings = sp["soup"].find_all(["h1", "h2", "h3"])
        heading_titles = {norm(h.get_text()) for h in headings}
        for item in children:
            index = pdf.reader.get_destination_page_number(item)
            y = float(item.top)
            # Закладки идут по документу: так видно перепутанные и выпавшие листы
            if previous and (index, -y) < (previous[0], -previous[1] - 1):
                bad.append((str(item.title)[:50], "закладка выше предыдущей: лист %d" % (index + 1)))
            previous = (index, y)
            if not destination_form(item):
                bad.append((str(item.title)[:50], "форма назначения"))
                continue
            title = norm(str(item.title))
            _, why = pdf.landing(index, y, title[:25], args.window, args.tight, title in heading_titles, pdf.first_line(first))
            if why:
                bad.append((str(item.title)[:50], "лист %d: %s" % (index + 1, why)))
    site_titles = {sp["title"] for sp in site.pages.values()}
    for title, first, item, children in sections:
        if title not in site_titles:
            total += 1 + len(children)
            check_section_bookmark(pdf, item, first, bad)
            check_image_bookmarks(pdf, site, children, args, bad)
    report["bookmarks_total"] = total
    report["bookmarks_bad"] = len(bad)
    if bad:
        problems.append("закладки с ошибкой: %d, примеры: %s" % (len(bad), bad[:8]))


def check_section_bookmark(pdf, item, first, bad):
    if not destination_form(item):
        bad.append((str(item.title), "форма назначения у закладки раздела"))
    if not pdf.height(first) - 1 <= float(item.top) <= pdf.height(first):
        bad.append((str(item.title), "закладка раздела не у верха листа"))


def check_image_bookmarks(pdf, site, children, args, bad):
    """Закладка картинки стоит у верха своего листа, а подпись называет запись, где эта картинка на сайте."""
    by_sha = {pdf.file_sha(h): h for h in site.image_heads if h.lower().endswith((".jpg", ".jpeg"))}
    previous = -1
    for item in children:
        index = pdf.reader.get_destination_page_number(item)
        if not destination_form(item):
            bad.append((str(item.title)[:50], "форма назначения"))
            continue
        if index <= previous:
            bad.append((str(item.title)[:50], "картинки не по порядку"))
        previous = index
        _, why = pdf.landing(index, float(item.top), norm(str(item.title))[:25], args.window, args.tight)
        if why:
            bad.append((str(item.title)[:50], why))
        for image in pdf.page_images(index):
            href = by_sha.get(hashlib.sha1(image._data).hexdigest())
            if href and not all(h in norm(str(item.title)) for h in site.image_heads[href]):
                bad.append((str(item.title)[:50], "подпись не от этой картинки: %s" % href))


def check_sheets(pdf, report, problems):
    """Пустых листов нет, текст не выходит за лист и не сжат до нечитаемого."""
    blank, cropped, outside, microscopic = [], [], 0, 0
    for index, page in enumerate(pdf.plumber.pages):
        chars = pdf.page_chars(index)
        if not chars and not pdf.page_images(index):
            blank.append(index + 1)
        # Просмотрщик показывает CropBox: лист не должен быть обрезан или повёрнут
        source = pdf.reader.pages[index]
        if list(map(float, source.cropbox)) != list(map(float, source.mediabox)) or source.get("/Rotate", 0):
            cropped.append(index + 1)
        for c in chars:
            if c["x0"] < -1 or c["x1"] > float(page.width) + 1 or c["top"] < -1 or c["bottom"] > float(page.height) + 1:
                outside += 1
            if c["size"] < 1:
                microscopic += 1
    report["sheets"] = len(pdf.plumber.pages)
    report["blank_sheets"] = blank
    if blank:
        problems.append("пустые листы: %s" % blank[:10])
    if cropped:
        problems.append("обрезанные или повёрнутые листы: %s" % cropped[:10])
    if outside:
        problems.append("символов за пределами листа: %d" % outside)
    if microscopic:
        problems.append("символов с кеглем меньше 1 pt: %d" % microscopic)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("site")
    ap.add_argument("--markers")
    ap.add_argument("--window", type=float, default=45, help="где искать запись ниже точки перехода, pt")
    ap.add_argument("--tight", type=float, default=20, help="как далеко может начинаться запись, pt")
    ap.add_argument("--json")
    args = ap.parse_args()

    site = Site(args.site)  # если сайт не ответил, проверка завершится с ошибкой
    pdf = Pdf(args.pdf)
    problems = []
    report = {"site_pages": len(site.pages)}
    total_pages = len(pdf.reader.pages)

    catalog = pdf.reader.trailer["/Root"]
    names = catalog.get("/Names")
    if "/Dests" in catalog or (names is not None and "/Dests" in names.get_object()):
        problems.append("в PDF остались именованные назначения")

    sections = top_level_sections(pdf)
    report["sections"] = [s[0] for s in sections]
    section_by_title = {}
    for sp in site.pages.values():
        for i, section in enumerate(sections):
            if section[0] == sp["title"]:
                end = sections[i + 1][1] if i + 1 < len(sections) else total_pages
                section_by_title[page_key(sp["url"])] = (section[1], end)
    for sp in site.pages.values():
        if page_key(sp["url"]) not in section_by_title:
            problems.append("нет раздела для %s (%s)" % (sp["url"], sp["title"]))
    if problems:
        print("\n".join(problems))
        return 1

    check_links(pdf, site, args, section_by_title, report, problems)
    check_text(pdf, site, args, section_by_title, report, problems)
    check_bookmarks(pdf, site, sections, args, section_by_title, report, problems)
    check_sheets(pdf, report, problems)

    report["problems"] = problems
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in report.items() if k != "problems"}, ensure_ascii=False, indent=1))
    if problems:
        print("ПРОБЛЕМЫ:")
        for p in problems:
            print(" -", p[:1500])
        return 1
    print("ВСЁ ВЕРНО: ни одной ссылки мимо")
    return 0


if __name__ == "__main__":
    sys.exit(main())
