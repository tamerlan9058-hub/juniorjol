import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

TODAY = "2026-10-05"


def form_body(**over):
    values = {
        "Компания": "Example Tech",
        "Позиция": "Стажёр Python-разработчик",
        "Тип": "Стажировка",
        "Направление": "Backend",
        "Город": "Алматы",
        "Формат работы": "Гибрид",
        "Оплата": "Оплачиваемая",
        "Зарплата / стипендия": "_No response_",
        "Ссылка на вакансию": "https://example.kz/careers/python-intern",
        "Дедлайн отклика": "_No response_",
        "Продвижение": "Бесплатное размещение",
        "Подтверждение": "- [X] Вакансия реальная, ссылка ведёт на официальный источник\n"
                         "- [X] В объявлении нет требований к полу, возрасту, внешности и т. п.",
    }
    values.update(over)
    return "\n\n".join(f"### {k}\n\n{v}" for k, v in values.items())


def issue(number=7, labels=("new-listing",), **over):
    return {"number": number, "body": form_body(**over), "labels": [{"name": n} for n in labels]}


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for rel in ("README.md", "config.json", "docs/employers.md"):
            (self.tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, self.tmp / rel)
        (self.tmp / "data").mkdir()
        (self.tmp / "data" / "listings.json").write_text("[]", encoding="utf-8")
        (self.tmp / "data" / "archive.json").write_text("[]", encoding="utf-8")
        cfg = json.loads((self.tmp / "config.json").read_text(encoding="utf-8"))
        cfg.update({"launch_promo": False, "kaspi_pay_link": "https://pay.kaspi.kz/pay/test",
                    "contact_email": "hello@example.kz"})
        (self.tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        os.environ["BOARD_ROOT"] = str(self.tmp)
        os.environ["BOARD_TODAY"] = TODAY
        os.environ.pop("GITHUB_OUTPUT", None)
        import board
        self.b = importlib.reload(board)
        self.cfg = self.b.config()

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("BOARD_ROOT", None)
        os.environ.pop("BOARD_TODAY", None)

    # ---------------------------------------------------------------- helpers

    def listings(self):
        return json.loads((self.tmp / "data" / "listings.json").read_text(encoding="utf-8"))

    def archive(self):
        return json.loads((self.tmp / "data" / "archive.json").read_text(encoding="utf-8"))

    def apply(self, iss, label):
        event = self.tmp / "event.json"
        event.write_text(json.dumps({"issue": iss, "label": {"name": label}}, ensure_ascii=False), encoding="utf-8")
        self.b.cmd_apply(str(event))
        result = json.loads((self.tmp / "out" / "result.json").read_text(encoding="utf-8"))
        comment = (self.tmp / "out" / "comment.md")
        return result, comment.read_text(encoding="utf-8") if comment.exists() else ""

    def errors(self, **over):
        return self.b.build_listing(issue(**over), self.cfg)[1]

    # ---------------------------------------------------------------- validation

    def test_valid_listing(self):
        listing, errors, featured = self.b.build_listing(issue(), self.cfg)
        self.assertEqual(errors, [])
        self.assertFalse(featured)
        self.assertEqual(listing["id"], "jj-7")
        self.assertEqual(listing["company"], "Example Tech")
        self.assertEqual(listing["salary"], "")
        self.assertEqual(listing["expires"], "2026-11-19")  # 45 дней

    def test_deadline_sets_expiry_and_accepts_dotted_format(self):
        listing, errors, _ = self.b.build_listing(issue(**{"Дедлайн отклика": "30.11.2026"}), self.cfg)
        self.assertEqual(errors, [])
        self.assertEqual(listing["deadline"], "2026-11-30")
        self.assertEqual(listing["expires"], "2026-11-30")

    def test_required_and_choices(self):
        errs = self.errors(**{"Компания": "_No response_", "Город": "Москва"})
        self.assertTrue(any("Компания" in e for e in errs))
        self.assertTrue(any("Город" in e for e in errs))

    def test_bad_urls(self):
        for url in ("http://example.kz/job", "javascript:alert(1)", "https://example.kz/a b", "https://localhost"):
            self.assertTrue(any("https" in e or "Ссылка" in e for e in self.errors(**{"Ссылка на вакансию": url})), url)

    def test_url_parentheses_are_encoded(self):
        listing, errors, _ = self.b.build_listing(issue(**{"Ссылка на вакансию": "https://x.kz/job_(1)"}), self.cfg)
        self.assertEqual(errors, [])
        self.assertEqual(listing["url"], "https://x.kz/job_%281%29")

    def test_deadline_rules(self):
        self.assertTrue(self.errors(**{"Дедлайн отклика": "2026-10-01"}))
        self.assertTrue(self.errors(**{"Дедлайн отклика": "завтра"}))
        self.assertTrue(self.errors(**{"Дедлайн отклика": "2028-01-01"}))

    def test_confirmations_required(self):
        errs = self.errors(**{"Подтверждение": "- [X] Вакансия реальная\n- [ ] Без дискриминации"})
        self.assertTrue(any("Подтверждение" in e for e in errs))

    def test_discrimination_filter(self):
        for text in ("Стажёр (девушки)", "Junior до 25 лет", "Менеджер, приятная внешность"):
            self.assertTrue(any("Трудовым кодексом" in e for e in self.errors(**{"Позиция": text})), text)

    def test_length_limit(self):
        self.assertTrue(self.errors(**{"Компания": "A" * 81}))

    # ---------------------------------------------------------------- rendering

    def test_markdown_is_escaped(self):
        listing, _, _ = self.b.build_listing(issue(**{"Компания": "Evil | [x](https://bad) <b>"}), self.cfg)
        row = self.b.table_row(listing)
        self.assertNotIn("[x](https://bad)", row)
        self.assertNotIn("<b>", row)
        self.assertEqual(len(re.findall(r"(?<!\\)\|", row)), 9)  # 8 колонок, разделители не поехали
        self.assertIn(r"Evil \| \[x\]", row)

    def test_intake_ok_and_featured_payment(self):
        comment, label = self.b.intake_comment(issue(**{"Продвижение": self.b.FEATURED_OPTION}), self.cfg)
        self.assertEqual(label, "ready-for-review")
        self.assertIn(self.b.BOT_MARK, comment)
        self.assertIn("9 900 ₸", comment)
        self.assertIn("https://pay.kaspi.kz/pay/test", comment)
        self.assertIn("JuniorJol #7", comment)

    def test_intake_launch_promo(self):
        self.cfg["launch_promo"] = True
        comment, _ = self.b.intake_comment(issue(**{"Продвижение": self.b.FEATURED_OPTION}), self.cfg)
        self.assertIn("бесплатно", comment)
        self.assertNotIn("pay.kaspi.kz", comment)

    def test_intake_errors_and_no_mentions(self):
        comment, label = self.b.intake_comment(issue(**{"Ссылка на вакансию": "@octocat"}), self.cfg)
        self.assertEqual(label, "needs-fix")
        self.assertNotIn("@octocat", comment)

    # ---------------------------------------------------------------- lifecycle

    def test_publish_renders_everything(self):
        result, comment = self.apply(issue(), "approved")
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["telegram"], ["jj-7"])
        self.assertTrue(result["close"])
        self.assertIn("опубликована", comment)
        readme = (self.tmp / "README.md").read_text(encoding="utf-8")
        self.assertIn("**Example Tech**", readme)
        self.assertIn("**Активных: 1**", readme)
        self.assertIn("обновлено 5 октября 2026", readme)
        ET.fromstring((self.tmp / "data" / "feed.xml").read_text(encoding="utf-8"))
        meta = json.loads((self.tmp / "data" / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["active"], 1)
        prices = (self.tmp / "docs" / "employers.md").read_text(encoding="utf-8")
        self.assertIn("9 900 ₸", prices)
        self.assertEqual(self.b.render_all(check=True), 0)

    def test_republish_keeps_added_and_does_not_repost(self):
        self.apply(issue(), "approved")
        os.environ["BOARD_TODAY"] = "2026-10-10"
        result, _ = self.apply(issue(**{"Позиция": "Стажёр Go-разработчик"}), "approved")
        items = self.listings()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Стажёр Go-разработчик")
        self.assertEqual(items[0]["added"], TODAY)
        self.assertEqual(result["telegram"], [])

    def test_duplicate_url_rejected(self):
        self.apply(issue(7), "approved")
        result, comment = self.apply(issue(8), "approved")
        self.assertEqual(result["status"], "error")
        self.assertIn("#7", comment)
        self.assertEqual(len(self.listings()), 1)

    def test_publish_with_errors(self):
        result, comment = self.apply(issue(**{"Ссылка на вакансию": "http://x.kz"}), "approved")
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["changed"])
        self.assertEqual(self.listings(), [])

    def test_paid_after_publish_features_and_sorts_first(self):
        self.apply(issue(7), "approved")
        os.environ["BOARD_TODAY"] = "2026-10-06"
        self.apply(issue(8, **{"Ссылка на вакансию": "https://other.kz/job", "Компания": "Other"}), "approved")
        result, comment = self.apply(issue(7, labels=("new-listing", "published", "paid")), "paid")
        self.assertEqual(result["status"], "featured")
        self.assertIn("05.11.2026", comment)
        readme = (self.tmp / "README.md").read_text(encoding="utf-8")
        rows = [l for l in readme.splitlines() if l.startswith("| ⭐ |") or l.startswith("|  | **")]
        self.assertTrue(rows[0].startswith("| ⭐ | **Example Tech**"), rows)

    def test_paid_before_publish(self):
        result, _ = self.apply(issue(labels=("new-listing", "paid")), "paid")
        self.assertEqual(result["status"], "noop")
        self.apply(issue(labels=("new-listing", "paid", "approved")), "approved")
        self.assertEqual(self.listings()[0]["featured_until"], "2026-11-04")

    def test_close_by_number_and_url(self):
        self.apply(issue(7), "approved")
        self.apply(issue(8, **{"Ссылка на вакансию": "https://other.kz/job"}), "approved")
        close = lambda n, target: {"number": n, "labels": [{"name": "close-listing"}],
                                   "body": f"### Номер заявки или ссылка на вакансию\n\n{target}\n\n### Причина\n\nНабор закрыт"}
        result, _ = self.apply(close(20, "#7"), "approved")
        self.assertEqual(result["status"], "closed")
        result, _ = self.apply(close(21, "https://other.kz/job"), "approved")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(self.listings(), [])
        self.assertEqual([a["reason"] for a in self.archive()], ["Набор закрыт", "Набор закрыт"])
        result, _ = self.apply(close(22, "99"), "approved")
        self.assertEqual(result["status"], "error")

    def test_unrelated_label_is_noop(self):
        result, _ = self.apply(issue(labels=("sponsor-request",)), "approved")
        self.assertEqual(result["status"], "noop")

    def test_expire(self):
        self.apply(issue(7, labels=("new-listing", "paid")), "approved")
        self.apply(issue(8, **{"Ссылка на вакансию": "https://other.kz/job", "Дедлайн отклика": "2026-10-20"}), "approved")
        os.environ["BOARD_TODAY"] = "2026-11-05"
        self.b.cmd_expire()
        items = self.listings()
        self.assertEqual([i["id"] for i in items], ["jj-7"])
        self.assertIsNone(items[0]["featured_until"])
        self.assertEqual(self.archive()[0]["reason"], "Истёк срок")
        readme = (self.tmp / "README.md").read_text(encoding="utf-8")
        self.assertIn("обновлено 5 ноября 2026", readme)

    def test_empty_state(self):
        self.b.render_all()
        readme = (self.tmp / "README.md").read_text(encoding="utf-8")
        self.assertIn("Сейчас нет активных вакансий", readme)

    # ---------------------------------------------------------------- telegram

    def test_telegram_post_escapes_html(self):
        listing, _, _ = self.b.build_listing(issue(**{"Компания": "<script>A&B</script>"}), self.cfg)
        post = self.b.telegram_post(listing, self.cfg)
        self.assertNotIn("<script>", post)
        self.assertIn("&lt;script&gt;A&amp;B", post)
        self.assertIn("#стажировка #backend #алматы", post)

    def test_digest(self):
        self.assertIsNone(self.b.telegram_digest([], self.cfg, self.b.today()))
        listing, _, _ = self.b.build_listing(issue(), self.cfg)
        self.assertIn("Example Tech", self.b.telegram_digest([listing], self.cfg, self.b.today()))


class FormsMatchCode(unittest.TestCase):
    """Подписи и варианты в Issue Forms должны совпадать с тем, что ждёт скрипт."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(REPO / "scripts"))
        import board
        cls.b = board

    def form(self, name):
        return (REPO / ".github" / "ISSUE_TEMPLATE" / name).read_text(encoding="utf-8")

    def labels(self, text):
        return re.findall(r"^\s+label:\s*(.+?)\s*$", text, re.M)

    def test_new_listing_form(self):
        text = self.form("new-listing.yml")
        labels = self.labels(text)
        for label in self.b.FIELDS:
            self.assertIn(label, labels)
        for options in self.b.CHOICES.values():
            for option in options:
                self.assertRegex(text, rf"(?m)^\s+- {re.escape(option)}\s*$")
        self.assertEqual(text.count("required: true"), len(self.b.REQUIRED) + 2)

    def test_close_form(self):
        text = self.form("close-listing.yml")
        for label in self.b.CLOSE_FIELDS:
            self.assertIn(label, self.labels(text))
        for option in self.b.CLOSE_REASONS:
            self.assertRegex(text, rf"(?m)^\s+- {re.escape(option)}\s*$")


if __name__ == "__main__":
    unittest.main()
