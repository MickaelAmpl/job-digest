"""Stage 9: 'Вернуть' - o:<job_id> on the Sunday list of vacancies the weekly liveness check closed brings one back to
'Интересно'. Only a row the check closed (closed_by 'liveness') can be reopened; a second press is harmless. Fake
Telegram api and temp DB (from test_bot_tailor.Env); no network."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
from tests.test_bot_tailor import CHAT, Env  # noqa: E402

LIVENESS = {"at": "2026-10-25T06:30:00", "reason": "Sorry, this job has expired", "url": "https://x.at/j/1"}


class Reopen(Env):
    def setUp(self):
        super().setUp()
        self.con.execute("delete from jobs")
        self.con.commit()

    def add(self, jid, closed=1, closed_by="liveness", data=None):
        data = {"liveness": dict(LIVENESS)} if data is None else data
        self.con.execute("insert into jobs (id, key, company, title, verdict, status, verdict_at, closed, closed_by, data)"
                         " values (?,?,?,?,?,?,?,?,?,?)",
                         (jid, jid, "ACME", f"Title {jid}", "interested", None, "2026-10-01T10:00:00", closed,
                          closed_by, json.dumps(data, ensure_ascii=False)))
        self.con.commit()

    def job(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def toast(self):
        return self.api.of("answerCallbackQuery")[-1]["text"]

    def list_button(self, jid):
        return {"text": f"ACME · Title {jid}", "callback_data": f"o:{jid}"}


class Reopening(Reopen):
    def test_reopen_brings_the_row_back_into_interesting(self):
        self.add("c1")
        self.press("o:c1", markup=[[self.list_button("c1")]])
        row = self.job("c1")
        self.assertEqual((row["closed"], row["closed_by"]), (0, None))
        self.assertEqual(self.toast(), "Вернул в «Интересно»")
        lines, buttons = bot.interesting_list(self.con)
        self.assertTrue(any("Title c1" in line for line in lines))
        self.assertIn("k:c1", [b["callback_data"] for b in buttons if b])

    def test_the_rest_of_data_liveness_stays_and_reopened_at_is_set(self):
        self.add("c1")
        self.press("o:c1")
        liveness = json.loads(self.job("c1")["data"])["liveness"]
        self.assertEqual({k: liveness[k] for k in LIVENESS}, LIVENESS)  # at, reason, url kept
        self.assertTrue(liveness["reopened_at"])  # the weekly check skips this row from now on

    def test_second_press_is_harmless_and_does_not_rewrite_reopened_at(self):
        self.add("c1")
        self.press("o:c1")
        first = json.loads(self.job("c1")["data"])["liveness"]["reopened_at"]
        self.press("o:c1")
        row = self.job("c1")
        self.assertEqual((row["closed"], row["closed_by"]), (0, None))
        self.assertEqual(self.toast(), "Уже вернул в «Интересно»")
        self.assertEqual(json.loads(row["data"])["liveness"]["reopened_at"], first)

    def test_the_pressed_button_leaves_the_message_and_the_others_stay(self):
        self.add("c1")
        self.add("c2")
        self.press("o:c1", markup=[[self.list_button("c1")], [self.list_button("c2")]])
        edit = self.api.of("editMessageReplyMarkup")[-1]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"], [[self.list_button("c2")]])

    def test_the_last_button_leaves_an_empty_keyboard(self):
        self.add("c1")
        self.press("o:c1", markup=[[self.list_button("c1")]])
        self.assertEqual(self.api.of("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"], [])


class Refused(Reopen):
    def test_a_row_the_check_did_not_close_is_refused(self):
        for jid, closed, closed_by in (("e1", 1, "enrich"), ("f1", 1, "feed"), ("o1", 0, None), ("d1", 1, "duplicate")):
            self.add(jid, closed=closed, closed_by=closed_by, data={})
            self.press(f"o:{jid}", markup=[[self.list_button(jid)]])
            self.assertEqual(self.toast(), "Эту вакансию закрыл не я - не трогаю", jid)
            row = self.job(jid)
            self.assertEqual((row["closed"], row["closed_by"]), (closed, closed_by), jid)
        self.assertEqual(self.api.of("editMessageReplyMarkup"), [])  # refused: the message is left alone

    def test_an_unknown_job_is_not_found(self):
        self.press("o:nope")
        self.assertEqual(self.toast(), "Эта вакансия не найдена в базе")

    def test_a_reopened_row_is_not_closed_again_by_the_check_and_stays_open(self):
        self.add("c1")
        self.press("o:c1")
        row = self.job("c1")
        self.assertEqual((row["closed"], row["closed_by"]), (0, None))
        self.assertIn("reopened_at", json.loads(row["data"])["liveness"])


if __name__ == "__main__":
    unittest.main()
