#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор тестового сайта той же структуры, что у сайта-родословной.

4 страницы и липкая шапка, ~160 людей в li#M-P-xxxx > details, ~120 источников
#M-A-xxxx в таблице с display:block, ссылки с якорями между страницами и внутри
страницы, внешняя ссылка. В каждом скрытом блоке есть уникальный маркер.
Маркеры сохраняются в markers.txt рядом с сайтом.
"""
import base64
import os
import sys

PEOPLE = 160
SOURCES = 120
PER_PAGE = PEOPLE // 2

CSS = """:root { --bg: #faf8f4; --fg: #22201c; --card: #fff; --line: #ddd5c7; --accent: #8a3b12; --link: #1f5f8b; }
@media (prefers-color-scheme: dark) { :root { --bg: #1b1a18; --fg: #ebe6dc; --card: #252320; --line: #3b3731; --link: #7db8e3; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 -apple-system, Arial, sans-serif; }
.wrap { max-width: 980px; margin: 0 auto; padding: 0 16px; }
header { background: var(--card); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
nav a { margin-right: 14px; }
a { color: var(--link); }
table { border-collapse: collapse; width: 100%; display: block; overflow-x: auto; }
td, th { border: 1px solid var(--line); padding: 6px 8px; min-width: 160px; vertical-align: top; }
.badge { font-size: .74rem; padding: 2px 7px; border-radius: 10px; color: #fff; background: #2f8a4c; }
.tree summary { padding: 4px 8px; background: var(--card); border: 1px solid var(--line); border-radius: 6px; display: inline-block; }
.info { padding: 8px 12px; border: 1px dashed var(--line); }
.tabpanel[hidden] { display: none; }
.collapse { display: none; }
"""

FILLER = ("Подробный текст для объёма, чтобы вёрстка занимала много листов и якоря "
          "попадали в разные места страницы при любом масштабе. ")

# Маленький настоящий PNG: 1x1 пиксель
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def page(title, active, body):
    nav = "".join('<a href="%s">%s</a>' % (h, t) for h, t in [
        ("index.html", "Главная"), ("mama.html", "Линия мамы"),
        ("papa.html", "Линия папы"), ("sources.html", "Источники")])
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>%s · Макет</title>'
            '<link rel="stylesheet" href="style.css"></head><body><header><div class="wrap"><nav>%s</nav></div></header>'
            '<main class="wrap">%s</main></body></html>' % (title, nav, body))


def src_link(n):
    return ('<a class="ref" href="sources.html#M-A-%04d" title="Полное описание источника номер %d">A-%04d</a>'
            % (n, n, n))


def person(prefix, n, markers):
    marker = "СКРЫТО-%s-%04d" % (prefix, n)
    markers.append(marker)
    return ('<li class="node" id="%s-P-%04d"><details><summary><span class="role">предок</span> '
            '<b>Человек %s-P-%04d</b> <span class="badge">гипотеза</span></summary><div class="info">'
            '<p>Почему так считаем (R-%04d): %s %s</p><p>Источник: %s</p></div></details></li>'
            % (prefix, n, prefix, n, n, FILLER, marker, src_link(n % SOURCES + 1)))


def people_page(title, prefix, numbers, markers, other_page, other_prefix):
    items = "".join(person(prefix, n, markers) for n in numbers)
    jumps = "".join('<a href="#%s-P-%04d">к %s-P-%04d</a>, ' % (prefix, n, prefix, n) for n in numbers[::5])
    cross = "".join('<a href="%s#%s-P-%04d">там %s-P-%04d</a>, ' % (other_page, other_prefix, n, other_prefix, n)
                    for n in (range(1, PER_PAGE + 1, 7) if prefix == "P" else range(PER_PAGE + 1, PEOPLE + 1, 7)))
    return page(title, prefix, '<h1>%s</h1><h2 id="переходы">Переходы по странице</h2><p>%s</p><h2>Соседняя линия</h2><p>%s</p>'
                '<h2 id="древо">Древо</h2><ul class="tree">%s</ul>' % (title, jumps, cross, items))


def make(out):
    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    markers = []
    with open(os.path.join(out, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)
    with open(os.path.join(out, "img", "photo.png"), "wb") as f:
        f.write(PNG)

    index_links = "".join('<li><a href="mama.html#M-P-%04d">К человеку M-P-%04d</a></li>' % (n, n) for n in range(1, PER_PAGE + 1, 3))
    index_links += "".join('<li><a href="papa.html#P-P-%04d">К человеку P-P-%04d</a></li>' % (n, n) for n in range(PER_PAGE + 1, PEOPLE + 1, 3))
    index_links += "".join("<li>%s</li>" % src_link(n) for n in range(1, SOURCES + 1, 2))
    index_body = (
        '<h1 id="главная">Главная</h1><p><a href="https://example.com/external?x=1">Внешний сайт</a></p>'
        # Идентификатор кириллицей, отсутствующий якорь и картинка
        '<p><a href="mama.html#древо">К древу мамы</a>, <a href="mama.html#НЕТ-ТАКОГО">Несуществующий якорь</a>, '
        '<a href="#нет-здесь">Несуществующий здесь</a>, <a href="img/photo.png"><img src="img/photo.png" alt="Фото" width="40" height="40"></a></p>'
        # Другие раскрывашки: aria-controls, вкладка и collapse
        '<button aria-expanded="false" aria-controls="panel1">Открыть</button>'
        '<div id="panel1" hidden>СКРЫТО-ARIA-0001</div>'
        '<div role="tabpanel" class="tabpanel" hidden>СКРЫТО-TAB-0001</div>'
        '<div class="collapse">СКРЫТО-COLLAPSE-0001</div>'
        '<details><summary>Раскрывашка без id</summary><div class="info">СКРЫТО-MAIN-0001</div></details>'
        '<h2>Ссылки</h2><ul>%s</ul>' % index_links)
    markers.extend(["СКРЫТО-ARIA-0001", "СКРЫТО-TAB-0001", "СКРЫТО-COLLAPSE-0001", "СКРЫТО-MAIN-0001"])
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page("Главная", "", index_body))

    with open(os.path.join(out, "mama.html"), "w", encoding="utf-8") as f:
        f.write(people_page("Линия мамы", "M", list(range(1, PER_PAGE + 1)), markers, "papa.html", "P"))
    with open(os.path.join(out, "papa.html"), "w", encoding="utf-8") as f:
        f.write(people_page("Линия папы", "P", list(range(PER_PAGE + 1, PEOPLE + 1)), markers, "mama.html", "M"))

    rows = "".join(
        '<tr id="M-A-%04d"><td>A-%04d</td><td>%s</td><td>%s</td></tr>' % (n, n, FILLER, "колонка " * 12)
        for n in range(1, SOURCES + 1))
    sources_body = ('<h1>Источники</h1><p><a href="#M-A-0100">К источнику A-0100</a></p>'
                    '<table><tr><th>Код</th><th>Описание</th><th>Заметки</th></tr>%s</table>' % rows)
    with open(os.path.join(out, "sources.html"), "w", encoding="utf-8") as f:
        f.write(page("Источники", "", sources_body))

    with open(os.path.join(out, "markers.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(markers) + "\n")
    return markers


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "tests/out/site"
    print("Макет: %s, маркеров %d" % (target, len(make(target))))
