"""Task 5: PDF checks with poppler faked (this Mac has none; the server runs the real tools)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cv.tailor import pdfcheck  # noqa: E402

PDFFONTS = """name                                 type              encoding         emb sub uni object ID
------------------------------------ ----------------- ---------------- --- --- --- ---------
AAAAAA+PTSerif-Bold                  CID TrueType      Identity-H       yes yes yes      5  0
BAAAAA+PTSerif-Regular               CID TrueType      Identity-H       yes yes yes     10  0
"""
TYPE3 = PDFFONTS + "T3Font_0                             Type 3            Custom           yes no  no      15  0\n"
EXPECTED = "Alex Morgan\nSummary\nBuilt the go-to-market plan with marketing\nExperience\nReached 120,000+ users"
GOOD_TEXT = "ALEX MORGAN\nSUMMARY\nBuilt the go-to-market plan\nwith marketing\nEXPERIENCE\nReached 120,000+ users\n\f"


def fake(info="Pages:          1\n", fonts=PDFFONTS, text=GOOD_TEXT):
    calls = []

    def runner(cmd):
        calls.append(cmd[0])
        return {"pdfinfo": info, "pdffonts": fonts, "pdftotext": text}[cmd[0]]
    runner.calls = calls
    return runner


class Check(unittest.TestCase):
    def setUp(self):
        self.saved = pdfcheck.RUN
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "cv.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 /Type /Pages /Count 1")

    def tearDown(self):
        pdfcheck.RUN = self.saved
        self.tmp.cleanup()

    def test_clean_pdf(self):
        pdfcheck.RUN = fake()
        r = pdfcheck.check(self.pdf, EXPECTED)
        self.assertEqual(r["pages"], 1)
        self.assertEqual(r["issues"], [])
        self.assertIn("go-to-market", r["text"])

    def test_two_pages(self):
        pdfcheck.RUN = fake(info="Title: x\nPages:          2\n")
        self.assertEqual(pdfcheck.check(self.pdf)["pages"], 2)

    def test_type3_and_no_tounicode(self):
        pdfcheck.RUN = fake(fonts=TYPE3)
        issues = pdfcheck.check(self.pdf)["issues"]
        self.assertIn("Type3 font: T3Font_0", issues)
        self.assertIn("font without ToUnicode: T3Font_0", issues)

    def test_ligature(self):
        pdfcheck.RUN = fake(text="Proﬁle of the ﬂow")
        self.assertTrue(any("ligature" in i for i in pdfcheck.check(self.pdf)["issues"]))

    def test_split_hyphen(self):
        pdfcheck.RUN = fake(text=GOOD_TEXT.replace("go-to-market", "go-to-\nmarket"))
        issues = pdfcheck.check(self.pdf, EXPECTED)["issues"]
        self.assertIn("hyphenated word split or missing: go-to-market", issues)

    def test_order_and_missing(self):
        swapped = "ALEX MORGAN\nEXPERIENCE\nReached 120,000+ users\nSUMMARY\nBuilt the go-to-market plan"
        pdfcheck.RUN = fake(text=swapped)
        issues = pdfcheck.check(self.pdf, EXPECTED)["issues"]
        self.assertTrue(any("out of order" in i for i in issues), issues)
        pdfcheck.RUN = fake(text="ALEX MORGAN")
        issues = pdfcheck.check(self.pdf, EXPECTED)["issues"]
        self.assertTrue(any("missing" in i for i in issues))

    def test_empty_text(self):
        pdfcheck.RUN = fake(text="  \n")
        self.assertIn("no extractable text", pdfcheck.check(self.pdf)["issues"])

    def test_tool_missing(self):
        def runner(cmd):
            raise pdfcheck.ToolMissing("pdfinfo not installed")
        pdfcheck.RUN = runner
        with self.assertRaises(pdfcheck.ToolMissing):
            pdfcheck.check(self.pdf)

    def test_default_runner_maps_missing_binary(self):
        saved = pdfcheck.subprocess.run

        def boom(*a, **k):
            raise FileNotFoundError("pdfinfo")
        try:
            pdfcheck.subprocess.run = boom
            with self.assertRaises(pdfcheck.ToolMissing):
                pdfcheck.run_tool(["pdfinfo", "x.pdf"])
        finally:
            pdfcheck.subprocess.run = saved


class PageCount(unittest.TestCase):
    def test_from_bytes(self):
        self.assertEqual(pdfcheck.page_count(b"<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>"), 2)
        self.assertEqual(pdfcheck.page_count(b"<< /Count 1 /Kids [3 0 R] /Type /Pages >>"), 1)

    def test_real_approved_pdf(self):
        pdf = ROOT / "cv" / "out" / "Alex_Morgan_CV_EN.pdf"
        self.assertEqual(pdfcheck.page_count(pdf.read_bytes()), 1)


if __name__ == "__main__":
    unittest.main()
