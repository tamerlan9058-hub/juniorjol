#!/usr/bin/env python3
"""Собирает сайт для GitHub Pages: вакансии вписываются прямо в HTML (для поисковиков
и превью ссылок), рядом кладутся sitemap.xml и данные.

Использование: python3 scripts/build_site.py _site
"""
import html
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board  # noqa: E402

E = html.escape


def site_url(cfg):
    if cfg.get("site_url"):
        return cfg["site_url"].rstrip("/") + "/"
    owner, _, name = (cfg.get("repo") or "/").partition("/")
    return f"https://{owner.lower()}.github.io/{name}/" if owner and name else ""


def card(item):
    featured = board.is_featured(item)
    pay = item["paid"] + (f" · {item['salary']}" if item.get("salary") else "")
    tags = (["⭐ Featured"] if featured else []) + [item["type"], item["direction"], board.where(item), pay]
    if item.get("deadline"):
        tags.append("до " + board.short_date(item["deadline"]))
    tag_html = "".join(
        f'<span class="tag{" star" if t == "⭐ Featured" else ""}">{E(t)}</span>' for t in tags)
    url = item["url"] if item["url"].startswith("https://") else "#"
    return (f'<article class="card{" featured" if featured else ""}">'
            f'<div><h2>{E(item["title"])}</h2><div class="company">{E(item["company"])}</div></div>'
            f'<div class="tags">{tag_html}</div>'
            f'<a class="btn primary apply" href="{E(url)}" target="_blank" rel="noopener nofollow">Откликнуться</a>'
            f'</article>')


def head_tags(cfg, url, count):
    title = f"{cfg.get('brand', 'JuniorJol')} — стажировки и junior-вакансии в IT Казахстана"
    desc = (f"{count} актуальных стажировок и junior-вакансий в IT: Алматы, Астана, удалённо. "
            "Проверены модератором, бесплатно для студентов.")
    tags = [
        f'<meta property="og:type" content="website">',
        f'<meta property="og:title" content="{E(title)}">',
        f'<meta property="og:description" content="{E(desc)}">',
        '<meta property="og:locale" content="ru_RU">',
        '<meta name="twitter:card" content="summary">',
    ]
    if url:
        tags += [f'<link rel="canonical" href="{E(url)}">', f'<meta property="og:url" content="{E(url)}">']
    if cfg.get("google_site_verification"):
        tags.append(f'<meta name="google-site-verification" content="{E(cfg["google_site_verification"])}">')
    if cfg.get("yandex_verification"):
        tags.append(f'<meta name="yandex-verification" content="{E(cfg["yandex_verification"])}">')
    return "\n".join(tags)


def build(out):
    out = Path(out)
    cfg = board.config()
    items = board.sorted_listings(board.load_json(board.LISTINGS, []))
    meta = board.load_json(board.META, {})
    url = site_url(cfg)

    page = (board.ROOT / "site" / "index.html").read_text(encoding="utf-8")
    page = page.replace("<!-- SEO -->", head_tags(cfg, url, len(items)))
    page = page.replace('<p class="count" id="count">Загрузка…</p>',
                        f'<p class="count" id="count">Найдено: {len(items)} из {len(items)}</p>')
    page = page.replace('<div class="list" id="list"></div>',
                        '<div class="list" id="list">' + "".join(card(i) for i in items) + "</div>")

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(board.ROOT / "site", out)
    (out / "index.html").write_text(page, encoding="utf-8")
    for name in ("listings.json", "feed.xml", "meta.json"):
        shutil.copy(board.DATA / name, out / name)

    # robots.txt для сайта проекта бесполезен: поисковики читают его только из корня домена
    # (owner.github.io/robots.txt). Карту сайта отправляем через Search Console / Яндекс Вебмастер.
    if url:
        lastmod = f"<lastmod>{meta['updated']}</lastmod>" if meta.get("updated") else ""
        (out / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"<url><loc>{E(url)}</loc>{lastmod}<changefreq>daily</changefreq></url>\n"
            "</urlset>\n", encoding="utf-8")
    print(f"Сайт собран в {out}: {len(items)} вакансий, адрес {url or 'не задан'}")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "_site")
