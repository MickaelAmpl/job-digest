"""Task 1: the CV builds offline with the bundled PT Serif, on one A4 page, under 2 MB."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.source import build  # noqa: E402

BASE = ROOT / "cv" / "source" / "content" / "en.json"


class OfflineFont(unittest.TestCase):
    def test_html_has_no_remote_fonts(self):
        import json
        page = build.render(json.loads(BASE.read_text(encoding="utf-8")))
        self.assertNotIn("fonts.googleapis", page)
        self.assertIn("@font-face", page)
        build.check_offline(page)  # must not raise

    def test_offline_guard_rejects_remote_font(self):
        bad = '<link href="https://fonts.googleapis.com/css2?family=PT+Serif" rel="stylesheet">'
        with self.assertRaises(RuntimeError):
            build.check_offline(bad)

    def test_bundled_fonts_present(self):
        for style in ("Regular", "Bold", "Italic", "BoldItalic"):
            self.assertTrue((build.FONTS / f"PT_Serif-Web-{style}.ttf").is_file(), style)
        self.assertTrue((build.FONTS / "OFL.txt").is_file())

    def test_chrome_from_env(self):
        old = os.environ.get("CHROME_BIN")
        os.environ["CHROME_BIN"] = "/usr/bin/google-chrome"
        try:
            self.assertEqual(build.chrome_bin(), "/usr/bin/google-chrome")
        finally:
            if old is None:
                os.environ.pop("CHROME_BIN")
            else:
                os.environ["CHROME_BIN"] = old


@unittest.skipUnless(Path(build.chrome_bin()).exists() if hasattr(build, "chrome_bin") else False,
                     "Chrome not installed here")
class BuildOnePage(unittest.TestCase):
    def test_build_one_page_offline(self):
        os.environ["CV_OFFLINE"] = "1"
        try:
            with tempfile.TemporaryDirectory() as d:
                pdf = build.build(BASE, Path(d) / "cv.pdf", html_dir=d)
                data = Path(pdf).read_bytes()
                self.assertIn(b"/Count 1", data)
                self.assertLess(len(data), 2 * 1024 * 1024)
                self.assertIn(b"PTSerif-Regular", data)  # bundled font embedded, not a fallback serif
        finally:
            os.environ.pop("CV_OFFLINE", None)


if __name__ == "__main__":
    unittest.main()
