"""Quiet-failure signals (audit 16.10): enrich finds too little, no new kept jobs, no LinkedIn or StepStone mail,
a company feed failing 3 mornings in a row. Each needs history, fires on the right history only, and is pushed
once a day. No network: the Telegram sender is a fake, the clock is set by the test, out/ is a scratch folder."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import health  # noqa: E402
import prepare  # noqa: E402
import store  # noqa: E402
from scripts import alert  # noqa: E402

CLOCK = {"today": date(2026, 10, 16)}


class FakeDate(date):
    @classmethod
    def today(cls):
        return CLOCK["today"]


def ago(k):
    return (CLOCK["today"] - timedelta(days=k)).isoformat()


ENRICH_LOW = "Поиск описаний нашёл мало: 1 из 10 и 2 из 12 за два утра - проверь enrich"
KEPT = "Два утра подряд нет новых вакансий после фильтра"


class Signals(unittest.TestCase):
    def setUp(self):
        CLOCK["today"] = date(2026, 10, 16)
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "data").mkdir()
        (d / "out").mkdir()
        self.out = d / "out"
        self.send = mock.MagicMock(return_value=True)  # never reaches Telegram
        self.patches = [mock.patch.object(store, "DB", d / "data" / "state.db"),
                        mock.patch.object(store, "OUT", self.out),
                        mock.patch.object(health, "date", FakeDate),
                        mock.patch.object(alert, "send", self.send)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    # ---- helpers: history as finish() would have stored it, and this morning's run
    def past(self, *entries):
        """entries: dicts with 'k' (days ago) and any of enrich/new_kept/feeds_failed/mails."""
        rows = [dict({"day": ago(e["k"]), "enrich": None, "new_kept": None, "feeds_failed": None, "mails": None,
                      "email_error": False, "pushed": []}, **{x: y for x, y in e.items() if x != "k"}) for e in entries]
        (self.out / "health_history.json").write_text(json.dumps(rows), encoding="utf-8")

    def morning(self, enrich=None, kept=None, mails=None, failed=None, email_error=False, **enrich_extra):
        """Today's run: collect, enrich, prepare records and company_feeds.json, as the morning would leave them."""
        health.start()
        if enrich is not None:
            todo, found = enrich
            health.record("enrich", todo=todo, found=found, not_found=todo - found, closed=0,
                          failed=enrich_extra.pop("claude_failed", 0), skipped=0, **enrich_extra)
        if kept is not None:
            health.record("prepare", new=kept + 1, new_kept=kept)
        collect = {"email_error": "LoginError" if email_error else "", "email_days": 2, "mails": 0}
        if mails is not None:
            collect["mails_by_source"] = mails
        health.record("collect", **collect)
        if failed is not None:
            (self.out / "company_feeds.json").write_text(json.dumps(
                {"totals": {"x": 1}, "failed": {cid: f"{cid}: HTTPError" for cid in failed}, "asked": 5}))

    def lines(self):
        fails, notes = health.problems()
        return fails + notes

    def pushed_texts(self):
        return [c.args[0] for c in self.send.call_args_list]

    # ---- first morning and no history: silence
    def test_first_morning_is_silent_even_when_everything_looks_broken(self):
        self.morning(enrich=(10, 0), kept=0, mails={"linkedin": 0, "stepstone": 0}, failed=["jobiqo"])
        text = " ".join(self.lines())
        self.assertNotIn("Поиск описаний нашёл мало", text)
        self.assertNotIn("нет новых вакансий", text)
        self.assertNotIn("Нет писем", text)
        self.assertNotIn("не отвечает 3 дня", text)
        health.finish()
        self.assertEqual(self.send.call_count, 0)

    # ---- 1. enrich
    def test_enrich_fires_on_two_low_mornings(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 1}})
        self.morning(enrich=(12, 2))
        self.assertIn(ENRICH_LOW, self.lines())
        health.finish()
        self.assertEqual(self.pushed_texts(), [ENRICH_LOW])

    def test_enrich_quiet_when_one_morning_is_fine(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 4}})  # 0.40
        self.morning(enrich=(12, 2))
        self.assertFalse([t for t in self.lines() if "Поиск описаний" in t])

    def test_enrich_threshold_is_strict(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 3}})  # exactly 0.30 is not low
        self.morning(enrich=(10, 3))
        self.assertFalse([t for t in self.lines() if "Поиск описаний" in t])

    def test_enrich_quiet_on_small_todo(self):
        self.past({"k": 1, "enrich": {"todo": 4, "found": 0}})
        self.morning(enrich=(4, 0))
        self.assertFalse([t for t in self.lines() if "Поиск описаний" in t])

    def test_enrich_quiet_across_a_missing_morning(self):
        self.past({"k": 2, "enrich": {"todo": 10, "found": 0}})  # yesterday is missing: no comparison
        self.morning(enrich=(10, 0))
        self.assertFalse([t for t in self.lines() if "Поиск описаний" in t])

    # ---- 2. new kept jobs
    def test_zero_kept_fires_on_two_mornings(self):
        self.past({"k": 1, "new_kept": 0})
        self.morning(kept=0)
        self.assertIn(KEPT, self.lines())

    def test_zero_kept_quiet_when_yesterday_had_some(self):
        self.past({"k": 1, "new_kept": 2})
        self.morning(kept=0)
        self.assertNotIn(KEPT, self.lines())

    def test_zero_kept_quiet_when_prepare_did_not_record(self):
        self.past({"k": 1, "new_kept": 0})
        self.morning()  # no prepare record today
        self.assertNotIn(KEPT, self.lines())

    # ---- 3. mails
    def test_missing_linkedin_mail_is_named_alone(self):
        self.past({"k": 1})
        self.morning(mails={"linkedin": 0, "stepstone": 2})
        lines = self.lines()
        self.assertIn("Нет писем LinkedIn за 2 дня", lines)
        self.assertFalse([t for t in lines if "StepStone" in t])

    def test_missing_stepstone_mail_fires(self):
        self.past({"k": 1})
        self.morning(mails={"linkedin": 3})
        self.assertIn("Нет писем StepStone за 2 дня", self.lines())

    def test_mailbox_failure_does_not_add_a_mail_line(self):
        self.past({"k": 1})
        self.morning(mails={}, email_error=True)
        self.assertFalse([t for t in self.lines() if t.startswith("Нет писем")])

    def test_no_mail_count_recorded_is_silent(self):
        self.past({"k": 1})
        self.morning(mails=None)
        self.assertFalse([t for t in self.lines() if t.startswith("Нет писем")])

    # ---- 4. company feeds
    def test_feed_failing_three_mornings_in_a_row_fires_once(self):
        self.past({"k": 2, "feeds_failed": ["jobiqo", "x"]}, {"k": 1, "feeds_failed": ["jobiqo"]})
        self.morning(failed=["jobiqo"])
        self.assertIn("Лента Jobiqo не отвечает 3 дня подряд", self.lines())
        health.finish()
        self.assertEqual(self.pushed_texts(), ["Лента Jobiqo не отвечает 3 дня подряд"])

    def test_feed_failing_two_mornings_is_quiet(self):
        self.past({"k": 2, "feeds_failed": []}, {"k": 1, "feeds_failed": ["jobiqo"]})
        self.morning(failed=["jobiqo"])
        self.assertFalse([t for t in self.lines() if "не отвечает 3 дня" in t])

    def test_feed_gap_in_history_is_quiet(self):
        self.past({"k": 2, "feeds_failed": ["jobiqo"]})
        self.morning(failed=["jobiqo"])
        self.assertFalse([t for t in self.lines() if "не отвечает 3 дня" in t])

    def test_feed_not_read_today_is_quiet(self):
        self.past({"k": 2, "feeds_failed": ["jobiqo"]}, {"k": 1, "feeds_failed": ["jobiqo"]})
        self.morning(failed=None)  # no company_feeds.json today
        self.assertFalse([t for t in self.lines() if "не отвечает 3 дня" in t])

    # ---- once a day, every day
    def test_push_once_per_day_even_when_finish_runs_twice(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 1}})
        self.morning(enrich=(12, 2))
        health.finish()
        self.morning(enrich=(12, 2))  # a second collect run the same morning
        health.finish()
        self.assertEqual(self.pushed_texts(), [ENRICH_LOW])
        self.assertIn(ENRICH_LOW, self.lines())  # the digest block still shows it

    def test_failed_push_is_retried_on_the_next_finish(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 1}})
        self.morning(enrich=(12, 2))
        self.send.return_value = False
        health.finish()
        self.send.return_value = True
        health.finish()
        self.assertEqual(self.send.call_count, 2)

    def test_persistent_signal_pushes_on_day_one_then_every_third_day(self):
        """Streak of 7 mornings: pushed on days 1, 4, 7 only; the digest block still shows it each day."""
        self.past({"k": 1, "enrich": {"todo": 10, "found": 1}})
        pushes = []
        for day in range(16, 23):
            CLOCK["today"] = date(2026, 10, day)
            self.morning(enrich=(12, 2))
            health.finish()
            pushes.append(self.send.call_count)
            self.assertTrue([t for t in self.lines() if t.startswith("Поиск описаний нашёл мало")])
        self.assertEqual(pushes, [1, 1, 1, 2, 2, 2, 3])

    def test_streak_restarts_after_a_quiet_morning(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 1}})
        self.morning(enrich=(12, 2))
        health.finish()                      # day 16: streak 1, pushed
        CLOCK["today"] = date(2026, 10, 17)
        self.morning(enrich=(12, 9))         # quiet: streak broken
        health.finish()
        CLOCK["today"] = date(2026, 10, 18)
        self.morning(enrich=(12, 2))         # one low morning after a quiet one: the 2-morning rule does not hold yet
        health.finish()
        self.assertEqual(self.send.call_count, 1)
        CLOCK["today"] = date(2026, 10, 19)
        self.morning(enrich=(12, 2))         # two low mornings again: day 1 of a new streak, pushed
        health.finish()
        self.assertEqual(self.send.call_count, 2)

    def test_pages_signal_fires_when_pages_give_nothing_but_paraphrases_do(self):
        low = {"todo": 10, "found": 0, "found_page": 0, "found_paraphrase": 4}
        self.past({"k": 1, "enrich": dict(low)})
        self.morning(enrich=(10, 4), found_page=0, found_paraphrase=4)
        self.assertIn("Скачивание страниц не работает: за два утра ни одного текста со страницы компании, "
                      "пересказов 4 и 4 - проверь enrich", self.lines())

    def test_pages_signal_needs_three_paraphrases(self):
        """Two paraphrases may all be job-board links, which the page download refuses by design."""
        self.past({"k": 1, "enrich": {"todo": 10, "found": 2, "found_page": 0, "found_paraphrase": 2}})
        self.morning(enrich=(10, 2), found_page=0, found_paraphrase=2)
        self.assertFalse([t for t in self.lines() if "Скачивание" in t])

    def test_failed_push_is_retried_next_morning_and_cadence_stays(self):
        """Day 1 push fails: day 2 retries (and succeeds); the 1/4/7 cadence still counts from day 1."""
        self.past({"k": 1, "enrich": {"todo": 10, "found": 1}})
        self.send.return_value = False
        self.morning(enrich=(12, 2))
        health.finish()                                  # day 16: scheduled, fails
        self.assertEqual(self.send.call_count, 1)
        self.send.return_value = True
        CLOCK["today"] = date(2026, 10, 17)
        self.morning(enrich=(12, 2))
        health.finish()                                  # day 17: retry, succeeds
        self.assertEqual(self.send.call_count, 2)
        CLOCK["today"] = date(2026, 10, 18)
        self.morning(enrich=(12, 2))
        health.finish()                                  # day 18: nothing new
        self.assertEqual(self.send.call_count, 2)
        CLOCK["today"] = date(2026, 10, 19)
        self.morning(enrich=(12, 2))
        health.finish()                                  # day 19: streak day 4, scheduled
        self.assertEqual(self.send.call_count, 3)

    def test_pages_signal_quiet_when_pages_find_something(self):
        self.past({"k": 1, "enrich": {"todo": 10, "found": 3, "found_page": 3, "found_paraphrase": 4}})
        self.morning(enrich=(10, 3), found_page=0, found_paraphrase=4)
        self.assertFalse([t for t in self.lines() if "Скачивание" in t])

    def test_found_counts_page_plus_paraphrase_and_leaves_out_claude_failures(self):
        """7 found of 10 asked (failures excluded): not low, even though plain found was 0."""
        self.past({"k": 1, "enrich": {"todo": 10, "found": 0, "found_page": 2, "found_paraphrase": 5, "failed": 0}})
        self.morning(enrich=(10, 0), found_page=2, found_paraphrase=5)
        self.assertFalse([t for t in self.lines() if "Поиск описаний" in t])
        self.past({"k": 1, "enrich": {"todo": 10, "found": 0, "found_page": 0, "found_paraphrase": 0, "failed": 6}})
        self.morning(enrich=(10, 0), claude_failed=6)  # asked = 4 < 5: too few to judge
        self.assertFalse([t for t in self.lines() if "Поиск описаний" in t])

    def test_bad_history_entries_are_dropped_and_problems_never_raise(self):
        bad = ["junk", {"day": 5}, {"day": "not-a-date", "new_kept": 0},
               {"day": ago(1), "enrich": {"todo": "10", "found": 1}},
               {"day": ago(1), "new_kept": True}, {"day": ago(1), "mails": {"linkedin": "0"}}]
        (self.out / "health_history.json").write_text(json.dumps(bad), encoding="utf-8")
        self.morning(enrich=(10, 0), kept=0, mails={"linkedin": 0})
        self.assertEqual(self.lines().count(KEPT), 0)
        self.assertEqual(health.problems()[0].count(ENRICH_LOW), 0)
        health.finish()
        self.assertEqual(self.send.call_count, 0)

    def test_corrupt_history_file_does_not_break_the_digest_header(self):
        (self.out / "health_history.json").write_text("{not json", encoding="utf-8")
        self.morning(enrich=(10, 0))
        fails, notes = health.problems()
        self.assertIsInstance(fails, list)
        (self.out / "health_history.json").write_text(json.dumps({"day": "x"}), encoding="utf-8")
        self.assertIsInstance(health.problems()[0], list)

    def test_manual_rerun_keeps_the_richer_morning(self):
        self.past({"k": 1, "new_kept": 0})
        self.morning(enrich=(12, 2), kept=0)
        health.finish()
        self.morning(enrich=(0, 0), kept=0)  # a manual enrich rerun finds nothing to do
        health.finish()
        days = json.loads((self.out / "health_history.json").read_text(encoding="utf-8"))
        self.assertEqual(days[-1]["enrich"]["todo"], 12)
        self.assertIn(KEPT, self.lines())

    def test_history_keeps_seven_days(self):
        self.past(*[{"k": k, "new_kept": 1} for k in range(1, 9)])  # 8 past mornings
        self.morning(kept=1)
        health.finish()
        days = json.loads((self.out / "health_history.json").read_text(encoding="utf-8"))
        self.assertEqual(len(days), health.HISTORY_DAYS)
        self.assertNotIn(ago(8), [d["day"] for d in days])
        self.assertEqual(days[-1]["day"], ago(0))

    def test_mails_without_a_vacancy_list_are_logged_not_shown(self):
        """Stage 10 item 4 (04.11.2026): jobs.at and karriere.at mails without a vacancy list are a known source limit.
        The digest header does not name them every morning; the collect record (the log) keeps the count."""
        health.start()
        health.record("collect", email_days=2, adzuna_calls=10, no_list=3)
        self.assertEqual([s for s in self.lines() if "без списка" in s], [])
        self.assertEqual(health.load()["collect"]["no_list"], 3)

    def test_block_lines_are_plain_russian_in_the_digest_header_list(self):
        self.past({"k": 1, "new_kept": 0})
        self.morning(kept=0)
        fails, _ = health.problems()
        self.assertIn(KEPT, fails)
        self.assertLessEqual(len(fails), health.ALERT_LINES)


