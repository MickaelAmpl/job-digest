"""scripts/metrics.py (stage 7 step 0): exact numbers on a temp DB and temp logs, missing log days, junk rows,
read-only DB, defensive log parsing. The fixture numbers were counted by hand; no network, no real data."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import metrics  # noqa: E402

SCHEMA = """create table jobs (
  id text primary key, key text, company text, title text, url text, source text,
  first_seen text, sent_at text, score integer, verdict text, verdict_at text, data text,
  closed integer default 0, closed_by text, rule text, listed integer default 0,
  has_desc integer default 1, enrich_tries integer default 0)"""

# id, company, title, source, first_seen, sent_at, score, rule, has_desc, closed, closed_by, listed,
# verdict, verdict_at, data
JOBS = [
    # 10.10: one kept company job with a description, one LinkedIn closed and old, one not kept, one junk footer
    ("a1", "Acme", "Junior PM", "company", "2026-10-10T06:31:00", "2026-10-10T07:30:00", 62, "keep", 1, 0, None, 0,
     None, None, '{"posted": "2026-10-08"}'),
    ("b1", "Beta", "Product analyst", "linkedin", "2026-10-10T06:31:00", "2026-10-10T07:30:00", 55, "keep", 0, 1,
     "feed", 1, None, None, '{"posted": "2026-08-01"}'),
    ("c1", "Gamma", "Buchhalter", "stepstone", "2026-10-10T06:31:00", None, None, "german_required", 1, 0, None, 0,
     "interested", "2026-10-10T18:00:00", "{}"),
    ("j1", "New jobs from your other alerts", "See all jobs on LinkedIn: https://x", "linkedin",
     "2026-10-10T06:31:00", None, 50, "keep", 0, 0, None, 0, None, None, "{}"),
    # 11.10: a paraphrase job sent and marked, a closed one, a senior one, two more junk rows
    ("d1", "Delta", "Praktikum PM", "company", "2026-10-11T06:31:00", "2026-10-11T07:30:00", 40, "keep", 2, 0, None,
     0, "not_interested", "2026-10-11T09:00:00", '{"posted": "2026-10-01"}'),
    ("e1", "Epsilon", "Junior Analyst", "linkedin", "2026-10-11T06:31:00", None, 51, "keep", 1, 1, "enrich", 0,
     None, None, "{}"),
    ("f1", "Zeta", "Sales", "adzuna", "2026-10-11T06:31:00", None, 20, "senior", 0, 0, None, 0, None, None, "{}"),
    ("j2", "Eta", "Edit alert https://y", "linkedin", "2026-10-11T06:31:00", None, 58, "keep", 0, 0, None, 0,
     None, None, "{}"),
    ("j3", "Iota", "Theta PM", "adzuna", "2026-10-11T06:31:00", None, 70, "keep", 1, 0, "junk", 0, None, None, "{}"),
]

LOG_10 = """lookback: email 2 days, adzuna 2 days
raw per source: {'linkedin': 35, 'stepstone': 66, 'adzuna': 20}
jobs in Austria: 1501 from 74 companies
skipped (no adapter / no feed): 119
failed: ['Jobiqo: HTTPError']
seen 1521, new 719, rule backfilled 0, feeds answered 71, closed by feed 47, reopened 48
without description: 15, found originals: 0, not found: 15, closed: 1, failed (not counted as tries): 0, skipped for time: 0
"""

LOG_11_NEW = """raw per source: {'company': 3, 'linkedin': 2}
failed: []
skipped (no adapter / no feed): 119
seen 1505, new 17, rule backfilled 0, feeds answered 73, closed by feed 2, reopened 0
without description: 10, found originals: 7 (page 3, direct 2, paraphrase 2), not found: 3, closed: 0, failed (not counted as tries): 0, skipped for time: 0
"""

LOG_11 = """raw per source: {'company': 3, 'linkedin': 2}
failed: []
skipped (no adapter / no feed): 119
seen 1505, new 17, rule backfilled 0, feeds answered 73, closed by feed 2, reopened 0
without description: 10, found originals: 3, found page: 2, found paraphrase: 1, not found: 7, closed: 0, failed (not counted as tries): 0, skipped for time: 0
"""


def make_db(path, jobs):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    names = ("id", "company", "title", "source", "first_seen", "sent_at", "score", "rule", "has_desc", "closed",
             "closed_by", "listed", "verdict", "verdict_at", "data")
    for job in jobs:
        rec = dict(zip(names, job))
        rec["key"] = rec["id"]
        cols = ", ".join(rec)
        marks = ", ".join("?" for _ in rec)
        con.execute(f"insert into jobs ({cols}) values ({marks})", tuple(rec.values()))
    con.commit()
    con.close()


def row(**kw):
    """A sqlite3.Row with the given columns, as the metrics code receives them."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    cols = ", ".join(f"? as {k}" for k in kw)
    r = con.execute(f"select {cols}", tuple(kw.values())).fetchone()
    con.close()
    return r


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Path(self.tmp) / "state.db"
        self.logs = Path(self.tmp) / "logs"
        self.logs.mkdir()
        self.root = Path(self.tmp) / "root"
        self.root.mkdir()
        (self.root / "companies.json").write_text(json.dumps([
            {"id": "a", "feed_ok": True, "feed_url": "https://x/a"},
            {"id": "b", "feed_ok": True, "feed_url": ""},
            {"id": "c", "feed_ok": False, "feed_url": "https://x/c"},
            {"id": "d", "feed_ok": True, "feed_url": "https://x/d"},
        ]), encoding="utf-8")
        make_db(str(self.db), JOBS)
        (self.logs / "2026-10-10.log").write_text(LOG_10, encoding="utf-8")
        (self.logs / "2026-10-11.log").write_text(LOG_11, encoding="utf-8")

    def run_main(self, *args):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JOBDIGEST_DB": str(self.db)}), \
                mock.patch.object(metrics, "ROOT", self.root), contextlib.redirect_stdout(out):
            code = metrics.main(list(args) + ["--logs", str(self.logs)])
        self.assertEqual(code, 0)
        return out.getvalue()

    @staticmethod
    def line(out, label):
        found = [ln for ln in out.splitlines() if ln.startswith(f"| {label} |")]
        assert len(found) == 1, (label, found)
        return found[0]


