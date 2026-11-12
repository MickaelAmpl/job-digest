"""Stage 8 in the numbers: a manual application (source 'manual', or a bot card with data.manual_applied) counts as an
application everywhere (/stats, the report, the Sunday summary), never as a bot card or a bot mark, and never moves
scripts/metrics.py. Temp DB with the real schema; every expected number below is counted by hand from the fixture."""
import html
import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import digest  # noqa: E402
import report  # noqa: E402
import store  # noqa: E402
from scripts import metrics  # noqa: E402

ACME = {"company": "Acme Labs", "title": "Brand & Marketing Intern",
             "url": "https://acme.example/careers/brand-marketing-intern"}
EMBER = {"company": "Ember Studio", "title": "Open application", "url": "https://www.ember.example/"}
THETA = {"company": "Theta", "title": "Growth Analyst", "url": "https://theta.example/jobs/7"}  # a bot card, attached
GHOST = {"company": "Ghost Co", "title": "Ghost job", "url": ""}  # added and removed again: appears nowhere
IOTA = {"company": "Iota", "title": "Ops Coordinator", "url": "https://iota.example/ops"}  # applied 10 days ago


def ago(days):
    """A full ISO time `days` back, for bot rows."""
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def day_ago(days):
    """A day 'YYYY-MM-DD' back, for manual applications (stored as noon of that day)."""
    return (date.today() - timedelta(days=days)).isoformat()


def rows_with(page, title):
    """The <tr> fragments of the report that show this title (the page has it escaped)."""
    needle = html.escape(title)
    return [seg for seg in page.split("<tr>")[1:] if needle in seg]


class Base(unittest.TestCase):
    """A temp DB with the bot's rows only: 8 rows, 7 delivered cards, 6 marks."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(store, "DB", Path(tmp.name) / "data" / "state.db")
        patch.start()
        self.addCleanup(patch.stop)
        self.con = store.connect()
        self.addCleanup(self.con.close)
        self.seed_bot()

    def bot_row(self, jid, company, title, url="", source="company", verdict=None, reasons="[]", status=None,
                applied_at=None, listed=0):
        """A row the bot made: sent today (listed=0 is a card with buttons, listed=1 only a line in 'Отсеял')."""
        t = store.now()
        job = {"company": company, "title": title, "url": url}
        self.con.execute(
            "insert into jobs (id, key, company, title, url, source, rule, first_seen, sent_at, verdict, reasons,"
            " verdict_at, status, status_at, applied_at, data, listed, closed)"
            " values (?,?,?,?,?,?,'keep',?,?,?,?,?,?,?,?,?,?,0)",
            (jid, store.dedupe_key(job), company, title, url, source, t, t, verdict, reasons,
             t if verdict else None, status, applied_at if status else None, applied_at,
             json.dumps(job, ensure_ascii=False), listed))
        self.con.commit()

    def log(self, jid, *pairs):
        for status, at in pairs:
            self.con.execute("insert into status_log (job_id, status, at) values (?,?,?)", (jid, status, at))
        self.con.commit()

    def seed_bot(self):
        self.bot_row("c1", "Acme", "Junior PM", "https://acme.example/1", "company", "interested",
                     status="applied", applied_at=ago(3))  # card application
        self.log("c1", ("applied", ago(3)))
        self.bot_row("c2", "Beta", "Analyst", "https://beta.example/2", "adzuna", "interested",
                     status="replied", applied_at=ago(4))  # card application, replied
        self.log("c2", ("applied", ago(4)), ("replied", ago(1)))
        self.bot_row("c3", "Gamma", "Sales", "https://gamma.example/3", "linkedin", "not_interested")
        self.bot_row("c4", "Delta", "Coordinator", "https://delta.example/4", "stepstone", "cant_fit",
                     reasons='["skill"]')
        self.bot_row("c5", "Epsilon", "Assistant", "https://eps.example/5", "company")  # in the витрина, no mark
        self.bot_row("c6", "Zeta", "Intern", "https://zeta.example/6", "adzuna", listed=1)  # only a line in 'Отсеял'
        self.bot_row("c7", "Eta", "Researcher", "https://eta.example/7", "company", "interested")  # not applied yet
        self.bot_row("g1", THETA["company"], THETA["title"], THETA["url"], "adzuna", "interested")  # attached below

    def add(self, job, applied_at, **kw):
        jid, outcome = store.add_manual(self.con, job, applied_at, **kw)
        self.con.commit()
        return jid, outcome

    def seed_manual(self):
        """Two manual applications (Ember Studio replied), the bot card 'Growth Analyst' that Misha attached an
        application to, and one manual row he removed again."""
        _, out1 = self.add(ACME, day_ago(2), channel="email")
        _, out2 = self.add(EMBER, day_ago(5), status="replied", status_at=day_ago(1))
        _, out3 = self.add(THETA, day_ago(1), attach_to="g1")
        ghost, _ = self.add(GHOST, day_ago(1))
        self.assertTrue(store.remove_manual(self.con, ghost))
        self.con.commit()
        self.assertEqual([out1, out2, out3], ["created", "created", "attached"])


class Full(Base):
    """The bot's rows plus the manual ones: the state the numbers are checked against."""

    def setUp(self):
        super().setUp()
        self.seed_manual()