class PrepareRecordsKept(unittest.TestCase):
    """prepare.py records how many new jobs passed the rule: that number is what the signal reads."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "data").mkdir()
        (d / "out").mkdir()
        self.out = d / "out"
        self.patches = [mock.patch.object(store, "DB", d / "data" / "state.db"),
                        mock.patch.object(store, "OUT", self.out),
                        mock.patch.object(prepare, "smartrecruiters_description", lambda u: "")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    def test_new_kept_counts_only_rule_keep(self):
        jobs = [{"id": "a", "source": "linkedin", "title": "Junior PM", "company": "ACME", "location": "Wien",
                 "url": "https://www.linkedin.com/jobs/view/1/", "rule": "keep"},
                {"id": "b", "source": "linkedin", "title": "Sales Manager", "company": "Other", "location": "Wien",
                 "url": "https://www.linkedin.com/jobs/view/2/", "rule": "title:sales"}]
        (self.out / "prefiltered.json").write_text(json.dumps(jobs), encoding="utf-8")
        (self.out / "company_jobs.json").write_text("[]", encoding="utf-8")
        health.start()
        with contextlib.redirect_stdout(io.StringIO()):
            prepare.main()
        rec = health.load()["prepare"]
        self.assertEqual((rec["new"], rec["new_kept"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
