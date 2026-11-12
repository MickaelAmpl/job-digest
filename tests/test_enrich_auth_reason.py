"""Stage 11 Dev C (05.11.2026 incident): when the Claude login on the server expires, a title-only job's search fails with
an auth error. enrich.py marks the job data.enrich_error = "auth" (not counted as a try), the next successful search
clears it, and the digest card says why the description is missing. No network, no real claude: subprocess.run and
fetch_posting are fakes."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import digest  # noqa: E402
import enrich  # noqa: E402
import store  # noqa: E402

LOGIN_LOST = "Failed to authenticate: OAuth session expired and could not be refreshed"
SCORE = {"score": 55, "why_ru": "по названию", "german": "none", "hours": "part-time", "level": "junior",
         "skill_gaps": []}
JOB = {"id": "e1", "title": "Product Owner", "company": "TechTalk", "location": "Wien", "description": "",
       "enrich_tries": 0, "url": ""}


def envelope(result, is_error=True):
    return json.dumps({"is_error": is_error, "result": result})


class Markers(unittest.TestCase):
    def test_login_messages_are_auth_errors(self):
        for text in (LOGIN_LOST, "Please run /login", "Not logged in", "Invalid API key",
                     "authentication error: token", "OAuth error"):
            with self.subTest(text=text):
                self.assertTrue(enrich.auth_error(text))

    def test_other_failures_are_not_auth_errors(self):
        for text in ("TimeoutExpired: timed out after 420 seconds", "rate limit reached, try later",
                     "529 overloaded", "RuntimeError: empty answer", ""):
            with self.subTest(text=text):
                self.assertFalse(enrich.auth_error(text))


class Flag(unittest.TestCase):
    """enrich.main on one title-only job; claude is a fake subprocess that answers as the test says."""

    def run_main(self, job, run):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "data").mkdir()
            (d / "out").mkdir()
            (d / "out" / "to_score.json").write_text(json.dumps([job]))
            fake = types.SimpleNamespace(fetch_description=lambda url, job, timeout=20: ("", "fail: none"))
            with mock.patch.object(store, "DB", d / "data" / "state.db"), mock.patch.object(store, "OUT", d / "out"), \
                    mock.patch.object(enrich, "PATH", d / "out" / "to_score.json"), \
                    mock.patch.object(enrich, "direct_page", lambda j: None), \
                    mock.patch.object(enrich.subprocess, "run", run), \
                    mock.patch.dict(sys.modules, {"fetch_posting": fake}), mock.patch.object(enrich.health, "record"), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                enrich.main()
            saved = json.loads((d / "out" / "to_score.json").read_text())[0]
        return saved, err.getvalue()

    def test_login_failure_marks_the_job_and_is_not_a_try(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, envelope(LOGIN_LOST), ""))
        saved, _ = self.run_main(dict(JOB), run)
        self.assertEqual(saved.get("enrich_error"), "auth")
        self.assertEqual(saved["enrich_tries"], 0)  # not counted as a search try
        self.assertEqual(saved.get("enrich_errors", 0), 0)  # the server's login, not the job's: no error count

    def test_six_login_outages_do_not_block_the_search_after_relogin(self):
        """A login outage is the server's fault: 6 mornings of auth failures (more than MAX_ERRORS) leave the job
        searchable, and the first successful search after the re-login runs."""
        job = dict(JOB)
        for _ in range(6):
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, envelope(LOGIN_LOST), ""))
            job, _ = self.run_main(job, run)
            self.assertEqual(job.get("enrich_errors", 0), 0)
            self.assertEqual(job.get("enrich_error"), "auth")
        answer = json.dumps({"found": False, "still_open": "unknown"})
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, envelope(answer, is_error=False), ""))
        job, _ = self.run_main(job, run)
        run.assert_called_once()  # the search ran
        self.assertEqual(job["enrich_tries"], 1)
        self.assertNotIn("enrich_error", job)

    def test_login_message_only_on_stderr_is_still_marked(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", LOGIN_LOST))
        saved, _ = self.run_main(dict(JOB), run)
        self.assertEqual(saved.get("enrich_error"), "auth")

    def test_other_failures_do_not_mark_the_job(self):
        run = mock.Mock(side_effect=subprocess.TimeoutExpired(["claude"], 420))
        saved, _ = self.run_main(dict(JOB), run)
        self.assertNotIn("enrich_error", saved)
        self.assertEqual(saved["enrich_errors"], 1)

        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, envelope("rate limit reached"), ""))
        saved, _ = self.run_main(dict(JOB), run)
        self.assertNotIn("enrich_error", saved)

    def test_a_successful_search_clears_the_flag(self):
        answer = json.dumps({"found": False, "still_open": "unknown"})
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, envelope(answer, is_error=False), ""))
        saved, _ = self.run_main(dict(JOB, enrich_error="auth"), run)
        self.assertNotIn("enrich_error", saved)
        self.assertEqual(saved["enrich_tries"], 1)


class Card(unittest.TestCase):
    """The card line for a title-only job (digest.job_message); the other cases keep their old lines."""

    NEW = "<i>Описание не искал: Claude на сервере разлогинился - поищу после повторного входа.</i>"
    OLD = "<i>Описание ещё не искал (не хватило времени утром), оценка по названию - поищу завтра.</i>"

    def job(self, **kw):
        return dict({"id": "x1", "title": "Sales Lead", "company": "Acme", "title_only": True, "searched": False}, **kw)

    def test_login_lost_gets_the_login_line(self):
        text, _ = digest.job_message(self.job(enrich_error="auth"), SCORE)
        self.assertIn(self.NEW, text)
        self.assertNotIn("Описание ещё не искал", text)

    def test_other_title_only_card_is_unchanged(self):
        text, _ = digest.job_message(self.job(), SCORE)
        self.assertIn(self.OLD, text)
        self.assertNotIn("разлогинился", text)

    def test_searched_and_not_found_is_unchanged(self):
        text, _ = digest.job_message(self.job(searched=True), SCORE)
        self.assertIn("<i>Описание не нашёл, оценка только по названию - открой объявление.</i>", text)
        self.assertNotIn("разлогинился", text)

    def test_flag_survives_the_row_round_trip_to_the_card(self):
        """score.save writes the job dict into data; the card reads it back through card_item."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "data").mkdir()
            with mock.patch.object(store, "DB", d / "data" / "state.db"), mock.patch.object(store, "OUT", d):
                con = store.connect()
                data = dict(JOB, enrich_error="auth")
                con.execute("insert into jobs (id, key, company, title, url, source, rule, first_seen, scored_at,"
                            " score, score_json, has_desc, enrich_tries, data) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("e1", "techtalk|po|e1", "TechTalk", "Product Owner", "", "company", "keep", store.now(),
                             store.now(), 55, json.dumps(SCORE), 0, 0, json.dumps(data)))
                con.commit()
                row = con.execute("select * from jobs where id='e1'").fetchone()
                job, score, _ = digest.card_item(row)
                con.close()
        text, _ = digest.job_message(job, score)
        self.assertIn("разлогинился", text)


if __name__ == "__main__":
    unittest.main()
