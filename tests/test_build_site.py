import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


class BuildSiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "site", self.tmp / "site")
        shutil.copytree(REPO / "data", self.tmp / "data")
        cfg = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
        cfg.update(repo="Owner/board", google_site_verification="g-123")
        (self.tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        evil = {"id": "jj-1", "issue": 1, "company": "<script>x</script>", "title": "A & B", "type": "Стажировка",
                "direction": "QA", "city": "Алматы", "format": "Офис", "paid": "Не указано", "salary": "",
                "url": "https://x.kz/1", "deadline": None, "added": "2026-10-01", "expires": "2026-11-15",
                "featured_until": None}
        (self.tmp / "data" / "listings.json").write_text(json.dumps([evil], ensure_ascii=False), encoding="utf-8")
        os.environ["BOARD_ROOT"] = str(self.tmp)
        os.environ["BOARD_TODAY"] = "2026-10-05"
        import board
        import build_site
        importlib.reload(board)
        self.bs = importlib.reload(build_site)

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("BOARD_ROOT", None)
        os.environ.pop("BOARD_TODAY", None)

    def test_build(self):
        out = self.tmp / "_site"
        self.bs.build(out)
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page)
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("A &amp; B", page)
        self.assertIn('href="https://owner.github.io/board/"', page)
        self.assertIn('name="google-site-verification" content="g-123"', page)
        self.assertNotIn("<!-- SEO -->", page)
        self.assertIn("https://owner.github.io/board/", (out / "sitemap.xml").read_text(encoding="utf-8"))
        for name in ("listings.json", "feed.xml", "meta.json"):
            self.assertTrue((out / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
