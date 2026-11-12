"""Stage 9 (liveness) in the Sunday send, digest.py: the block 'Проверил «Интересно»' in the weekly summary, the
'Вернуть' message with one button per vacancy the weekly check closed, and the once-per-Sunday guard. A temp DB with
the real schema and the liveness_checks table; no network (Telegram is a fake), no real out/ folder."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import digest  # noqa: E402
import store  # noqa: E402

SUNDAY = date(2026, 11, 1)  # a Sunday: the weekly summary goes out with the morning run
TABLE = """create table if not exists liveness_checks (
  id integer primary key autoincrement, job_id text not null, checked_at text not null,
  result text not null, reason text, url text);
create index if not exists liveness_checks_at on liveness_checks(checked_at);"""


class FakeTelegram:
    """Records (text, buttons) of every message digest sends; answers ok like Telegram."""

    def __init__(self):
        self.sent = []

    def __call__(self, req, timeout=None):
        form = urllib.parse.parse_qs(req.data.decode(), keep_blank_values=True)
        markup = json.loads(form["reply_markup"][0])["inline_keyboard"] if "reply_markup" in form else None
        self.sent.append((form["text"][0], markup))
        return io.BytesIO(b'{"ok": true}')


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "data").mkdir()
        (d / "out").mkdir()
        for p in (mock.patch.object(store, "DB", d / "data" / "state.db"),
                  mock.patch.object(store, "OUT", d / "out")):
            p.start()
            self.addCleanup(p.stop)
        self.con = store.connect()
        self.addCleanup(self.con.close)
        self.con.executescript(TABLE)
        # the moment the Sunday run would have: the checks are a few hours before it
        self.ref = datetime.combine(SUNDAY, datetime.now().time())

    def job(self, jid, company, title, url="", closed_by=None):
        self.con.execute("insert into jobs (id, key, company, title, url, source, first_seen, verdict, closed,"
                         " closed_by, data) values (?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, f"{company}|{title}|{jid}", company, title, url, "company", store.now(), "interested",
                          1 if closed_by else 0, closed_by, json.dumps({"title": title, "company": company})))
        self.con.commit()

    def check(self, jid, result, reason="", url="", hours_before=2):
        at = (self.ref - timedelta(hours=hours_before)).isoformat(timespec="seconds")
        self.con.execute("insert into liveness_checks (job_id, checked_at, result, reason, url) values (?,?,?,?,?)",
                         (jid, at, result, reason, url))
        self.con.commit()

    def card(self, jid="n1"):
        """A scored vacancy the morning digest has not sent yet (a normal card, not a liveness row)."""
        score = {"score": 70, "why_ru": "ok", "german": "none", "hours": "part-time", "level": "entry", "skill_gaps": []}
        self.con.execute("insert into jobs (id, key, company, title, url, source, first_seen, scored_at, score,"
                         " score_json, rule, has_desc, data, verdict, closed) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                         (jid, f"Newco|Fresh role|{jid}", "Newco", "Fresh role", "https://newco.example/1", "company",
                          store.now(), store.now(), 70, json.dumps(score), "keep", 1,
                          json.dumps({"title": "Fresh role", "company": "Newco", "url": "https://newco.example/1"}), None))
        self.con.commit()

    def run_digest(self, tg, argv=("--today=2026-11-01",)):
        env = {"TELEGRAM_CHAT_ID": "1", "TELEGRAM_BOT_TOKEN": "111:SECRET"}
        with mock.patch.object(digest, "load_env", lambda: dict(env)), \
                mock.patch.object(digest.urllib.request, "urlopen", tg), mock.patch.object(digest.time, "sleep"), \
                mock.patch.object(sys, "argv", ["digest.py", *argv]), contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            digest._dead[0] = 0
            rc = digest.main()
        return rc, out.getvalue()


class Week(Base):
    """One Sunday's check of 'Интересно': 2 closed by the check, 2 'check yourself', 1 open, 1 reopened by Misha."""

    def setUp(self):
        super().setUp()
        self.job("a1", "Sportradar", "Sales Lead", "https://sportradar.example/jobs/1", closed_by="liveness")
        self.check("a1", "closed", "this job has expired", "https://sportradar.example/jobs/1")
        self.job("a2", "UNIQA", "Analyst & <Ops>", "https://uniqa.example/job/2", closed_by="liveness")
        self.check("a2", "closed", "Diese Stelle wurde leider bereits besetzt", "https://uniqa.example/job/2")
        self.job("u1", "TOWA", "Engineer", "https://towa.example/careers/3")
        self.check("u1", "unknown", "сайт не пустил (403)", "https://towa.example/careers/3")
        self.job("u2", "LinkedIn Co", "Product")  # no link at all
        self.check("u2", "unknown", "нет ссылки")
        self.job("o1", "Acme", "Open role", "https://acme.example/open")
        self.check("o1", "open")
        self.job("s1", "Reopened Ltd", "Kept role", "https://kept.example/9")
        self.check("s1", "skipped", "вернул Миша")


