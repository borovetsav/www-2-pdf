#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Верификатор PDF. Ожидания берёт из самого сайта, а не из www2pdf.py.

Для каждой ссылки в PDF: текст под ссылкой сверяется с видимым текстом ссылки на
сайте, а у точки перехода (в пределах --window pt ниже) должна стоять именно та
запись, на которую ссылка ведёт на сайте (ФИО из summary, код и заголовок источника).

Запуск: python verify.py файл.pdf http://сайт/ [--markers markers.txt] [--images]
Код возврата 0 только если нет ни одного расхождения.
"""
import argparse
import hashlib
import json
import posixpath
import re
import sys
import unicodedata
import urllib.request
from collections import Counter, defaultdict
from urllib.parse import unquote, urljoin, urlsplit

import pdfplumber
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.generic import ArrayObject

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif"}
TOP_TOLERANCE = 3  # допуск в pt: глиф может начинаться чуть выше точки перехода


def norm(text):
    """Сравниваем без пробелов и дефисов: pdfplumber отдаёт дефис из шрифтов Chrome как U+E088."""
    text = unicodedata.normalize("NFKC", text or "")  # лигатуры шрифта: ﬁ -> fi
    return re.sub(r"[\s\-‐-―­]+", "", text)


def fetch(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", "replace"), r.headers.get("content-type", "")


def page_key(url):
    parts = urlsplit(url)
    path = parts.path or "/"
    if posixpath.basename(path).lower() in ("index.html", "index.htm"):
        path = posixpath.dirname(path).rstrip("/") + "/"
    return parts.scheme, parts.netloc, path, parts.query


def expected_text(el):
    """Строка, которая должна стоять у точки перехода на этот элемент."""
    summary = el if el.name == "summary" else el.find("summary")
    if summary is not None:
        bold = summary.find("b")
        return norm((bold or summary).get_text())[:40]
    return norm(el.get_text())[:40]


class Site:
    """Обход сайта стандартными средствами, независимо от браузера и от www2pdf.py."""

    def __init__(self, root):
        self.root = root
        self.pages = {}
        queue, seen = [root], {page_key(root)}
        while queue:
            url = queue.pop(0)
            html, ctype = fetch(url)
            if "html" not in ctype:
                continue
            soup = BeautifulSoup(html, "html.parser")
            key = page_key(url)
            ids = {}
            for el in soup.find_all(id=True):
                ids.setdefault(el["id"], el)  # при повторах браузер идёт на первый
            links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                links.append({"href": href, "text": norm(a.get_text()), "title": a.get("title"),
                              "own": self.is_own(href)})
                if self.is_own(href) and posixpath.splitext(urlsplit(href).path)[1].lower() not in IMAGE_EXT:
                    target = page_key(href)
                    if target not in seen:
                        seen.add(target)
                        queue.append(href.split("#")[0])
            self.pages[key] = {"url": url, "soup": soup, "ids": ids, "links": links,
                               "title": soup.title.get_text().strip() if soup.title else ""}

    def is_own(self, href):
        a, b = urlsplit(href), urlsplit(self.root)
        return a.scheme in ("http", "https") and (a.hostname, a.port) == (b.hostname, b.port)


class Pdf:
    def __init__(self, path):
        self.reader = PdfReader(path)
        self.plumber = pdfplumber.open(path)
        self.page_index = {p.indirect_reference.idnum: i for i, p in enumerate(self.reader.pages)}
        self.chars = {}

    def page_chars(self, index):
        if index not in self.chars:
            self.chars[index] = self.plumber.pages[index].chars
        return self.chars[index]

    def page_has_image(self, index, href):
        xobjects = self.reader.pages[index]["/Resources"].get("/XObject")
        images = [v.get_object() for v in (xobjects.get_object().values() if xobjects else [])]
        images = [o for o in images if o.get("/Subtype") == "/Image"]
        if not images:
            return False
        if not href.lower().endswith((".jpg", ".jpeg")):
            return True
        with urllib.request.urlopen(href, timeout=60) as r:
            want = hashlib.sha1(r.read()).hexdigest()
        return any(hashlib.sha1(o._data).hexdigest() == want for o in images)

    def height(self, index):
        return float(self.reader.pages[index].mediabox.height)

    def text_window(self, index, y, window):
        """Текст в полосе от точки перехода y вниз на window pt (порядок как в потоке)."""
        top = self.height(index) - y
        chars = [c for c in self.page_chars(index)
                 if top - TOP_TOLERANCE <= c["top"] and c["top"] <= top + window]
        return norm("".join(c["text"] for c in chars))

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
        """[page_ref /XYZ 0 y null] -> (номер листа, y). Именованные назначения -> None."""
        if not isinstance(dest, ArrayObject) or len(dest) < 4:
            return None
        idnum = getattr(list.__getitem__(dest, 0), "idnum", None)
        if idnum not in self.page_index or str(dest[1]) != "/XYZ":
            return None
        return self.page_index[idnum], float(dest[3])


def top_level_sections(pdf):
    """Разделы PDF по закладкам верхнего уровня."""
    sections = []
    for item in pdf.reader.outline:
        if not isinstance(item, list):
            sections.append((str(item.title), pdf.reader.get_destination_page_number(item)))
    return sections


def walk_outline(reader, items, out):
    for item in items:
        if isinstance(item, list):
            walk_outline(reader, item, out)
        else:
            out.append(item)


def collect_pdf_links(pdf, first, last):
    """Логические ссылки раздела в порядке документа."""
    links = []
    for index in range(first, last):
        for a in pdf.annotations(index):
            action = a["/A"].get_object() if "/A" in a else None
            entry = {"page": index, "rect": [float(v) for v in a["/Rect"]],
                     "text": pdf.text_under(index, [float(v) for v in a["/Rect"]]),
                     "contents": str(a["/Contents"]) if "/Contents" in a else None}
            if "/Dest" in a:
                entry.update(kind="dest", raw=str(a["/Dest"]))
            elif action is None:
                entry.update(kind="noaction")
            elif action.get("/S") == "/URI":
                entry.update(kind="uri", uri=str(action["/URI"]))
            elif action.get("/S") == "/GoTo":
                target = pdf.explicit_target(action["/D"])
                entry.update(kind="goto", target=target, raw=str(action["/D"]))
            else:
                entry.update(kind="other")
            links.append(entry)
    return links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("site")
    ap.add_argument("--markers")
    ap.add_argument("--window", type=float, default=45)
    ap.add_argument("--json")
    ap.add_argument("--images", action="store_true", help="картинки сайта должны вести на приложение в PDF")
    args = ap.parse_args()

    site = Site(args.site)
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
    site_pages = list(site.pages.values())
    for sp in site_pages:
        for i, (title, start) in enumerate(sections):
            if title == sp["title"]:
                end = sections[i + 1][1] if i + 1 < len(sections) else total_pages
                section_by_title[page_key(sp["url"])] = (start, end)
    for sp in site_pages:
        if page_key(sp["url"]) not in section_by_title:
            problems.append("нет раздела для %s (%s)" % (sp["url"], sp["title"]))
    if problems:
        print("\n".join(problems))
        return 1

    # 1. Каждая ссылка каждого раздела
    counts = Counter()
    misses, unexplained = [], []
    missing_anchor_list = []
    tiny = 0
    for key, sp in site.pages.items():
        first, last = section_by_title[key]
        pdf_links = collect_pdf_links(pdf, first, last)
        pending = defaultdict(list)
        for link in sp["links"]:
            pending[link["text"]].append(link)
        report.setdefault("site_links_total", 0)
        report["site_links_total"] += len(sp["links"])
        i = 0
        while i < len(pdf_links):
            link = pdf_links[i]
            rect = link["rect"]
            if abs(rect[2] - rect[0]) < 2 or abs(rect[3] - rect[1]) < 2:
                tiny += 1
            text = link["text"]
            used = 1
            # Ссылка на несколько строк даёт несколько прямоугольников с одной целью
            while (text not in pending or not pending[text]) and i + used < len(pdf_links):
                nxt = pdf_links[i + used]
                if nxt.get("raw") != link.get("raw") or nxt.get("uri") != link.get("uri"):
                    break
                text += nxt["text"]
                used += 1
            cands = pending.get(text) or []
            if not cands:
                unexplained.append((sp["url"], link["page"] + 1, link["text"], link.get("raw") or link.get("uri")))
                i += 1
                continue
            hit = match_link(pdf, site, link, cands, args.window, section_by_title, counts, missing_anchor_list)
            if hit is None:
                misses.append({"from": sp["url"], "page": link["page"] + 1, "text": text,
                               "goto": str(link.get("target")),
                               "uri": link.get("uri"),
                               "candidates": [c["href"] for c in cands[:3]]})
                cands.pop(0)
            else:
                cands.remove(hit)
            i += used
            # Ссылка с картинкой внутри текста даёт ещё и пустой прямоугольник с той же целью
            while i < len(pdf_links) and pdf_links[i]["text"] == "" and pdf_links[i].get("raw") == link.get("raw"):
                i += 1
        for lst in pending.values():
            for lost in lst:
                problems.append("ссылка с сайта не найдена в PDF: %s -> %s (%r)" % (sp["url"], lost["href"], lost["text"]))
    report["links"] = dict(counts)
    report["missing_anchors_on_site"] = sorted(set(missing_anchor_list))
    report["missing_anchor_links"] = len(missing_anchor_list)
    report["misses"] = len(misses)
    if misses:
        problems.append("переходы мимо цели: %d, примеры: %s" % (len(misses), json.dumps(misses[:8], ensure_ascii=False)))
    if unexplained:
        problems.append("ссылки PDF без пары на сайте: %d, примеры: %s" % (len(unexplained), unexplained[:8]))
    if tiny:
        problems.append("остались служебные крошечные ссылки: %d" % tiny)

    # 2. Скрытый текст
    stream_text = {}
    for key, (first, last) in section_by_title.items():
        stream_text[key] = norm("".join(c["text"] for i in range(first, last) for c in pdf.page_chars(i)))
    details_total = details_ok = 0
    lost_blocks = []
    for key, sp in site.pages.items():
        for d in sp["soup"].find_all("details"):
            details_total += 1
            blocks = [d.find("summary")] + list(d.find_all(["p", "li", "div"], recursive=True))
            ok = True
            for b in blocks:
                if b is None or b.find(["p", "li", "div", "ul", "ol", "table"]):
                    continue
                want = norm(b.get_text())
                if want and want not in stream_text[key]:
                    ok = False
                    lost_blocks.append((sp["url"], b.get_text()[:80]))
            details_ok += ok
    report["details_site"] = details_total
    report["details_in_pdf"] = details_ok
    if lost_blocks:
        problems.append("потерян текст в details: %d, примеры: %s" % (len(lost_blocks), lost_blocks[:5]))
    if args.markers:
        all_text = "".join(stream_text.values())
        markers = [m.strip() for m in open(args.markers, encoding="utf-8") if m.strip()]
        lost = [m for m in markers if norm(m) not in all_text]
        report["markers"] = len(markers)
        report["markers_found"] = len(markers) - len(lost)
        if lost:
            problems.append("не найдены маркеры: %s" % lost[:10])

    # 3. Закладки ведут на свои заголовки
    outline = []
    walk_outline(pdf.reader, pdf.reader.outline, outline)
    bad_outline = []
    for item in outline:
        index = pdf.reader.get_destination_page_number(item)
        y = float(item.top) if item.top is not None else pdf.height(index)
        want = norm(str(item.title))[:25]
        if any(str(item.title) == s[0] for s in sections):
            continue
        if want not in pdf.text_window(index, y, args.window):
            bad_outline.append((str(item.title)[:50], index + 1))
    report["bookmarks"] = len(outline)
    report["bookmarks_ok"] = len(outline) - len(bad_outline)
    if bad_outline:
        problems.append("закладки мимо: %d, примеры: %s" % (len(bad_outline), bad_outline[:8]))
    expected_bookmarks = sum(len(sp["soup"].find_all(["h1", "h2", "h3", "summary"])) for sp in site.pages.values())
    report["bookmarks_expected_h1_h3_summary"] = expected_bookmarks
    last_site_page = max(end for _, end in section_by_title.values())
    site_outline_count = sum(1 for item in outline if pdf.reader.get_destination_page_number(item) < last_site_page)
    report["bookmarks_site_sections"] = site_outline_count - len(site.pages)
    if site_outline_count - len(site.pages) != expected_bookmarks:
        problems.append("закладок в разделах сайта %d, а заголовков и людей на сайте %d" % (site_outline_count - len(site.pages), expected_bookmarks))

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


def match_link(pdf, site, link, cands, window, section_by_title, counts, missing_list):
    """Находит на сайте ссылку, которой соответствует эта ссылка PDF, и проверяет цель."""
    for cand in cands:
        href = cand["href"]
        if not cand["own"]:
            if link["kind"] == "uri" and unquote(link["uri"]) == unquote(href):
                counts["external_uri"] += 1
                return cand
            continue
        path_ext = posixpath.splitext(urlsplit(href).path)[1].lower()
        if link["kind"] != "goto" or not link.get("target"):
            continue
        index, y = link["target"]
        if path_ext in IMAGE_EXT:
            # Перешли на лист приложения, где лежит именно этот файл (JPEG хранится побайтно)
            if link["text"] == "" and pdf.page_has_image(index, href):
                counts["image_goto"] += 1
                return cand
            continue
        target_key = page_key(href)
        if target_key not in section_by_title:
            continue
        first, last = section_by_title[target_key]
        frag = unquote(urlsplit(href).fragment)
        target_page = site.pages[target_key]
        if frag and frag in target_page["ids"]:
            want = expected_text(target_page["ids"][frag])
            if first <= index < last and want in pdf.text_window(index, y, window):
                counts["anchor_exact"] += 1
                return cand
        else:
            if index == first and y >= pdf.height(index) - 1:
                if frag:
                    counts["anchor_missing_to_section"] += 1
                    missing_list.append("%s#%s" % (urlsplit(href).path.lstrip("/"), frag))
                else:
                    counts["section_start"] += 1
                return cand
    return None


if __name__ == "__main__":
    sys.exit(main())
