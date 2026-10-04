#!/usr/bin/env python3
"""JuniorJol: доска стажировок и junior-вакансий, которая живёт в GitHub.

Только стандартная библиотека Python. Скрипт не ходит в сеть, кроме команд
telegram и telegram-digest, поэтому всё остальное можно проверять тестами.

Команды:
  intake  --event FILE   проверить заявку из Issue Form, подготовить комментарий бота
  apply   --event FILE   опубликовать / закрыть / продвинуть по метке модератора
  expire                 убрать просроченные вакансии и закончившееся продвижение
  render  [--check]      перегенерировать README, docs/employers.md, feed.xml, meta.json
  telegram --ids ID,...  отправить посты о новых вакансиях в Telegram
  telegram-digest        отправить еженедельный дайджест в Telegram
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(os.environ.get("BOARD_ROOT", Path(__file__).resolve().parent.parent))
DATA = ROOT / "data"
LISTINGS = DATA / "listings.json"
ARCHIVE = DATA / "archive.json"
FEED = DATA / "feed.xml"
META = DATA / "meta.json"
CONFIG = ROOT / "config.json"
README = ROOT / "README.md"
EMPLOYERS = ROOT / "docs" / "employers.md"
OUT = ROOT / "out"

BOT_MARK = "<!-- juniorjol-bot -->"
NEW_FORM = "new-listing.yml"

# Подписи полей должны совпадать с label в .github/ISSUE_TEMPLATE/*.yml (проверяется тестом).
FIELDS = {
    "Компания": "company",
    "Позиция": "title",
    "Тип": "type",
    "Направление": "direction",
    "Город": "city",
    "Формат работы": "format",
    "Оплата": "paid",
    "Зарплата / стипендия": "salary",
    "Ссылка на вакансию": "url",
    "Дедлайн отклика": "deadline",
    "Продвижение": "promotion",
    "Подтверждение": "confirm",
}
LABEL = {v: k for k, v in FIELDS.items()}
CLOSE_FIELDS = {
    "Номер заявки или ссылка на вакансию": "target",
    "Причина": "reason",
}
REQUIRED = ["company", "title", "type", "direction", "city", "format", "paid", "url", "promotion"]
CHOICES = {
    "type": ["Стажировка", "Junior-вакансия", "Part-time для студентов"],
    "direction": ["Backend", "Frontend", "Fullstack", "Mobile", "QA", "Data / ML",
                  "DevOps / SRE", "UI/UX дизайн", "Product / Project", "Другое"],
    "city": ["Алматы", "Астана", "Шымкент", "Караганда", "Другой город", "Любой (удалённо)"],
    "format": ["Офис", "Гибрид", "Удалённо"],
    "paid": ["Оплачиваемая", "Неоплачиваемая", "Не указано"],
    "promotion": ["Бесплатное размещение", "Featured — закрепить вверху на 30 дней (платно)"],
}
CLOSE_REASONS = ["Набор закрыт", "Вакансия неактуальна", "Ошибка в объявлении", "Другое"]
FEATURED_OPTION = CHOICES["promotion"][1]
CONFIRMATIONS = 2
LIMITS = {"company": 80, "title": 120, "salary": 60}

# Трудовой кодекс РК запрещает дискриминацию при приёме на работу: такие заявки не публикуем.
DISCRIMINATION = [
    r"девушк", r"\bмужчин", r"\bженщин", r"\bпарн(?:и|ей|я)\b", r"\bдо\s*\d{2}\s*лет",
    r"не\s+старше", r"славянск", r"приятн\w*\s+внешн", r"\bтолько\s+(?:муж|жен)",
]

TYPE_SHORT = {"Стажировка": "Стажировка", "Junior-вакансия": "Junior", "Part-time для студентов": "Part-time"}
PAID_ICON = {"Оплачиваемая": "💰", "Неоплачиваемая": "—", "Не указано": "?"}

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# --------------------------------------------------------------------------- utils

def today():
    forced = os.environ.get("BOARD_TODAY")
    if forced:
        return dt.date.fromisoformat(forced)
    # Казахстан живёт по UTC+5.
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5)).date()


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def config():
    return load_json(CONFIG, {})


def money(value, cfg):
    return f"{value:,}".replace(",", " ") + " " + cfg.get("currency", "₸")


def one_line(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def md_escape(text):
    """Экранирует текст для ячейки таблицы GitHub Markdown."""
    text = one_line(text)
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", text)


def no_mention(text):
    # Чтобы бот не упоминал пользователей через @ в комментариях.
    return text.replace("@", "@​")


def ru_date(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def short_date(iso):
    d = dt.date.fromisoformat(iso)
    return d.strftime("%d.%m.%Y")


def parse_date(raw):
    raw = one_line(raw)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def check_url(raw):
    url = one_line(raw)
    if not url:
        return None, "Укажите ссылку на вакансию."
    if len(url) > 500 or re.search(r"[\s<>\"'`]", url):
        return None, "Ссылка выглядит некорректно: уберите пробелы и кавычки."
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or "." not in parts.netloc or "@" in parts.netloc:
        return None, "Ссылка должна начинаться с https:// и вести на сайт компании или job-платформу."
    # Скобки ломают Markdown-ссылки.
    return url.replace("(", "%28").replace(")", "%29"), None


def is_featured(item, on=None):
    on = on or today()
    until = item.get("featured_until")
    return bool(until) and dt.date.fromisoformat(until) >= on


def sorted_listings(items):
    featured = sorted([i for i in items if is_featured(i)], key=lambda i: (i["added"], i["issue"]), reverse=True)
    rest = sorted([i for i in items if not is_featured(i)], key=lambda i: (i["added"], i["issue"]), reverse=True)
    return featured + rest


# --------------------------------------------------------------------------- forms

def parse_form(body, fields):
    sections, current = {}, None
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            current = m.group(1).strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    out = {}
    for label, key in fields.items():
        raw = "\n".join(sections.get(label, [])).strip()
        out[key] = "" if raw == "_No response_" else raw
    return out


def issue_labels(issue):
    return {lbl["name"] if isinstance(lbl, dict) else lbl for lbl in issue.get("labels", [])}


def build_listing(issue, cfg, on=None):
    """Возвращает (listing, errors, wants_featured)."""
    on = on or today()
    f = parse_form(issue.get("body"), FIELDS)
    errors = []

    for key in REQUIRED:
        if not one_line(f[key]):
            errors.append(f"Не заполнено поле «{LABEL[key]}».")
    for key, options in CHOICES.items():
        if f[key] and one_line(f[key]) not in options:
            errors.append(f"В поле «{LABEL[key]}» неизвестное значение. Выберите вариант из списка.")
    for key, limit in LIMITS.items():
        if len(one_line(f[key])) > limit:
            errors.append(f"Поле «{LABEL[key]}» слишком длинное (максимум {limit} символов).")

    url, url_error = check_url(f["url"])
    if url_error and f["url"]:
        errors.append(url_error)

    deadline = None
    if one_line(f["deadline"]):
        deadline = parse_date(f["deadline"])
        if not deadline:
            errors.append("Дедлайн укажите в формате ГГГГ-ММ-ДД, например 2026-11-30.")
        elif deadline < on:
            errors.append("Дедлайн уже прошёл.")
        elif deadline > on + dt.timedelta(days=365):
            errors.append("Дедлайн дальше чем через год — укажите реальную дату или оставьте поле пустым.")

    if len(re.findall(r"^\s*-\s*\[[xX]\]", f["confirm"], re.M)) < CONFIRMATIONS:
        errors.append("Отметьте оба пункта в блоке «Подтверждение».")

    text = " ".join([f["company"], f["title"], f["salary"]]).lower()
    if any(re.search(p, text) for p in DISCRIMINATION):
        errors.append("В объявлении есть требования к полу, возрасту или внешности. "
                      "Такие требования запрещены Трудовым кодексом РК — уберите их.")

    expires = deadline or (on + dt.timedelta(days=cfg.get("default_ttl_days", 45)))
    listing = {
        "id": f"jj-{issue['number']}",
        "issue": issue["number"],
        "company": one_line(f["company"]),
        "title": one_line(f["title"]),
        "type": one_line(f["type"]),
        "direction": one_line(f["direction"]),
        "city": one_line(f["city"]),
        "format": one_line(f["format"]),
        "paid": one_line(f["paid"]),
        "salary": one_line(f["salary"]),
        "url": url,
        "deadline": deadline.isoformat() if deadline else None,
        "added": on.isoformat(),
        "expires": expires.isoformat(),
        "featured_until": None,
    }
    wants_featured = one_line(f["promotion"]) == FEATURED_OPTION
    return listing, errors, wants_featured


# --------------------------------------------------------------------------- rendering

def where(item):
    if item["city"] == "Любой (удалённо)":
        return "Удалённо"
    return f"{item['city']} · {item['format']}"


def pay(item):
    icon = PAID_ICON.get(item["paid"], "?")
    return f"{icon} {md_escape(item['salary'])}" if item.get("salary") else icon


def table_row(item):
    star = "⭐" if is_featured(item) else ""
    deadline = short_date(item["deadline"]) if item.get("deadline") else "—"
    return (f"| {star} | **{md_escape(item['company'])}** | {md_escape(item['title'])} | "
            f"{TYPE_SHORT.get(item['type'], md_escape(item['type']))} · {md_escape(item['direction'])} | "
            f"{md_escape(where(item))} | {pay(item)} | {deadline} | [Откликнуться]({item['url']}) |")


TABLE_HEAD = ("|   | Компания | Позиция | Тип | Где | Оплата | Дедлайн |   |\n"
              "|---|---|---|---|---|---|---|---|")


def last_change(items, archive):
    """Дата последнего изменения данных — чтобы README не менялся каждый день без причины."""
    dates = [i["added"] for i in items] + [i.get("closed", i["added"]) for i in archive]
    return dt.date.fromisoformat(max(dates)) if dates else today()


def render_listings_block(items, on, ttl=45):
    items = sorted_listings(items)
    counts = {t: sum(1 for i in items if i["type"] == t) for t in CHOICES["type"]}
    lines = [
        f"**Активных: {len(items)}** · стажировок: {counts['Стажировка']} · "
        f"junior: {counts['Junior-вакансия']} · part-time: {counts['Part-time для студентов']} · "
        f"обновлено {ru_date(on)} · [JSON](data/listings.json) · [RSS](data/feed.xml)",
        "",
    ]
    if items:
        lines.append(TABLE_HEAD)
        lines += [table_row(i) for i in items]
        lines.append("")
        lines.append("⭐ — Featured: работодатель продвигает вакансию. "
                     f"«—» в дедлайне — вакансия висит {ttl} дней или до закрытия набора.")
    else:
        lines.append("> Сейчас нет активных вакансий. "
                     f"[Разместите первую →](../../issues/new?template={NEW_FORM})")
    return "\n".join(lines)


def render_prices_block(cfg):
    p = cfg.get("prices", {})
    days = cfg.get("featured_days", 30)
    rows = [
        "| Тариф | Что входит | Цена |",
        "|---|---|---|",
        "| **Бесплатно** | Вакансия в таблице README, на сайте, в JSON/RSS и пост в Telegram-канале | 0 ₸ |",
        f"| **Featured** | ⭐ вверху списка на {days} дней, закреп в Telegram на 3 дня, "
        f"повтор в еженедельном дайджесте | {money(p.get('featured', 0), cfg)} |",
        "| **Пакет «Набор стажёров»** | 3 Featured-вакансии + отдельный пост о вашей программе "
        f"стажировок | {money(p.get('hiring_pack', 0), cfg)} |",
        "| **Спонсор месяца** | Строка с логотипом/ссылкой в шапке README и сайта + упоминание "
        f"в каждом дайджесте | {money(p.get('sponsor_month', 0), cfg)} / мес |",
    ]
    if cfg.get("launch_promo"):
        rows += ["", "> 🚀 **Запуск:** первые 10 работодателей получают Featured бесплатно."]
    return "\n".join(rows)


def replace_block(text, name, content):
    start, end = f"<!-- {name}:START -->", f"<!-- {name}:END -->"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if not pattern.search(text):
        raise SystemExit(f"Не найдены маркеры {start} / {end}")
    return pattern.sub(lambda _: f"{start}\n{content}\n{end}", text)


def repo_url(cfg):
    repo = cfg.get("repo")
    return f"https://github.com/{repo}" if repo else "https://github.com/"


def render_feed(items, cfg):
    def rfc(iso):
        d = dt.date.fromisoformat(iso)
        return format_datetime(dt.datetime(d.year, d.month, d.day, 9, tzinfo=dt.timezone(dt.timedelta(hours=5))))

    esc = html.escape
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{esc(cfg.get('brand', 'JuniorJol'))}</title>",
        f"<description>{esc(cfg.get('tagline', ''))}</description>",
        f"<link>{esc(repo_url(cfg))}</link>",
        "<language>ru</language>",
    ]
    for i in sorted(items, key=lambda i: (i["added"], i["issue"]), reverse=True):
        desc = f"{i['type']} · {i['direction']} · {where(i)} · {i['paid']}"
        if i.get("salary"):
            desc += f" ({i['salary']})"
        if i.get("deadline"):
            desc += f" · дедлайн {short_date(i['deadline'])}"
        parts += [
            "<item>",
            f"<title>{esc(i['title'])} — {esc(i['company'])}</title>",
            f"<link>{esc(i['url'])}</link>",
            f'<guid isPermaLink="false">{esc(i["id"])}</guid>',
            f"<pubDate>{rfc(i['added'])}</pubDate>",
            f"<description>{esc(desc)}</description>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>", ""]
    return "\n".join(parts)


def render_all(check=False):
    cfg = config()
    items = load_json(LISTINGS, [])
    updated = last_change(items, load_json(ARCHIVE, []))
    targets = {
        README: replace_block(README.read_text(encoding="utf-8"), "LISTINGS", render_listings_block(items, updated, cfg.get("default_ttl_days", 45))),
        EMPLOYERS: replace_block(EMPLOYERS.read_text(encoding="utf-8"), "PRICES", render_prices_block(cfg)),
        FEED: render_feed(items, cfg),
        META: json.dumps({
            "brand": cfg.get("brand"),
            "tagline": cfg.get("tagline"),
            "repo": cfg.get("repo"),
            "telegram_channel": cfg.get("telegram_channel"),
            "updated": updated.isoformat(),
            "active": len(items),
        }, ensure_ascii=False, indent=2) + "\n",
    }
    stale = []
    for path, content in targets.items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == content:
            continue
        stale.append(path.relative_to(ROOT).as_posix())
        if not check:
            path.write_text(content, encoding="utf-8")
    if check and stale:
        print("Нужно запустить `python3 scripts/board.py render`, устарели: " + ", ".join(stale))
        return 1
    return 0


# --------------------------------------------------------------------------- bot texts

def payment_text(issue_number, cfg):
    p = cfg.get("prices", {})
    lines = [f"💳 **Вы выбрали Featured** — {money(p.get('featured', 0), cfg)} за "
             f"{cfg.get('featured_days', 30)} дней."]
    if cfg.get("launch_promo"):
        lines.append("Сейчас идёт запуск: первые 10 работодателей получают Featured **бесплатно** — "
                     "модератор отметит заявку после проверки, платить ничего не нужно.")
    elif cfg.get("kaspi_pay_link"):
        lines.append(f"Оплатить можно через Kaspi: {cfg['kaspi_pay_link']} — в сообщении к платежу "
                     f"укажите `JuniorJol #{issue_number}`. После оплаты модератор включит продвижение.")
    else:
        lines.append("Модератор пришлёт ссылку на оплату Kaspi Pay в этой заявке.")
    if cfg.get("contact_email"):
        lines.append(f"Нужен счёт на ТОО/ИП или закрывающие документы — напишите на {cfg['contact_email']}.")
    return "\n\n".join(lines)


def intake_comment(issue, cfg, on=None):
    listing, errors, wants_featured = build_listing(issue, cfg, on)
    parts = [BOT_MARK]
    if errors:
        parts.append("### ✋ Нужно поправить заявку")
        parts += [f"- {no_mention(e)}" for e in errors]
        parts.append("\nОтредактируйте описание issue (⋯ → **Edit**) — бот перепроверит заявку автоматически.")
        return "\n".join(parts), "needs-fix"
    parts.append("### ✅ Заявка заполнена правильно")
    parts.append("Так вакансия будет выглядеть в списке:\n")
    parts.append(TABLE_HEAD)
    parts.append(no_mention(table_row(listing)))
    expires = (f"до дедлайна ({short_date(listing['deadline'])})" if listing["deadline"]
               else f"{cfg.get('default_ttl_days', 45)} дней")
    parts.append(f"\nПосле публикации вакансия будет в списке {expires}. "
                 "Модератор проверит заявку в течение 1–2 рабочих дней.")
    if wants_featured:
        parts.append("\n" + payment_text(issue["number"], cfg))
    return "\n".join(parts), "ready-for-review"


# --------------------------------------------------------------------------- actions

def write_out(result, comment=None):
    OUT.mkdir(exist_ok=True)
    save_json(OUT / "result.json", result)
    if comment is not None:
        (OUT / "comment.md").write_text(comment, encoding="utf-8")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            for k, v in result.items():
                fh.write(f"{k}={','.join(v) if isinstance(v, list) else str(v).lower() if isinstance(v, bool) else v}\n")


def cmd_intake(event_path):
    issue = load_json(Path(event_path), {})["issue"]
    comment, label = intake_comment(issue, config())
    write_out({"label": label}, comment)
    print(comment)
    return 0


def publish(issue, cfg, on):
    listing, errors, _ = build_listing(issue, cfg, on)
    if errors:
        text = "\n".join(["❌ Не удалось опубликовать — в заявке есть ошибки:"] + [f"- {e}" for e in errors])
        return {"status": "error", "changed": False, "close": False, "telegram": []}, text

    items = load_json(LISTINGS, [])
    dupe = next((i for i in items if i["url"] == listing["url"] and i["issue"] != listing["issue"]), None)
    if dupe:
        return ({"status": "error", "changed": False, "close": False, "telegram": []},
                f"❌ Эта вакансия уже опубликована (заявка #{dupe['issue']}).")

    old = next((i for i in items if i["issue"] == listing["issue"]), None)
    if old:  # повторная публикация после правок: сохраняем дату и продвижение
        listing["added"] = old["added"]
        listing["featured_until"] = old.get("featured_until")
        if not listing["deadline"]:
            listing["expires"] = old["expires"]
        items = [i for i in items if i["issue"] != listing["issue"]]
    if "paid" in issue_labels(issue) and not is_featured(listing, on):
        listing["featured_until"] = (on + dt.timedelta(days=cfg.get("featured_days", 30))).isoformat()
    items.append(listing)
    save_json(LISTINGS, items)

    text = f"🎉 Вакансия опубликована: **{listing['title']}** — {listing['company']}."
    if listing["featured_until"]:
        text += f"\n\n⭐ Featured до {short_date(listing['featured_until'])}."
    text += ("\n\nКогда набор закроется, создайте заявку «Закрыть вакансию» или просто напишите "
             "здесь — модератор снимет объявление. Спасибо!")
    return ({"status": "published", "changed": True, "close": True,
             "telegram": [] if old else [listing["id"]]}, text)


def feature(issue, cfg, on):
    items = load_json(LISTINGS, [])
    item = next((i for i in items if i["issue"] == issue["number"]), None)
    if not item:
        # Метку paid поставили до публикации — включим продвижение при публикации.
        return {"status": "noop", "changed": False, "close": False, "telegram": []}, None
    item["featured_until"] = (on + dt.timedelta(days=cfg.get("featured_days", 30))).isoformat()
    save_json(LISTINGS, items)
    return ({"status": "featured", "changed": True, "close": False, "telegram": []},
            f"⭐ Оплата получена, вакансия закреплена вверху списка до {short_date(item['featured_until'])}. Спасибо!")


def close(issue, on):
    f = parse_form(issue.get("body"), CLOSE_FIELDS)
    target = one_line(f["target"])
    items = load_json(LISTINGS, [])
    num = re.fullmatch(r"#?(\d+)", target)
    item = None
    if num:
        item = next((i for i in items if i["issue"] == int(num.group(1))), None)
    elif target:
        url, _ = check_url(target)
        item = next((i for i in items if i["url"] in (url, target)), None)
    if not item:
        return ({"status": "error", "changed": False, "close": False, "telegram": []},
                "❌ Не нашёл активную вакансию по номеру заявки или ссылке. Проверьте поле и поставьте метку ещё раз.")
    items = [i for i in items if i is not item]
    archive = load_json(ARCHIVE, [])
    archive.append({**item, "closed": on.isoformat(), "reason": one_line(f["reason"]) or "Закрыта"})
    save_json(LISTINGS, items)
    save_json(ARCHIVE, archive)
    return ({"status": "closed", "changed": True, "close": True, "telegram": []},
            f"✅ Вакансия «{item['title']}» ({item['company']}) снята с публикации.")


def cmd_apply(event_path):
    event = load_json(Path(event_path), {})
    issue, label = event["issue"], event.get("label", {}).get("name")
    labels, cfg, on = issue_labels(issue), config(), today()
    if label == "approved" and "close-listing" in labels:
        result, text = close(issue, on)
    elif label == "approved" and "new-listing" in labels:
        result, text = publish(issue, cfg, on)
    elif label == "paid" and "new-listing" in labels:
        result, text = feature(issue, cfg, on)
    else:
        result, text = {"status": "noop", "changed": False, "close": False, "telegram": []}, None
    if result["changed"]:
        render_all()
    write_out(result, text)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_expire():
    on = today()
    items, archive = load_json(LISTINGS, []), load_json(ARCHIVE, [])
    keep = []
    for i in items:
        if dt.date.fromisoformat(i["expires"]) < on:
            archive.append({**i, "closed": on.isoformat(), "reason": "Истёк срок"})
            continue
        if i.get("featured_until") and dt.date.fromisoformat(i["featured_until"]) < on:
            i["featured_until"] = None
        keep.append(i)
    removed = len(items) - len(keep)
    save_json(LISTINGS, keep)
    if removed:
        save_json(ARCHIVE, archive)
    render_all()
    print(f"Снято просроченных: {removed}, активных: {len(keep)}")
    return 0


# --------------------------------------------------------------------------- telegram

def tg_tag(text):
    return "#" + re.sub(r"[^\w]+", "_", text.lower()).strip("_")


def telegram_post(item, cfg):
    e = html.escape
    lines = [
        f"{'⭐ ' if is_featured(item) else ''}<b>{e(item['title'])}</b> — {e(item['company'])}",
        f"📍 {e(where(item))}",
        f"💼 {e(item['type'])} · {e(item['direction'])}",
        f"💰 {e(item['paid'])}" + (f" ({e(item['salary'])})" if item.get("salary") else ""),
    ]
    if item.get("deadline"):
        lines.append(f"⏳ до {short_date(item['deadline'])}")
    lines.append(f'👉 <a href="{e(item["url"])}">Откликнуться</a>')
    tags = [tg_tag(TYPE_SHORT.get(item["type"], item["type"])), tg_tag(item["direction"])]
    if item["city"] not in ("Другой город", "Любой (удалённо)"):
        tags.append(tg_tag(item["city"]))
    if item["format"] == "Удалённо":
        tags.append("#удалённо")
    lines += ["", " ".join(tags)]
    return "\n".join(lines)


def telegram_digest(items, cfg, on):
    items = sorted_listings(items)[:20]
    if not items:
        return None
    e = html.escape
    lines = [f"🗓 <b>{e(cfg.get('brand', 'JuniorJol'))}: вакансии недели</b> ({on.strftime('%d.%m')})", ""]
    for i in items:
        star = "⭐ " if is_featured(i) else "• "
        lines.append(f'{star}<a href="{e(i["url"])}">{e(i["title"])}</a> — {e(i["company"])}, {e(where(i))}')
    lines += ["", "Разместить вакансию бесплатно: Issues → «Новая вакансия» в репозитории."]
    return "\n".join(lines)


def tg_send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram не настроен (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — пропускаю.")
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20) as r:
        r.read()


def cmd_telegram(ids):
    cfg, wanted = config(), {i for i in ids.split(",") if i}
    for item in load_json(LISTINGS, []):
        if item["id"] in wanted:
            tg_send(telegram_post(item, cfg))
    return 0


def cmd_digest():
    text = telegram_digest(load_json(LISTINGS, []), config(), today())
    if text:
        tg_send(text)
    return 0


# --------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("intake", "apply"):
        sub.add_parser(name).add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH"))
    sub.add_parser("expire")
    sub.add_parser("render").add_argument("--check", action="store_true")
    sub.add_parser("telegram").add_argument("--ids", default="")
    sub.add_parser("telegram-digest")
    args = ap.parse_args(argv)
    if args.cmd == "intake":
        return cmd_intake(args.event)
    if args.cmd == "apply":
        return cmd_apply(args.event)
    if args.cmd == "expire":
        return cmd_expire()
    if args.cmd == "render":
        return render_all(check=args.check)
    if args.cmd == "telegram":
        return cmd_telegram(args.ids)
    return cmd_digest()


if __name__ == "__main__":
    sys.exit(main())