class Block(Week):
    def test_numbers_and_lines(self):
        lines = digest.weekly_summary(self.con, self.ref).split("\n")
        self.assertIn("Проверил «Интересно»: 6. Закрылись: 2. Не смог проверить: 2.", lines)
        self.assertIn('закрылась: <a href="https://sportradar.example/jobs/1">Sales Lead</a> · Sportradar'
                      " (this job has expired)", lines)
        self.assertIn('закрылась: <a href="https://uniqa.example/job/2">Analyst &amp; &lt;Ops&gt;</a> · UNIQA'
                      " (Diese Stelle wurde leider bereits besetzt)", lines)
        self.assertIn('проверь сам: <a href="https://towa.example/careers/3">Engineer</a> · TOWA'
                      " (сайт не пустил (403))", lines)
        self.assertIn("проверь сам: Product · LinkedIn Co (нет ссылки)", lines)  # no link: the title is plain text
        self.assertEqual(sum(ln.startswith("закрылась: ") for ln in lines), 2)
        self.assertEqual(sum(ln.startswith("проверь сам: ") for ln in lines), 2)

    def test_open_and_skipped_are_counted_but_not_listed(self):
        text = digest.weekly_summary(self.con, self.ref)
        self.assertNotIn("Open role", text)
        self.assertNotIn("Kept role", text)


class NoBlock(Base):
    def test_no_checks_no_block(self):
        self.assertNotIn("Проверил", digest.weekly_summary(self.con, self.ref))

    def test_checks_older_than_a_day_no_block(self):
        self.job("old", "Old Co", "Old role", "https://old.example/1")
        self.check("old", "closed", "this job has expired", "https://old.example/1", hours_before=30)
        self.assertNotIn("Проверил", digest.weekly_summary(self.con, self.ref))

    def test_no_liveness_table_reads_as_no_checks(self):
        self.con.execute("drop table liveness_checks")
        self.con.commit()
        self.assertNotIn("Проверил", digest.weekly_summary(self.con, self.ref))  # must not raise


class Limits(Base):
    def test_long_block_is_capped_and_fits_one_message(self):
        for i in range(12):
            jid = f"c{i:02d}"
            self.job(jid, f"Company {i} " + "x" * 60, "T" * 120, closed_by="liveness")
            self.check(jid, "closed", "this job has expired " + "r" * 80, "https://example.com/" + "p" * 280)
        for i in range(12):
            jid = f"u{i:02d}"
            self.job(jid, "Co " + "y" * 40, "U" * 120, "https://example.com/u/" + "q" * 400)  # too long for a link
            self.check(jid, "unknown", "сайт не пустил (403)", "https://example.com/u/" + "q" * 400)
        text = digest.weekly_summary(self.con, self.ref)
        lines = text.split("\n")
        self.assertIn("Проверил «Интересно»: 24. Закрылись: 12. Не смог проверить: 12.", lines)
        self.assertLessEqual(len(text), 4096)
        self.assertLessEqual(sum(ln.startswith("закрылась: ") for ln in lines), 10)
        self.assertLessEqual(sum(ln.startswith("проверь сам: ") for ln in lines), 10)
        self.assertIn("не поместились", text)  # the cut is said, not silent
        self.assertNotIn("https://example.com/u/", text)  # a link over 300 characters is left out


class Buttons(Week):
    def test_one_button_per_closed_vacancy(self):
        text, buttons = digest.closed_message(self.con, self.ref)
        self.assertIn("Закрылись из «Интересно»", text)
        flat = [b for row in buttons for b in row]
        self.assertEqual([b["callback_data"] for b in flat], ["o:a1", "o:a2"])
        self.assertTrue(all(b["text"].startswith("Вернуть: ") for b in flat))
        for b in flat:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)
            self.assertLessEqual(len(b["text"]), 64)

    def test_reopened_vacancy_gets_no_button(self):
        self.con.execute("update jobs set closed=0, closed_by=null where id='a1'")
        self.con.commit()
        _, buttons = digest.closed_message(self.con, self.ref)
        self.assertEqual([b["callback_data"] for row in buttons for b in row], ["o:a2"])

    def test_no_closed_vacancy_no_message(self):
        self.con.execute("delete from liveness_checks where result='closed'")
        self.con.commit()
        self.assertIsNone(digest.closed_message(self.con, self.ref))

    def test_buttons_are_capped_and_labels_too(self):
        for i in range(12):
            self.job(f"b{i:02d}", "Company " + "z" * 150, "Role " + "w" * 150, closed_by="liveness")
            self.check(f"b{i:02d}", "closed", "this job has expired")
        _, buttons = digest.closed_message(self.con, self.ref)
        flat = [b for row in buttons for b in row]
        self.assertEqual(len(flat), 10)
        self.assertTrue(all(len(b["text"]) <= 64 for b in flat))


