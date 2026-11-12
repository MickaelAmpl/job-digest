"""Stage 7 step 2 (16.10.2026): LinkedIn alert mails bundle several saved searches, and the service lines between
them ('See all jobs', 'Edit alert', saved-search names, links) must never come out as a job's title or company.
Fixtures in tests/fixtures/linkedin are cut before the personal footer and carry no real name; the 38 real
mails are read from the session scratchpad when it exists (skipped otherwise). scripts/repair_junk.py is run
on a scratch database only."""
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402
import store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "linkedin"
SAMPLES = Path("<scratch>"
               "/scratchpad/s7/linkedin_mails")
JUNK_TOKENS = ("see all jobs", "edit alert", "http", "<strong", "jobs in vienna", "manage alerts",
               "match your preferences")
VIEW = re.compile(r"View job: https://www\.linkedin\.com/comm/jobs/view/(\d+)")


def fixture_mails():
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("*.txt"))]


def sample_mails():
    """The 38 real alert mails (sanitized copies in the session scratchpad); empty when that folder is gone."""
    if not SAMPLES.is_dir():
        return []
    return [(p.name, p.read_text(encoding="utf-8")) for p in sorted(SAMPLES.glob("linkedin-*.txt"))]


def mails():
    return fixture_mails() + sample_mails()