class KnownNumbers(Fixture):
    def test_db_rows_exact(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11", "--md")
        expect = {
            "Новых вакансий (по дню first_seen)": "| 4 | 5 | 9 |",
            "Прошли фильтр (rule = keep)": "| 3 | 4 | 7 |",
            "↳ источник linkedin": "| 2 | 2 | 4 |",
            "↳ источник company": "| 1 | 1 | 2 |",
            "↳ источник adzuna": "| 0 | 1 | 1 |",
            "↳ с описанием (has_desc = 1)": "| 1 | 2 | 3 |",
            "↳ с пересказом (has_desc = 2)": "| 0 | 1 | 1 |",
            "↳ только заголовок (has_desc = 0)": "| 2 | 1 | 3 |",
            "↳ оценка 60 и выше": "| 1 | 1 | 2 |",
            "↳ оценка 50-59": "| 2 | 2 | 4 |",
            "↳ оценка ниже 50": "| 0 | 1 | 1 |",
            "Мусор из писем LinkedIn (футеры)": "| 1 | 2 | 3 |",
            "Новых и закрыто (closed = 1)": "| 1 | 1 | 2 |",
            "Отправлено, включая строки списка (по дню sent_at)": "| 2 | 1 | 3 |",
            "Карточки, не строки списка (listed = 0, по дню sent_at)": "| 1 | 1 | 2 |",
            "Отправлено закрытых": "| 1 | 0 | 1 |",
            "Отправлено старых (posted старше 30 дн.)": "| 1 | 0 | 1 |",
            "Отметок (по дню verdict_at)": "| 1 | 1 | 2 |",
        }
        for label, cells in expect.items():
            with self.subTest(label=label):
                self.assertEqual(self.line(out, label), f"| {label} {cells}")

    def test_log_rows_exact(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11", "--md")
        expect = {
            "Enrich: без описания на входе": "| 15 | 10 | 25 |",
            "Enrich: найдено оригиналов": "| 0 | 3 | 3 |",
            "↳ найдено: страница": "| н/д | 2 | 2 |",
            "↳ найдено: пересказ": "| н/д | 1 | 1 |",
            "Enrich: не найдено": "| 15 | 7 | 22 |",
            "Enrich: закрыто по оригиналу": "| 1 | 0 | 1 |",
            "Лент ответило (prepare; итого - среднее за день)": "| 71 | 73 | 72 |",
            "Без адаптера или ленты (fetch; итого - среднее за день)": "| 119 | 119 | 119 |",
            "Сбоев лент (failed)": "| 1 | 0 | 1 |",
            "Сырых записей (raw per source)": "| 121 | 5 | 126 |",
            "↳ сырых adzuna": "| 20 | 0 | 20 |",
            "↳ сырых company": "| 0 | 3 | 3 |",
            "↳ сырых linkedin": "| 35 | 2 | 37 |",
            "↳ сырых stepstone": "| 66 | 0 | 66 |",
        }
        for label, cells in expect.items():
            with self.subTest(label=label):
                self.assertEqual(self.line(out, label), f"| {label} {cells}")

    def test_header_companies_and_failures(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11", "--md", "--title", "Тест")
        lines = out.splitlines()
        self.assertEqual(lines[0], "# Тест")
        self.assertIn("| Показатель | 10.10 Сб | 11.10 Вс | Итого |", lines)
        self.assertIn("- Компании с лентой (companies.json, feed_ok и feed_url): 2 из 4", lines)
        self.assertIn("- Сбои лент за период: Jobiqo: HTTPError (1 дн.)", lines)
        self.assertNotIn("Нет лога", out)

    def test_old_days_threshold(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11", "--md", "--old-days", "5")
        self.assertEqual(self.line(out, "Отправлено старых (posted старше 5 дн.)"),
                         "| Отправлено старых (posted старше 5 дн.) | 1 | 1 | 2 |")

    def test_text_output_has_same_numbers(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11")
        self.assertTrue(out.startswith("Метрики job-digest: 2026-10-10 - 2026-10-11"))
        self.assertIn("Отметок (по дню verdict_at)", out)
        self.assertIn("Enrich: найдено оригиналов", out)


class MissingLogDay(Fixture):
    def setUp(self):
        super().setUp()
        (self.logs / "2026-10-11.log").unlink()

    def test_missing_day_is_not_zero(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11", "--md")
        self.assertEqual(self.line(out, "Enrich: без описания на входе"), "| Enrich: без описания на входе | 15 | н/д | 15 |")
        self.assertEqual(self.line(out, "Лент ответило (prepare; итого - среднее за день)"),
                         "| Лент ответило (prepare; итого - среднее за день) | 71 | н/д | 71 |")
        self.assertEqual(self.line(out, "Сырых записей (raw per source)"), "| Сырых записей (raw per source) | 121 | н/д | 121 |")
        self.assertNotIn("↳ найдено: страница", out)  # no log has the field: the row is not shown
        self.assertIn("Нет лога за: 2026-10-11 (в итогах логов учтены только дни с логом)", out)
        # the DB side is still complete for the day
        self.assertEqual(self.line(out, "Новых вакансий (по дню first_seen)"), "| Новых вакансий (по дню first_seen) | 4 | 5 | 9 |")

    def test_no_logs_folder_at_all(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JOBDIGEST_DB": str(self.db)}), contextlib.redirect_stdout(out):
            metrics.main(["--from", "2026-10-10", "--to", "2026-10-10", "--logs", str(self.tmp) + "/nope"])
        self.assertIn("Нет лога за: 2026-10-10", out.getvalue())


class NewEnrichFormat(Fixture):
    """enrich.py since 16.10 prints 'found originals: F (page P, direct D, paraphrase R)'."""

    def setUp(self):
        super().setUp()
        (self.logs / "2026-10-11.log").write_text(LOG_11_NEW, encoding="utf-8")

    def test_breakdown_rows_exact(self):
        out = self.run_main("--from", "2026-10-10", "--to", "2026-10-11", "--md")
        expect = {
            "Enrich: без описания на входе": "| 15 | 10 | 25 |",
            "Enrich: найдено оригиналов": "| 0 | 7 | 7 |",
            "↳ найдено: страница": "| н/д | 3 | 3 |",
            "↳ найдено: напрямую": "| н/д | 2 | 2 |",
            "↳ найдено: пересказ": "| н/д | 2 | 2 |",
            "Enrich: не найдено": "| 15 | 3 | 18 |",
            "Enrich: закрыто по оригиналу": "| 1 | 0 | 1 |",
        }
        for label, cells in expect.items():
            with self.subTest(label=label):
                self.assertEqual(self.line(out, label), f"| {label} {cells}")

    def test_parse_both_lines(self):
        nums, _ = metrics.parse_log(
            "without description: 12, found originals: 7 (page 3, direct 2, paraphrase 4), not found: 5, closed: 1, "
            "failed (not counted as tries): 0, skipped for time: 0\n")
        self.assertEqual({k: nums[k] for k in ("todo", "found", "found_page", "found_direct", "found_paraphrase",
                                               "not_found", "closed")},
                         {"todo": 12, "found": 7, "found_page": 3, "found_direct": 2, "found_paraphrase": 4,
                          "not_found": 5, "closed": 1})
        old, _ = metrics.parse_log(
            "without description: 10, found originals: 3, found page: 2, found paraphrase: 1, not found: 7, closed: 0\n")
        self.assertEqual((old["todo"], old["found"], old["found_page"], old["found_paraphrase"], old["not_found"]),
                         (10, 3, 2, 1, 7))
        self.assertNotIn("found_direct", old)

    def test_zero_breakdown_is_present_not_missing(self):
        nums, _ = metrics.parse_log("without description: 4, found originals: 0 (page 0, direct 0, paraphrase 0), "
                                    "not found: 4\n")
        self.assertEqual((nums["found"], nums["found_page"], nums["found_direct"], nums["found_paraphrase"]), (0, 0, 0, 0))
        self.assertEqual(nums["todo"], 4)


class Junk(unittest.TestCase):
    def test_footers_are_junk(self):
        self.assertTrue(metrics.is_junk(row(title="See all jobs on LinkedIn: https://x", company="Foo", closed_by=None)))
        self.assertTrue(metrics.is_junk(row(title="Edit alert https://y", company="Foo", closed_by=None)))
        self.assertTrue(metrics.is_junk(row(title="Product Manager", company="New jobs from your other alerts", closed_by=None)))
        self.assertTrue(metrics.is_junk(row(title="  see ALL jobs x", company="Foo", closed_by=None)))
        self.assertTrue(metrics.is_junk(row(title="Product Manager", company="Foo", closed_by="junk")))
        self.assertTrue(metrics.is_junk(row(title="New jobs match your preferences.", company="Manage alerts: https://x", closed_by=None)))
        self.assertTrue(metrics.is_junk(row(title="A new job matches your preferences.", company="Foo", closed_by=None)))

    def test_real_vacancies_are_not_junk(self):
        self.assertFalse(metrics.is_junk(row(title="Head of See all jobs", company="Foo", closed_by=None)))
        self.assertFalse(metrics.is_junk(row(title="Product Manager", company="Foo", closed_by=None)))
        self.assertFalse(metrics.is_junk(row(title=None, company=None, closed_by=None)))


class Safety(Fixture):
    def test_db_file_is_not_changed(self):
        before = self.db.read_bytes()
        self.run_main("--from", "2026-10-10", "--to", "2026-10-16", "--md")
        self.assertEqual(self.db.read_bytes(), before)

    def test_connection_is_read_only(self):
        con = metrics.open_ro(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("delete from jobs")
        finally:
            con.close()

    def test_missing_db_stops_with_message(self):
        with mock.patch.dict(os.environ, {"JOBDIGEST_DB": str(Path(self.tmp) / "absent.db")}):
            with self.assertRaises(SystemExit) as cm:
                metrics.main(["--from", "2026-10-10", "--to", "2026-10-10", "--logs", str(self.logs)])
        self.assertIn("no database", str(cm.exception))

    def test_bad_dates_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                metrics.main(["--from", "2026-10-11", "--to", "2026-10-10"])
            with self.assertRaises(SystemExit):
                metrics.main(["--from", "10.10.2026", "--to", "2026-10-10"])


class Parsing(unittest.TestCase):
    def test_new_format_fields_are_read(self):
        nums, failed = metrics.parse_log(LOG_11)
        self.assertEqual(nums["found_page"], 2)
        self.assertEqual(nums["found_paraphrase"], 1)
        self.assertEqual(nums["found"], 3)
        self.assertEqual(nums["feed_failed"], 0)
        self.assertEqual(failed, {})

    def test_absent_lines_are_absent_not_zero(self):
        nums, _ = metrics.parse_log("nothing useful here\n")
        self.assertEqual(dict(nums), {})

    def test_repeated_lines_add_up_and_bad_lines_are_ignored(self):
        text = ("feeds answered 5, closed by feed 1\nfeeds answered 4\n"
                "failed: not a list\nraw per source: [1, 2]\n"
                "failed: ['A: x', 'B: y']\n")
        nums, failed = metrics.parse_log(text)
        self.assertEqual(nums["feeds"], 9)
        self.assertEqual(nums["feed_failed"], 2)
        self.assertEqual(failed, {"A: x": 1, "B: y": 1})
        self.assertNotIn("raw_total", nums)

    def test_companies_counts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "companies.json"
            p.write_text(json.dumps([{"feed_ok": True, "feed_url": "u"}, {"feed_ok": True}, {"feed_url": "u"}]))
            self.assertEqual(metrics.companies_counts(p), (1, 3))
            self.assertIsNone(metrics.companies_counts(Path(d) / "none.json"))

    def test_day_range_is_inclusive(self):
        self.assertEqual(metrics.day_range("2026-10-15", "2026-10-16"), ["2026-10-15", "2026-10-16"])
        self.assertEqual(len(metrics.day_range("2026-10-10", "2026-10-16")), 7)


if __name__ == "__main__":
    unittest.main()