class Numbers(Full):
    def test_stats_split_bot_cards_marks_and_applications(self):
        st = report.stats(self.con)
        self.assertEqual(st["cards"], 7)  # c1-c5, c7, g1; c6 is only a list line; manual rows are no cards
        self.assertEqual(st["marked"], 6)  # c1, c2, c3, c4, c7, g1
        self.assertEqual(st["interested"], 4)  # c1, c2, c7, g1
        self.assertEqual(st["verdicts"], {"interested": 4, "not_interested": 1, "cant_fit": 1, "": 1})
        self.assertEqual(st["reasons"]["cant_fit"], {"skill": 1})
        self.assertEqual(st["applied"], 5)  # c1, c2, g1 (attached), Acme Labs, Ember Studio
        self.assertEqual(st["replied"], 2)  # c2, Ember Studio
        self.assertEqual((st["rejected"], st["no_answer"]), (0, 0))
        # Acme Labs, Ember Studio (source 'manual') and the attached card Theta (data.manual_applied, with a status);
        # not the removed row
        self.assertEqual(st["manual"], 3)

    def test_funnel_rows_end_with_the_manual_line(self):
        self.assertEqual(report.funnel_rows(report.stats(self.con)), [
            ("Прислано карточек", 7), ("Интересно", 4), ("Отправил резюме", 5), ("Ответили", 2),
            ("Интервью", 0), ("Оффер", 0), ("Добавлено вручную", 3)])

    def test_stats_command_shows_the_manual_line(self):
        lines = bot.stats_lines(self.con)
        self.assertIn("Добавлено вручную: 3", lines)
        self.assertIn("Отправил резюме: 5", lines)

    def test_removed_row_is_stored_as_removed_and_shown_nowhere(self):
        ghost = self.con.execute("select status, verdict, closed, closed_by from jobs where title=?",
                                 (GHOST["title"],)).fetchone()
        self.assertEqual(tuple(ghost), (None, None, 1, "manual_removed"))
        self.assertNotIn("Ghost", report.build(self.con, today=date.today()))
        self.assertNotIn("Ghost", "\n".join(bot.stats_lines(self.con)))

    def test_broken_data_json_does_not_break_the_counts(self):
        self.con.execute("insert into jobs (id, key, company, title, source, first_seen, data, closed)"
                         " values ('bad', 'bad-key', 'Broken', 'Broken data', 'company', ?, '{not json', 0)",
                         (store.now(),))
        self.con.commit()
        self.assertEqual(report.stats(self.con)["manual"], 3)  # must not raise
        report.build(self.con, today=date.today())  # must not raise
        self.assertIn("Добавлено вручную: 3", digest.weekly_summary(self.con, datetime.now()).split("\n"))


