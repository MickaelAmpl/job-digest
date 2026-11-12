"""Stage 10 (Misha 03.11): /applied lists the live applications only; rejected and no-answer ones are /rejected, with a
plain last line on /applied that points there. Fake Telegram api and temp DB (from test_bot_tailor.Env); no network."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import store  # noqa: E402
from tests.test_bot_tailor import CHAT, Env  # noqa: E402

LAST = "Отказы и без ответа: {} - /rejected"


class Split(Env):
    def setUp(self):
        super().setUp()
        self.pause, bot.PAUSE = bot.PAUSE, 0
        self.con.execute("delete from jobs")
        self.con.commit()

    def tearDown(self):
        bot.PAUSE = self.pause
        super().tearDown()

    def add(self, jid, status, n=0, company="ACME", title=None, verdict="interested", data="{}"):
        applied = f"2026-10-{10 + n:02d}T10:00:00" if status else None
        self.con.execute("insert into jobs (id, key, company, title, verdict, status, verdict_at, applied_at, score,"
                         " data) values (?,?,?,?,?,?,?,?,?,?)",
                         (jid, jid, company, title or f"Title {jid}", verdict, status, "2026-10-01T10:00:00", applied,
                          7, data))
        self.con.commit()

    def command(self, text):
        bot.handle_message({}, self.con, {"text": text, "chat": {"id": CHAT}})
        return self.api.of("sendMessage")

    def kb(self, params):
        return [b for r in (params.get("reply_markup") or {}).get("inline_keyboard", []) for b in r]

    def text(self, msgs):
        return "\n".join(p["text"] for p in msgs)

    def assert_aligned(self, msgs):
        """Each message: its buttons are the jobs of its own lines, in the same order."""
        for p in msgs:
            ids = [b["callback_data"][2:] for b in self.kb(p)]
            in_text = [line.split("Title ")[1].split("<")[0].split(" ")[0] for line in p["text"].split("\n")
                       if "Title " in line]
            self.assertEqual(ids, in_text)

    def seed_mixed(self):
        self.add("a1", "applied", n=1)
        self.add("r1", "replied", n=2)
        self.add("i1", "interview", n=3)
        self.add("o1", "offer", n=4)
        self.add("j1", "rejected", n=5)
        self.add("n1", "no_answer", n=6)


class AppliedSplit(Split):
    def test_applied_lists_only_the_live_statuses(self):
        self.seed_mixed()
        msgs = self.command("/applied")
        text = self.text(msgs)
        self.assertIn("Отправленные резюме: 4", text)
        for jid in ("Title a1", "Title r1", "Title i1", "Title o1"):
            self.assertIn(jid, text)
        for jid in ("Title j1", "Title n1"):
            self.assertNotIn(jid, text)
        self.assertNotIn("Отказ (", text)
        self.assertNotIn("Нет ответа (", text)

    def test_the_last_line_counts_rejected_and_no_answer_and_has_no_button(self):
        self.seed_mixed()
        msgs = self.command("/applied")
        self.assertEqual(msgs[-1]["text"].split("\n")[-1], LAST.format(2))
        self.assertEqual(len(self.kb(msgs[-1])), 4)  # the four live cards only: the last line adds none
        self.assert_aligned(msgs)

    def test_no_last_line_when_nothing_was_rejected(self):
        self.add("a1", "applied")
        self.add("r1", "replied", n=1)
        msgs = self.command("/applied")
        self.assertNotIn("Отказы и без ответа", self.text(msgs))

    def test_only_rejected_rows_say_no_live_application_and_point_to_rejected(self):
        self.add("j1", "rejected")
        msgs = self.command("/applied")
        self.assertEqual(msgs[-1]["text"].split("\n")[0], "Живых откликов нет.")
        self.assertEqual(msgs[-1]["text"].split("\n")[-1], LAST.format(1))
        self.assertNotIn("reply_markup", msgs[-1])

    def test_applied_empty_state_is_unchanged_when_nothing_was_sent(self):
        msgs = self.command("/applied")
        self.assertEqual(msgs[-1]["text"],
                         "Пока ни одного отправленного резюме. Отметь «Отправил резюме» под карточкой с «Интересно».")
        self.assertNotIn("reply_markup", msgs[-1])

    def test_live_groups_keep_the_chain_order_and_the_manual_mark(self):
        self.add("o1", "offer", n=1)
        self.add("a1", "applied", n=2, data=json.dumps({"manual_applied": True}))
        msgs = self.command("/applied")
        text = self.text(msgs)
        self.assertLess(text.index("Отправил резюме (1)"), text.index("Оффер (1)"))
        self.assertIn("(вручную)", text)


class Rejected(Split):
    def test_rejected_lists_rejected_then_no_answer_in_the_same_line_format(self):
        self.seed_mixed()
        msgs = self.command("/rejected")
        text = self.text(msgs)
        self.assertEqual(msgs[0]["text"].split("\n")[0], "<b>Отказы и без ответа: 2</b>")
        self.assertIn("Отказ (1)", text)
        self.assertIn("Нет ответа (1)", text)
        self.assertLess(text.index("Отказ (1)"), text.index("Нет ответа (1)"))
        self.assertNotIn("Title a1", text)  # live ones are not here
        line = [ln for ln in text.split("\n") if "Title j1" in ln][0]
        self.assertIn("отправил 15 октября", line)  # the same 'отправил <date>, N дней назад' as on /applied
        self.assert_aligned(msgs)

    def test_rejected_buttons_are_the_card_buttons_in_line_order(self):
        self.seed_mixed()
        msgs = self.command("/rejected")
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], ["k:j1", "k:n1"])

    def test_rejected_empty_state(self):
        self.add("a1", "applied")
        msgs = self.command("/rejected")
        self.assertEqual(msgs[-1]["text"], "Отказов нет.")
        self.assertNotIn("reply_markup", msgs[-1])

    def test_rejected_limit_40_with_the_rest_as_more(self):
        for i in range(45):
            self.add(f"j{i:02d}", "rejected" if i % 2 else "no_answer", n=i % 20)
        msgs = self.command("/rejected")
        self.assertEqual(sum(len(self.kb(p)) for p in msgs), 40)
        self.assertIn("ещё 5", msgs[-1]["text"])
        self.assert_aligned(msgs)

    def test_a_card_button_from_rejected_brings_the_card_back(self):
        self.add("j1", "rejected")
        self.press("k:j1")
        sends = self.api.of("sendMessage")
        self.assertEqual(sends[-1]["text"], bot.collapsed_text(self.con.execute(
            "select * from jobs where id='j1'").fetchone()))

    def test_rejected_is_a_command_and_in_the_menu_and_help(self):
        self.assertIn(("rejected", "отказы и без ответа"), bot.COMMANDS)
        self.assertIn("/rejected - отказы и без ответа", bot.HELP)
        self.assertEqual(bot.command_of("/Rejected@job_bot extra"), "/rejected")
        self.assertEqual(bot.command_of("/rejected"), "/rejected")

    def test_stats_is_unchanged_by_the_split(self):
        self.seed_mixed()
        p = self.command("/stats")[-1]
        self.assertNotIn("reply_markup", p)
        self.assertIn("Воронка", p["text"])


if __name__ == "__main__":
    unittest.main()
