"""/interesting: a closed vacancy (its posting is gone) is not a line with a dead link; it is counted in one plain
last line. A duplicate is neither listed nor counted. Fake Telegram api and temp DB (from test_bot_tailor.Env)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
from tests.test_bot_tailor import CHAT, Env  # noqa: E402

GONE = "Закрылись: {} (вакансии больше нет на сайте)"


class Closed(Env):
    def setUp(self):
        super().setUp()
        self.pause, bot.PAUSE = bot.PAUSE, 0
        self.con.execute("delete from jobs")
        self.con.commit()

    def tearDown(self):
        bot.PAUSE = self.pause
        super().tearDown()

    def add(self, jid, verdict="interested", status=None, n=0, closed=0, closed_by=None, score=7):
        self.con.execute("insert into jobs (id, key, company, title, verdict, status, verdict_at, score, closed,"
                         " closed_by, data) values (?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, jid, "ACME", f"Title {jid}", verdict, status, f"2026-10-01T10:{n:02d}:00", score,
                          closed, closed_by, "{}"))
        self.con.commit()

    def command(self):
        bot.handle_message({}, self.con, {"text": "/interesting", "chat": {"id": CHAT}})
        return self.api.of("sendMessage")

    def kb(self, params):
        return [b for r in (params.get("reply_markup") or {}).get("inline_keyboard", []) for b in r]

    def assert_aligned(self, msgs):
        """Every button is the job of the line it sits under: k:<id> with 'Title <id>' in the same message."""
        for p in msgs:
            ids = [b["callback_data"][2:] for b in self.kb(p)]
            in_text = [line.split("Title ")[1].split("<")[0].split(" ")[0] for line in p["text"].split("\n")
                       if "Title " in line]
            self.assertEqual(ids, in_text)


class ClosedRows(Closed):
    def test_open_row_is_listed_with_its_button(self):
        self.add("o1", n=1)
        msgs = self.command()
        self.assertEqual(len(msgs), 1)
        self.assertIn("Интересно, резюме ещё не отправлено: 1", msgs[0]["text"])
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:o1"])
        self.assertNotIn("Закрылись", msgs[0]["text"])

    def test_closed_row_is_not_listed_and_counted_in_the_last_line(self):
        self.add("o1", n=1)
        self.add("c1", n=2, closed=1, closed_by="enrich")
        msgs = self.command()
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:o1"])
        self.assertNotIn("Title c1", msgs[0]["text"])
        self.assertIn("Интересно, резюме ещё не отправлено: 1", msgs[0]["text"])
        last = msgs[-1]["text"].split("\n")[-1]
        self.assertEqual(last, GONE.format(1))
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:o1"])  # the last line adds no button

    def test_feed_and_missing_closed_by_count_as_gone(self):
        self.add("c1", closed=1, closed_by="feed")
        self.add("c2", closed=1, closed_by=None)
        self.add("c3", closed=1, closed_by="enrich")
        msgs = self.command()
        self.assertIn(GONE.format(3), msgs[-1]["text"])
        self.assertNotIn("Title c", msgs[0]["text"])

    def test_duplicate_is_neither_listed_nor_counted(self):
        self.add("o1", n=1)
        self.add("d1", n=2, closed=1, closed_by="duplicate")
        msgs = self.command()
        self.assertNotIn("Title d1", json_text(msgs))
        self.assertNotIn("Закрылись", json_text(msgs))
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:o1"])

    def test_only_closed_rows_give_the_empty_line_and_the_count(self):
        self.add("c1", closed=1, closed_by="enrich")
        msgs = self.command()
        self.assertIn("Нет вакансий с отметкой «Интересно» без отправленного резюме.", msgs[0]["text"])
        self.assertEqual(msgs[-1]["text"].split("\n")[-1], GONE.format(1))
        self.assertNotIn("reply_markup", msgs[-1])  # no open line, so no keyboard at all

    def test_the_line_text_of_a_closed_job_is_not_a_button(self):
        self.add("c1", closed=1, closed_by="enrich")
        self.add("o1", n=1)
        msgs = self.command()
        self.assertEqual(len(self.kb(msgs[0])), 1)
        self.assert_aligned(msgs)

    def test_status_set_and_other_verdicts_are_not_counted(self):
        self.add("a1", status="applied", closed=1, closed_by="enrich")
        self.add("n1", verdict="not_interested", closed=1, closed_by="enrich")
        self.add("o1", n=1)
        msgs = self.command()
        self.assertNotIn("Закрылись", json_text(msgs))
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:o1"])


class Limits(Closed):
    def test_buttons_stay_aligned_with_the_limit_and_the_last_line_is_the_count(self):
        for i in range(32):
            self.add(f"o{i:02d}", n=i % 60)
        for i in range(3):
            self.add(f"c{i}", closed=1, closed_by="feed", n=i)
        lines, buttons = bot.interesting_list(self.con)
        self.assertEqual(len(lines), len(buttons))
        self.assertEqual(sum(1 for b in buttons if b), 30)
        self.assertIn("ещё 2", lines[-2])
        self.assertEqual(lines[-1], GONE.format(3))
        self.assertIsNone(buttons[-1])
        msgs = self.command()
        self.assertEqual(sum(len(self.kb(p)) for p in msgs), 30)
        self.assertTrue(msgs[-1]["text"].endswith(GONE.format(3)))

    def test_order_is_by_the_mark_as_before(self):
        self.add("late", n=9)
        self.add("early", n=1)
        self.add("gone", n=0, closed=1, closed_by="enrich")
        msgs = self.command()
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:early", "k:late"])


def json_text(msgs):
    return "\n".join(p["text"] for p in msgs)


if __name__ == "__main__":
    unittest.main()