class Page(Full):
    def test_attached_card_whose_status_was_reset_is_neither_counted_nor_marked(self):
        # 'Сбросить' clears the status but leaves data.manual_applied: the row is no application any more
        store.set_status(self.con, "g1", None)
        self.con.commit()
        self.assertEqual(report.stats(self.con)["manual"], 2)
        self.assertIn("Добавлено вручную: 2", digest.weekly_summary(self.con, datetime.now()).split("\n"))
        page = report.build(self.con, today=date.today())
        for seg in rows_with(page, THETA["title"]):
            self.assertNotIn("(вручную)", seg)
        self.assertIn("<h2>Интересно, резюме ещё не отправлено (2)</h2>", page)  # Eta and Theta again
    def test_manual_rows_are_marked_in_the_tables(self):
        page = report.build(self.con, today=date.today())
        for title in (ACME["title"], EMBER["title"], THETA["title"]):
            segs = rows_with(page, title)
            self.assertTrue(segs, title)
            for seg in segs:
                self.assertIn("(вручную)", seg, title)
        for seg in rows_with(page, "Junior PM"):
            self.assertNotIn("(вручную)", seg)

    def test_headings_count_applications_and_bot_marks_separately(self):
        page = report.build(self.con, today=date.today())
        for heading in ("Интересно, резюме ещё не отправлено (1)",  # Eta only: Theta is applied now
                        "Отправлено (5)",  # c1, c2, Theta, Acme Labs, Ember Studio
                        "Компании, которые ответили (2)",  # Beta, Ember Studio
                        "Все отмеченные вакансии (6)"):  # bot marks only: the two manual rows are not in it
            self.assertIn(f"<h2>{heading}</h2>", page)

    def test_sources_table_puts_manual_applications_under_their_own_source(self):
        page = report.build(self.con, today=date.today())
        segs = [s for s in page.split("<tr>") if "<td>вручную</td>" in s]
        self.assertEqual(len(segs), 1)
        # cards, interested, applied
        self.assertEqual(re.findall(r'class="num"[^>]*>(\d+)</td>', segs[0]), ["0", "0", "2"])


class Weekly(Full):
    def lines(self):
        return digest.weekly_summary(self.con, datetime.now()).split("\n")

    def test_weekly_numbers(self):
        lines = self.lines()
        for expected in ("Прислано карточек: 7", "Отмечено: 6, из них Интересно: 4", "Отправил резюме: 5",
                         "Ответили: 2", "Добавлено вручную: 3"):
            self.assertIn(expected, lines)
        self.assertFalse(any("без ответа" in ln for ln in lines))

    def test_manual_line_counts_by_the_day_applied(self):
        self.add(IOTA, day_ago(10))  # sent 10 days ago, typed in today
        lines = self.lines()
        self.assertIn("Добавлено вручную: 3", lines)  # Iota is not this week's application
        self.assertIn("Отправил резюме: 5", lines)
        self.assertIn("Прислано карточек: 7", lines)  # and it is not a card either
        self.assertEqual(report.funnel_rows(report.stats(self.con))[-1], ("Добавлено вручную", 4))


class MetricsUnmoved(Base):
    def snapshot(self):
        today = date.today().isoformat()
        rows = metrics.db_rows(self.con, today, today)
        m = metrics.db_metrics(rows, [today])
        return len(rows), {k: m[k][today] for k in ("new", "kept", "sent", "cards", "verdicts", "closed_new")}

    def test_manual_rows_do_not_move_the_metrics(self):
        before = self.snapshot()
        self.assertEqual(before, (8, {"new": 8, "kept": 8, "sent": 8, "cards": 7, "verdicts": 6, "closed_new": 0}))
        self.seed_manual()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
