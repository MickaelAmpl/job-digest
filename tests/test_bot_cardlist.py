"""/interesting and /applied carry a button per job (k:<id>) that brings the job's card back as a new message,
with the fake Telegram api from test_bot_tailor (no network)."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import store  # noqa: E402
from tests.test_bot_tailor import CHAT, Env  # noqa: E402

LONG = "Raiffeisen Bank International (RBI) — Group Product Owner Data Platform – Vienna"


class Base(Env):
    def setUp(self):
        super().setUp()
        self.pause, bot.PAUSE = bot.PAUSE, 0
        self.con.execute("delete from jobs")
        self.con.commit()

    def tearDown(self):
        bot.PAUSE = self.pause
        super().tearDown()

    def add(self, jid, verdict="interested", status=None, n=0, title=None, company="ACME", url=None):
        self.con.execute("insert into jobs (id, key, company, title, verdict, status, verdict_at, applied_at, score,"
                         " url, data) values (?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, jid, company, title or f"Title {jid}", verdict, status, f"2026-10-01T10:{n:02d}:00",
                          f"2026-10-02T10:{n:02d}:00" if status else None, 7, url, "{}"))
        self.con.commit()

    def command(self, text):
        bot.handle_message({}, self.con, {"text": text, "chat": {"id": CHAT}})
        return self.api.of("sendMessage")

    def kb(self, params):
        return [b for r in (params.get("reply_markup") or {}).get("inline_keyboard", []) for b in r]


class Lists(Base):
    def assert_aligned(self, msgs):
        """Every message: its buttons are the jobs of its own lines, in the same order."""
        for p in msgs:
            ids = [b["callback_data"][2:] for b in self.kb(p)]
            in_text = [line.split("Title ")[1].split("<")[0].split(" ")[0] for line in p["text"].split("\n")
                       if "Title " in line]
            self.assertEqual(ids, in_text)

    def test_interesting_has_one_button_per_job_in_order(self):
        for i in range(5):
            self.add(f"i{i}", n=10 - i)  # verdict_at descending: the list sorts them oldest first
        self.add("a0", status="applied")
        self.add("x0", verdict="not_interested")
        msgs = self.command("/interesting")
        self.assertEqual(len(msgs), 1)
        self.assertEqual([b["callback_data"] for b in self.kb(msgs[0])], [f"k:i{i}" for i in range(4, -1, -1)])
        self.assertEqual(self.kb(msgs[0])[0]["text"], "ACME · Title i4")
        self.assert_aligned(msgs)

    def test_interesting_limit_30(self):
        for i in range(35):
            self.add(f"i{i:02d}", n=i)
        msgs = self.command("/interesting")
        self.assertEqual(sum(len(self.kb(p)) for p in msgs), 30)
        self.assertIn("ещё 5", msgs[-1]["text"])
        self.assert_aligned(msgs)

    def test_applied_limit_40_grouped(self):
        # live statuses only: rejected and no-answer rows are /rejected since stage 10 (see test_bot_applied_split)
        for i in range(45):
            self.add(f"a{i:02d}", status=bot.LIVE_STATUSES[i % len(bot.LIVE_STATUSES)], n=i)
        self.add("i0")
        msgs = self.command("/applied")
        self.assertEqual(sum(len(self.kb(p)) for p in msgs), 40)
        self.assertNotIn("k:i0", json.dumps([self.kb(p) for p in msgs]))
        self.assertIn("ещё 5", msgs[-1]["text"])
        self.assert_aligned(msgs)

    def test_button_text_and_callback_data_fit(self):
        jid = store.job_id("some company#" + "a very long title " * 10)
        self.add(jid, company=LONG, title=LONG)
        b = self.kb(self.command("/interesting")[0])[0]
        self.assertLessEqual(len(b["text"]), bot.BUTTON_TEXT)
        self.assertTrue(b["text"].endswith("..."))
        self.assertNotIn("—", b["text"])
        self.assertNotIn("–", b["text"])
        self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_empty_lists_have_no_keyboard(self):
        for cmd in ("/interesting", "/applied"):
            p = self.command(cmd)[-1]
            self.assertNotIn("reply_markup", p)

    def test_stats_unchanged_no_keyboard(self):
        self.add("i0")
        p = self.command("/stats")[-1]
        self.assertNotIn("reply_markup", p)
        self.assertNotIn("parse_mode", p)


class Split(Base):
    def test_long_list_split_buttons_follow_their_lines(self):
        for i in range(30):
            self.add(f"i{i:02d}", n=i, title=f"Title i{i:02d} " + "x" * 200, url="https://example.com/" + "p" * 150)
        msgs = self.command("/interesting")
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(p["text"]) <= 4096 for p in msgs))
        self.assertEqual(sum(len(self.kb(p)) for p in msgs), 30)
        for p in msgs:
            ids = [b["callback_data"][2:] for b in self.kb(p)]
            in_text = [line.split("Title ")[1][:3] for line in p["text"].split("\n") if "Title " in line]
            self.assertEqual(ids, in_text)

    def test_100_button_cap(self):
        lines = [f"line {i}" for i in range(250)]
        buttons = [{"text": f"b{i}", "callback_data": f"k:{i}"} for i in range(250)]
        bot.send_long({}, CHAT, ["header"] + lines, buttons=[None] + buttons)
        msgs = self.api.of("sendMessage")
        self.assertEqual([len(self.kb(p)) for p in msgs], [100, 100, 50])
        for p in msgs:
            shown = [line.split()[1] for line in p["text"].split("\n") if line.startswith("line ")]
            self.assertEqual([b["callback_data"][2:] for b in self.kb(p)], shown)

    def test_plain_lines_split_as_before(self):
        bot.send_long({}, CHAT, ["y" * 3000, "z" * 3000], html=False)
        msgs = self.api.of("sendMessage")
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all("reply_markup" not in p and "parse_mode" not in p for p in msgs))


class Resend(Base):
    def k(self, jid):
        self.api.calls.clear()
        self.press(f"k:{jid}", message_id=55)
        return self.api.calls

    def sent_card(self, calls):
        self.assertEqual(calls[0][0], "answerCallbackQuery")
        sends = [p for m, p in calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertFalse([m for m, _ in calls if m.startswith("edit")])  # the old message is left alone
        return sends[0]

    def test_interested_card_with_tailor_and_status_buttons(self):
        self.add("i0", url="https://example.com/job/1")
        p = self.sent_card(self.k("i0"))
        row = self.row("i0")
        self.assertEqual(p["text"], bot.collapsed_text(row))
        self.assertEqual(p["reply_markup"]["inline_keyboard"], bot.collapsed_markup(row, "i0"))
        flat = json.dumps(p["reply_markup"], ensure_ascii=False)
        for label in ("Адаптировать резюме", "Отправил резюме", "Открыть объявление", "Изменить"):
            self.assertIn(label, flat)
        self.assertEqual(row["card_msg_id"], self.api.next_id)

    def test_applied_card(self):
        self.add("a0", status="applied")
        p = self.sent_card(self.k("a0"))
        row = self.row("a0")
        self.assertEqual(p["reply_markup"]["inline_keyboard"], bot.collapsed_markup(row, "a0"))
        flat = json.dumps(p["reply_markup"], ensure_ascii=False)
        for label in ("Адаптировать резюме", "Ответили", "Изменить статус"):
            self.assertIn(label, flat)
        self.assertTrue(p["text"].startswith("<i>Отправил резюме"))
        self.assertEqual(row["card_msg_id"], self.api.next_id)

    def test_other_verdicts(self):
        self.add("x0", verdict="not_interested")
        p = self.sent_card(self.k("x0"))
        self.assertEqual(p["reply_markup"]["inline_keyboard"], [[{"text": "Изменить", "callback_data": "e:x0"}]])
        self.assertNotIn("c:tailor", json.dumps(p))
        self.add("n0", verdict=None)
        p = self.sent_card(self.k("n0"))
        self.assertIn("v:interested:n0", json.dumps(p["reply_markup"]))

    def test_unknown_id(self):
        calls = self.k("nope")
        self.assertEqual(calls[0], ("answerCallbackQuery", {"callback_query_id": "cq", "text": "Эта вакансия не найдена в базе"}))
        self.assertEqual(self.api.texts(), ["Эта вакансия не найдена в базе"])

    def test_bad_link_retried_without_link_row(self):
        self.add("i0", url="https://example.com/job/1")
        real = self.api

        def flaky(env, method, **params):
            if method == "sendMessage" and "Открыть объявление" in json.dumps(params, ensure_ascii=False):
                raise urllib.error.HTTPError("u", 400, "Bad Request: wrong url", {}, io.BytesIO(b""))
            return real(env, method, **params)

        bot.api = flaky
        self.press("k:i0")
        sends = real.of("sendMessage")
        self.assertEqual(len(sends), 1)
        self.assertIn("c:tailor:i0", json.dumps(sends[0]))
        self.assertEqual(self.row("i0")["card_msg_id"], real.next_id)

    def test_resent_card_buttons_work(self):
        """Pressing 'Отправил резюме' on the new message edits that message, as on any card."""
        self.add("i0")
        self.k("i0")
        new_id = self.row("i0")["card_msg_id"]
        self.api.calls.clear()
        self.press("s:applied:i0", message_id=new_id)
        self.assertEqual(self.row("i0")["status"], "applied")
        self.assertEqual(self.api.of("editMessageText")[0]["message_id"], new_id)


if __name__ == "__main__":
    unittest.main()