class LinkedinParseTest(unittest.TestCase):

    def assertClean(self, job, where):
        text = (job["title"] + " | " + job["company"]).lower()
        for token in JUNK_TOKENS:
            self.assertNotIn(token, text, f"{where}: service text in a job: {job['title']!r} / {job['company']!r}")

    def test_no_service_text_in_fixtures(self):
        found = fixture_mails()
        self.assertTrue(found)
        for name, body in found:
            for job in collect.parse_linkedin(body):
                self.assertClean(job, name)

    def test_one_job_per_distinct_view_link_in_fixtures(self):
        self.assertTrue(fixture_mails())
        for name, body in fixture_mails():
            self.check_counts(name, body)

    @unittest.skipUnless(SAMPLES.is_dir(), "real LinkedIn samples are in the session scratchpad only")
    def test_all_38_real_mails(self):
        found = sample_mails()
        self.assertEqual(len(found), 38)
        for name, body in found:
            self.check_counts(name, body)
            for job in collect.parse_linkedin(body):
                self.assertClean(job, name)

    def check_counts(self, name, body):
        ids = set(VIEW.findall(body))
        jobs = collect.parse_linkedin(body)
        self.assertEqual(len(jobs), len(ids), name)
        self.assertEqual({j["ext_id"] for j in jobs}, {f"li:{i}" for i in ids}, name)

    def test_known_pairs_from_mail_30(self):
        jobs = {j["ext_id"]: j for j in collect.parse_linkedin((FIXTURES / "linkedin-30.txt").read_text(encoding="utf-8"))}
        erste = jobs["li:4478779860"]
        self.assertEqual(erste["title"], "FH-/Uni-Praktikum Corporate Communications - Retail & Digital Strategy "
                                         "Communications (w/m/d)")
        self.assertEqual(erste["company"], "Erste Group")
        self.assertEqual(erste["location"], "Vienna, Austria")
        self.assertEqual(erste["extra"], "5 school alumni")
        capgemini = jobs["li:4479266499"]
        self.assertEqual(capgemini["title"], "Student Business Analyst Consultant (m/w/d)")
        self.assertEqual(capgemini["company"], "Capgemini")
        self.assertEqual(capgemini["extra"], "This company is actively hiring")
        self.assertEqual(jobs["li:4467605519"]["title"], "Freie/r Dienstnehmer:in Bereich Strategy & Sustainability "
                                                         "(m/w/d) - ca. 20h/Woche")
        self.assertEqual(len(jobs), 4)

    def test_header_and_wrapped_query_lines_above_a_job_are_not_read(self):
        dash = "-" * 30
        body = ("Your job alert for \"pm\" in Vienna\nTop job picks for you\n\n"
                "Product Owner (m/w/d)\nACME GmbH\nVienna\n5 school alumni\n"
                "View job: https://www.linkedin.com/comm/jobs/view/111/?x=1\n" + dash + "\n"
                "See all jobs https://www.linkedin.com/jobs/search-results/?keywords=(Internship\n"
                "(Entry level OR Praktikum)&distance=25\nEdit alert https://www.linkedin.com/jobs/jam/manage/1\n"
                "Praktikum Marketing\nBeta AG\nWien, Austria\nBe an early applicant\n"
                "View job: https://www.linkedin.com/comm/jobs/view/222/\n" + dash + "\n"
                "Promoted\nCustomer Connection Manager\nGamma\nWien\n"
                "View job: https://www.linkedin.com/comm/jobs/view/333/\n")
        jobs = {j["ext_id"]: j for j in collect.parse_linkedin(body)}
        self.assertEqual(set(jobs), {"li:111", "li:222", "li:333"})
        self.assertEqual((jobs["li:111"]["title"], jobs["li:111"]["company"], jobs["li:111"]["location"],
                          jobs["li:111"]["extra"]),
                         ("Product Owner (m/w/d)", "ACME GmbH", "Vienna", "5 school alumni"))
        self.assertEqual((jobs["li:222"]["title"], jobs["li:222"]["company"], jobs["li:222"]["extra"]),
                         ("Praktikum Marketing", "Beta AG", "Be an early applicant"))
        self.assertEqual((jobs["li:333"]["title"], jobs["li:333"]["company"]),
                         ("Customer Connection Manager", "Gamma"))

    def test_block_without_three_real_lines_is_skipped(self):
        body = ("See all jobs https://www.linkedin.com/jobs/search-results/\n"
                "Edit alert https://www.linkedin.com/jobs/jam/manage/1\n"
                "Only one real line\n"
                "View job: https://www.linkedin.com/comm/jobs/view/111/\n")
        self.assertEqual(collect.parse_linkedin(body), [])

    def test_service_lines_are_recognised(self):
        service = ["See all jobs on LinkedIn:  https://www.linkedin.com/comm/jobs/search-results/",
                   "Edit alert https://www.linkedin.com/jobs/jam/manage/5",
                   "New jobs from your other alerts", "Your job alert for \"pm\" in Vienna",
                   "A new job matches your preferences.", "New jobs match your preferences.",
                   "Manage alerts: https://www.linkedin.com/comm/jobs/alerts",
                   "You’ll receive notifications when new jobs are posted", "You'll receive notifications",
                   '<strong class="font-bold">(pm OR po)</strong> jobs in Vienna',
                   "(werkstudent) (product) JOBS IN AUSTRIA", "https://www.linkedin.com/jobs/view/1/",
                   "Top job picks for you", "Jobs you may be interested in", "Promoted",
                   "(Entry level OR Praktikum)&distance=25", "Senior <strong>pm</strong>", "More jobs for you",
                   "Something with https://example.test inside"]
        for line in service:
            self.assertTrue(collect.linkedin_service(line), line)
        for line in ["Product Owner (m/w/d)", "Erste Group", "Vienna, Austria", "5 school alumni",
                     "This company is actively hiring", "HTTP-Entwickler:in", "Praktikum im Bereich Marketing"]:
            self.assertFalse(collect.linkedin_service(line), line)


EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def parsed_rows(body):
    return [dict(id=j["ext_id"][3:], title=j["title"], company=j["company"], location=j["location"])
            for j in collect.parse_linkedin(body)]


class GoldenTest(unittest.TestCase):
    """expected.json: (job id, title, company, location) per mail, reviewed by hand against the mail text
    (15 jobs checked on 16.10.2026). The six committed fixtures always run; the 38 real mails only when the
    session scratchpad holds them."""

    def test_fixtures_match_expected(self):
        self.assertEqual(set(EXPECTED["fixtures"]), {p.name for p in FIXTURES.glob("*.txt")})
        for name, want in EXPECTED["fixtures"].items():
            self.assertEqual(parsed_rows((FIXTURES / name).read_text(encoding="utf-8")), want, name)

    @unittest.skipUnless(SAMPLES.is_dir(), "real LinkedIn samples are in the session scratchpad only")
    def test_samples_match_expected(self):
        self.assertEqual(len(EXPECTED["samples"]), 38)
        for name, want in EXPECTED["samples"].items():
            self.assertEqual(parsed_rows((SAMPLES / name).read_text(encoding="utf-8")), want, name)