class Sunday(Week):
    def test_summary_and_buttons_go_out_once(self):
        tg = FakeTelegram()
        rc, _ = self.run_digest(tg)
        self.assertEqual(rc, 0)
        summaries = [m for m in tg.sent if "Итоги недели" in m[0]]
        closed = [m for m in tg.sent if "Закрылись из «Интересно»" in m[0]]
        self.assertEqual(len(summaries), 1)
        self.assertIn("Проверил «Интересно»: 6. Закрылись: 2. Не смог проверить: 2.", summaries[0][0])
        self.assertEqual(len(closed), 1)
        self.assertEqual([b["callback_data"] for row in closed[0][1] for b in row], ["o:a1", "o:a2"])
        self.assertEqual(tg.sent.index(closed[0]), tg.sent.index(summaries[0]) + 1)  # right after the summary
        tg.sent.clear()
        self.run_digest(tg)  # a second run the same Sunday repeats neither
        self.assertFalse([m for m in tg.sent if "Итоги недели" in m[0] or "Закрылись из" in m[0]])

    def test_dry_run_prints_the_buttons_and_sends_nothing(self):
        tg = FakeTelegram()
        rc, out = self.run_digest(tg, argv=("--today=2026-11-01", "--dry-run"))
        self.assertEqual(rc, 0)
        self.assertIn("Вернуть: ", out)
        self.assertEqual(tg.sent, [])


class SundayRun(Week):
    """digest.main() on a Sunday with a real card to deliver and liveness closures: the run must exit 0 and count
    the card as delivered (a closed-list mix-up once made it '1 of 3', exit 3, and a false alert)."""

    def setUp(self):
        super().setUp()
        self.card()

    def test_run_delivers_everything_and_sends_summary_and_buttons_once(self):
        tg = FakeTelegram()
        rc, out = self.run_digest(tg)
        self.assertEqual(rc, 0)
        self.assertIn("jobs delivered: 1 of 1,", out)
        self.assertEqual(len([m for m in tg.sent if "Итоги недели" in m[0]]), 1)
        closed = [m for m in tg.sent if "Закрылись из «Интересно»" in m[0]]
        self.assertEqual(len(closed), 1)
        self.assertEqual([b["callback_data"] for row in closed[0][1] for b in row], ["o:a1", "o:a2"])
        self.assertFalse([m for m in tg.sent if "не дошла полностью" in m[0]])


class SundayRunNoClosures(Base):
    """A Sunday with checks but nothing closed: no second message, the run still exits 0 and delivers its card."""

    def setUp(self):
        super().setUp()
        self.job("o1", "Acme", "Open role", "https://acme.example/open")
        self.check("o1", "open")
        self.check("o1", "unknown", "сайт не пустил (403)", "https://acme.example/open", hours_before=1)
        self.card()

    def test_run_exits_zero_and_sends_no_buttons_message(self):
        tg = FakeTelegram()
        rc, out = self.run_digest(tg)
        self.assertEqual(rc, 0)
        self.assertIn("jobs delivered: 1 of 1,", out)
        self.assertEqual(len([m for m in tg.sent if "Итоги недели" in m[0]]), 1)
        self.assertEqual([m for m in tg.sent if "Закрылись из" in m[0]], [])
        self.assertTrue(any("Fresh role" in m[0] for m in tg.sent))


class SundayRunNoChecks(Base):
    """A Sunday with no liveness rows at all (the check did not run): the run is the ordinary one."""

    def setUp(self):
        super().setUp()
        self.card()

    def test_run_exits_zero(self):
        tg = FakeTelegram()
        rc, out = self.run_digest(tg)
        self.assertEqual(rc, 0)
        self.assertIn("jobs delivered: 1 of 1,", out)
        self.assertNotIn("Проверил", "".join(m[0] for m in tg.sent))


class SundayDry(Week):
    def test_dry_run_counts_the_card_once_and_prints_the_buttons(self):
        self.card()
        rc, out = self.run_digest(FakeTelegram(), argv=("--today=2026-11-01", "--dry-run"))
        self.assertEqual(rc, 0)
        self.assertIn("jobs delivered: 0 of 1,", out)  # a dry run delivers nothing, the card is the one total
        self.assertIn("Вернуть: Sportradar · Sales Lead", out)


class ReportTodo(Week):
    """Report table (a) 'Интересно, резюме ещё не отправлено' lists what /interesting lists: not the closed ones."""

    def test_closed_vacancies_are_not_in_the_todo_table(self):
        page = digest_report_page(self.con)
        self.assertIn("<h2>Интересно, резюме ещё не отправлено (4)</h2>", page)  # u1, u2, o1, s1 - not a1, a2
        section = page.split("<h2>Интересно, резюме ещё не отправлено")[1].split("<h2>")[0]
        self.assertIn("Engineer", section)
        self.assertNotIn("Sales Lead", section)
        self.assertNotIn("Analyst &amp; &lt;Ops&gt;", section)

    def test_report_todo_count_matches_interesting(self):
        import bot
        lines, _ = bot.interesting_list(self.con)
        self.assertEqual(lines[0], "<b>Интересно, резюме ещё не отправлено: 4</b>")
        self.assertIn("<h2>Интересно, резюме ещё не отправлено (4)</h2>", digest_report_page(self.con))


def digest_report_page(con):
    import report
    return report.build(con, today=SUNDAY)


if __name__ == "__main__":
    unittest.main()