class RepairJunkTest(unittest.TestCase):
    """scripts/repair_junk.py on a scratch database: a junk row whose job number is in the mails is repaired
    (verdict and sent_at kept), one whose number is not is closed, a dry run writes nothing, a second run changes
    nothing. Rows closed by a feed or enrich rule are never touched."""

    def run_script(self, db, *args):
        env = dict(os.environ, JOBDIGEST_DB=str(db), JOBDIGEST_OUT=str(db.parent / "out"))
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "repair_junk.py"), *args], env=env,
                              cwd=str(ROOT), capture_output=True, text=True, timeout=120)

    def make_db(self, db):
        made = subprocess.run([sys.executable, "-c", "import store; store.connect().close()"],
                              env=dict(os.environ, JOBDIGEST_DB=str(db)), cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(made.returncode, 0, made.stderr)
        con = sqlite3.connect(db)
        junk_title = "See all jobs https://www.linkedin.com/jobs/search-results/"
        junk_company = "Edit alert https://www.linkedin.com/jobs/jam/manage/1"
        rows = [
            # id, job number, title, company, location, verdict, sent_at, closed, closed_by
            ("a1", "4478779860", junk_title, junk_company, "<strong>(x)</strong> jobs in Vienna", None, None, 0, None),
            ("a2", "4479266499", "A new job matches your preferences.", "Manage alerts: https://www.linkedin.com/x",
             "Product Owner", "interested", "2026-10-14T07:30:00", 0, None),
            ("a3", "999999", junk_title, junk_company, "x", None, None, 0, None),
            ("a4", "4478779860", "Real Product Manager", "ACME", "Wien", None, None, 0, None),
            ("a5", "4479266499", junk_title, junk_company, "x", None, None, 1, "feed"),
            # a6: a real row already holding the key the repaired a1 would get (the key clash case)
            ("a7", "4477585904", junk_title, junk_company, "x", "interested", "2026-10-14T07:30:00", 0, None),
            ("a8", "4000000002", "Product Owner - Corporate Banking (all genders)", "Accenture DACH", "Vienna",
             None, None, 0, None),
            ("a6", "4000000001", "FH-/Uni-Praktikum Corporate Communications - Retail & Digital Strategy "
                                 "Communications (w/m/d)", "Erste Group", "Vienna, Austria", None, None, 0, None),
        ]
        for rid, num, title, company, loc, verdict, sent, closed, by in rows:
            key = (store.dedupe_key({"company": company, "title": title, "url": ""}) if rid in ("a6", "a8")
                   else "key-" + rid)
            con.execute("insert into jobs (id, key, company, title, location, url, source, first_seen, data, verdict,"
                        " sent_at, closed, closed_by, scored_at, has_desc, enrich_tries, score_tries, promote, msg)"
                        " values (?, ?, ?, ?, ?, ?, 'linkedin', '2026-10-14T06:00:00', ?, ?, ?, ?, ?,"
                        " '2026-10-14T06:38:08', 0, 1, 0, 0, 'old card')",
                        (rid, key, company, title, loc, f"https://www.linkedin.com/jobs/view/{num}/",
                         json.dumps({"title": title, "company": company, "location": loc, "source": "linkedin"}),
                         verdict, sent, closed, by))
        con.commit()
        con.close()

    def test_repairs_closes_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            self.make_db(db)
            before = self.rows(db)

            dry = self.run_script(db, "--dry-run", "--mails", str(FIXTURES))
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn("would repair: 3, closed as duplicate: 1, would close: 1", dry.stdout)
            self.assertEqual(self.rows(db), before)

            run = self.run_script(db, "--mails", str(FIXTURES))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("junk rows: 4, repaired: 3, closed as duplicate: 1, closed: 1, with Misha's verdict: 2",
                          run.stdout)
            self.assertIn("WARNING verdict=interested", run.stdout)
            after = self.rows(db)
            a1 = after["a1"]
            self.assertEqual((a1["title"], a1["company"], a1["location"]),
                             ("FH-/Uni-Praktikum Corporate Communications - Retail & Digital Strategy "
                              "Communications (w/m/d)", "Erste Group", "Vienna, Austria"))
            self.assertEqual(json.loads(a1["data"])["company"], "Erste Group")
            # the repaired title is a student-only format (FH-/Uni-Praktikum, dropped by the filter since 30.10.2026),
            # so the row is not queued for scoring again: scored_at stays as it was
            self.assertEqual((a1["scored_at"], a1["has_desc"], a1["enrich_tries"], a1["score_tries"], a1["promote"]),
                             ("2026-10-14T06:38:08", 0, 0, 0, 0))
            # a1's new key is taken by a6 and a1 has no verdict: closed as a duplicate of a6, key kept, msg cleared
            self.assertEqual((a1["closed"], a1["closed_by"], a1["dup_of"], a1["key"], a1["msg"]),
                             (1, "duplicate", "a6", "key-a1", None))
            self.assertIn("DUPLICATE a1", run.stdout)
            # a7 has a verdict and the same clash: repaired, stays open, reported
            a7 = after["a7"]
            self.assertEqual((a7["title"], a7["company"], a7["closed"], a7["verdict"], a7["dup_of"]),
                             ("Product Owner - Corporate Banking (all genders)", "Accenture DACH", 0, "interested", None))
            self.assertIn("WARNING a7: its repaired key is row a8's key, verdict kept", run.stdout)
            a2 = after["a2"]
            self.assertEqual((a2["title"], a2["company"]), ("Student Business Analyst Consultant (m/w/d)", "Capgemini"))
            self.assertEqual((a2["verdict"], a2["sent_at"]), ("interested", "2026-10-14T07:30:00"))
            # rule is recomputed with prefilter.verdict; only a 'keep' row is rescored (scored_at cleared).
            # a1 (FH-/Uni-Praktikum) is a student-only format since 30.10.2026, so it is no longer 'keep' either
            self.assertEqual((a1["rule"], a2["rule"]), ("excluded_field", "excluded_field"))
            self.assertEqual(a2["scored_at"], "2026-10-14T06:38:08")
            self.assertIn("rule=keep", run.stdout)
            self.assertEqual(a2["msg"], None)
            self.assertEqual(a2["key"], store.dedupe_key({"company": "Capgemini", "title": a2["title"],
                                                          "url": a2["url"]}))
            self.assertIn("REMINDER: run scripts/repair_junk.py BEFORE scripts/requeue_title_only.py", run.stdout)
            self.assertEqual((after["a3"]["closed"], after["a3"]["closed_by"]), (1, "junk"))
            self.assertEqual(after["a3"]["title"], before["a3"]["title"])
            self.assertEqual(after["a4"], before["a4"])
            self.assertEqual(after["a5"], before["a5"])
            self.assertEqual(after["a6"], before["a6"])
            self.assertEqual(after["a7"]["key"], "key-a7")
            self.assertEqual(after["a8"], before["a8"])

            again = self.run_script(db, "--mails", str(FIXTURES))
            self.assertIn("repaired: 0, closed as duplicate: 0, closed: 0", again.stdout)
            self.assertEqual(self.rows(db), after)

    @staticmethod
    def rows(db):
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return {r["id"]: dict(r) for r in con.execute("select * from jobs")}
        finally:
            con.close()


class CollectHealthTest(unittest.TestCase):
    """collect.main() records how many mails each source delivered, for the digest's health block."""

    def test_mails_by_source_is_recorded(self):
        body = (FIXTURES / "linkedin-30.txt").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mails = [("linkedin", "Thu, 15 Oct 2026 07:00:00", body), ("linkedin", "Fri, 16 Oct 2026 07:00:00", body),
                     ("stepstone", "Fri, 16 Oct 2026 07:00:00", "")]
            with mock.patch.object(collect, "load_env", return_value={}), \
                    mock.patch.object(collect, "fetch_alert_emails", lambda env, since, stats=None: mails), \
                    mock.patch.object(collect, "fetch_adzuna", lambda env, days, stats=None: []), \
                    mock.patch.object(collect, "OUT", tmp / "out"), \
                    mock.patch.object(collect, "STATE", tmp / "collect_state.json"), \
                    mock.patch.object(collect.health, "record") as record, \
                    mock.patch.object(sys, "argv", ["collect.py", "2"]), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                collect.main()
            self.assertEqual(record.call_args.args, ("collect",))
            self.assertEqual(record.call_args.kwargs["mails_by_source"], {"linkedin": 2, "stepstone": 1})


if __name__ == "__main__":
    unittest.main()
