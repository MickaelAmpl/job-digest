"""Stage 8: /add in the bot - an application sent by hand. The confirmation and its buttons, the questions the bot asks
(a reply to them is the answer, never a note), 'Это не отправлял', and the manual card. Fake Telegram api and temp DB
from test_bot_tailor.Env; the network is patched to fail, so a /add that fetched a link would fail the test."""
import contextlib
import json
import socket
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from digest import MONTHS  # noqa: E402
import bot  # noqa: E402
import store  # noqa: E402
from cv.tailor import worker  # noqa: E402
from tests.test_bot_tailor import CHAT, Env  # noqa: E402

URL = "https://acme.example/careers/brand-marketing-intern"
LINE = "Acme Labs / Brand & Marketing Intern"
TODAY = date.today()


class Api:
    """bot.api stand-in: ok answers with a growing message_id; every call is kept in order."""

    def __init__(self):
        self.calls, self.next_id = [], 300

    def __call__(self, env, method, **params):
        self.calls.append((method, params))
        self.next_id += 1
        return {"ok": True, "result": {"message_id": self.next_id}}

    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def toasts(self):
        return [p["text"] for p in self.of("answerCallbackQuery")]


def buttons(params):
    return [b for r in (params.get("reply_markup") or {}).get("inline_keyboard", []) for b in r]


class Base(Env):
    def setUp(self):
        super().setUp()
        self.api = Api()
        bot.api = worker.API = self.api
        self.pause, bot.PAUSE = bot.PAUSE, 0
        self.con.execute("delete from jobs")
        self.con.commit()

    def tearDown(self):
        bot.PAUSE = self.pause
        super().tearDown()

    # driving the bot
    def send(self, text, reply_to=None):
        m = {"text": text, "chat": {"id": CHAT}, "message_id": 50}
        if reply_to is not None:
            m["reply_to_message"] = {"message_id": reply_to, "chat": {"id": CHAT}, "text": "question"}
        bot.handle_message({}, self.con, m)

    def last_text(self):
        return self.api.of("sendMessage")[-1]["text"]

    def last_edit(self):
        return self.api.of("editMessageText")[-1]

    # reading the state
    def draft(self):
        """The open draft (a finished one keeps a tombstone for 24 h: json 'done')."""
        return self.con.execute("select * from manual_drafts where json not like '%\"done\"%'").fetchone()

    def ask(self, kind):
        return self.con.execute("select * from manual_requests where kind=?", (kind,)).fetchone()

    def count(self, where="1=1", table="jobs"):
        return self.con.execute(f"select count(*) from {table} where {where}").fetchone()[0]

    def job(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def manual_id(self):
        return self.con.execute("select id from jobs where source='manual'").fetchone()[0]

    def seed(self, jid, company="Acme Labs", title="Brand Marketing Intern Summer", url=None, verdict=None, status=None):
        key = store.dedupe_key({"company": company, "title": title, "url": url or ""})
        self.con.execute("insert into jobs (id, key, company, title, verdict, status, url, data) values (?,?,?,?,?,?,?,?)",
                         (jid, key, company, title, verdict, status, url, "{}"))
        self.con.commit()

    def add_and_save(self, line=LINE, url=URL):
        self.send(f"/add {line} {url}".strip())
        self.press(f"m:save:{self.draft()['token']}")
        return self.manual_id()


class Confirmation(Base):
    def test_add_with_text_shows_the_confirmation(self):
        self.send(f"/add {LINE} {URL}")
        d = self.draft()
        self.assertIsNotNone(d)
        p = self.api.of("sendMessage")[-1]
        self.assertEqual(p["parse_mode"], "HTML")
        self.assertIn("<b>Brand &amp; Marketing Intern</b> · Acme Labs · Отправил резюме: сегодня", p["text"])
        self.assertEqual(sorted(b["callback_data"] for b in buttons(p)),
                         sorted(f"m:{a}:{d['token']}" for a in ("save", "yday", "date", "edit", "cancel")))
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in buttons(p)))
        self.assertEqual(d["msg_id"], self.api.next_id)
        self.assertEqual(json.loads(d["json"])["url"], URL)
        self.assertEqual(self.count("1=1", "jobs"), 0)  # nothing is written until 'Сохранить'

    def test_add_without_text_asks_for_the_line(self):
        self.send("/add")
        p = self.api.of("sendMessage")[-1]
        self.assertTrue(p["reply_markup"]["force_reply"])
        self.assertEqual(self.ask("new")["token"], "")
        self.assertIsNone(self.draft())

    def test_bad_inline_line_is_asked_once_more(self):
        self.send("/add nonsense")
        self.assertEqual(self.ask("new")["token"], "again")
        self.assertIsNone(self.draft())

    def test_m_callbacks_are_not_taken_for_job_ids(self):
        self.press("m:save:nope")
        self.assertEqual(self.api.toasts()[-1], "Уже сохранено")
        self.assertNotIn("Эта вакансия не найдена в базе", self.api.toasts())


class Buttons(Base):
    def test_save_creates_the_application_and_turns_the_message_into_its_card(self):
        self.send(f"/add {LINE} {URL}")
        d = self.draft()
        msg_id = d["msg_id"]
        self.press(f"m:save:{d['token']}")
        row = self.con.execute("select * from jobs where source='manual'").fetchone()
        self.assertEqual((row["company"], row["title"], row["url"]), ("Acme Labs", "Brand & Marketing Intern", URL))
        self.assertEqual((row["verdict"], row["status"]), ("interested", "applied"))
        self.assertTrue(row["applied_at"].startswith(TODAY.isoformat()))
        self.assertIsNone(self.draft())
        self.assertEqual(self.api.toasts()[-1], "Записал отклик")
        edit = self.last_edit()
        self.assertEqual(edit["message_id"], msg_id)
        self.assertEqual(edit["text"], bot.collapsed_text(row))
        self.assertEqual(edit["reply_markup"]["inline_keyboard"], bot.collapsed_markup(row, row["id"]))
        self.assertIn("Отправил резюме", edit["text"])
        self.assertIn("(вручную)", edit["text"])
        flat = json.dumps(edit["reply_markup"], ensure_ascii=False)
        for label in ("Ответили", "Изменить статус", "Это не отправлял"):
            self.assertIn(label, flat)
        self.assertEqual(self.job(row["id"])["card_msg_id"], msg_id)
        self.assertEqual([r["status"] for r in self.con.execute(
            "select status from status_log where job_id=? order by rowid", (row["id"],))], ["applied"])

    def test_status_buttons_work_on_the_new_card(self):
        jid = self.add_and_save()
        self.api.calls.clear()
        self.press(f"s:replied:{jid}", message_id=self.job(jid)["card_msg_id"])
        self.assertEqual(self.job(jid)["status"], "replied")
        self.assertIn("Ответили", self.last_edit()["text"])

    def test_manual_card_has_no_cv_or_letter_buttons(self):
        jid = self.add_and_save()
        flat = json.dumps(self.last_edit()["reply_markup"], ensure_ascii=False)
        self.assertNotIn("c:tailor:", flat)
        self.assertNotIn("c:letter:", flat)
        self.assertIn(f"m:gone:{jid}", flat)

    def test_manual_card_has_no_edit_button_in_any_status(self):
        jid = self.add_and_save()
        flat = json.dumps(self.last_edit()["reply_markup"], ensure_ascii=False)
        self.assertNotIn(f'"callback_data": "e:{jid}"', flat)  # the verdict of a manual application is not changed by hand
        self.assertIn(f"s:menu:{jid}", flat)  # the status menu stays
        for status in store.STATUSES:
            self.con.execute("update jobs set status=? where id=?", (status, jid))
            self.assertNotIn(f'"callback_data": "e:{jid}"', json.dumps(bot.collapsed_markup(self.job(jid), jid)), status)

    def test_second_save_says_already_saved(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:save:{token}")
        self.press(f"m:save:{token}")
        self.assertEqual(self.api.toasts()[-1], "Уже сохранено")
        self.assertEqual(self.count("source='manual'"), 1)

    def test_yesterday_dates_the_application(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        self.press(f"m:yday:{token}")
        self.assertEqual(json.loads(self.draft()["json"])["day"], yesterday)
        self.assertIn("Отправил резюме: вчера", self.last_edit()["text"])
        self.press(f"m:save:{token}")
        row = self.con.execute("select * from jobs where source='manual'").fetchone()
        self.assertEqual(row["applied_at"], f"{yesterday}T12:00:00")

    def test_other_date_rolls_back_a_day_and_a_bad_or_future_one_is_refused(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:date:{token}")
        ask = self.ask("date")
        self.assertIsNotNone(ask)
        self.send("abc", reply_to=ask["asked_msg_id"])
        self.assertEqual(self.last_text(), bot.DATE_BAD)
        self.assertIsNotNone(self.ask("date"))  # the question stays open
        future = TODAY + timedelta(days=3)
        self.send(f"{future:%d.%m.%Y}", reply_to=ask["asked_msg_id"])
        self.assertEqual(self.last_text(), bot.DATE_FUTURE)
        self.assertEqual(json.loads(self.draft()["json"])["day"], TODAY.isoformat())
        self.send(f"{TODAY - timedelta(days=2):%d.%m}", reply_to=ask["asked_msg_id"])
        self.assertEqual(json.loads(self.draft()["json"])["day"], (TODAY - timedelta(days=2)).isoformat())
        self.assertIn("Отправил резюме: ", self.last_edit()["text"])
        self.assertIsNone(self.ask("date"))

    def test_edit_replaces_the_line_and_redraws_the_confirmation(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:edit:{token}")
        ask = self.ask("edit")
        self.send("Acme Labs / Senior Brand Lead", reply_to=ask["asked_msg_id"])
        d = json.loads(self.draft()["json"])
        self.assertEqual((d["company"], d["title"], d["url"]), ("Acme Labs", "Senior Brand Lead", ""))
        self.assertIn("<b>Senior Brand Lead</b>", self.last_edit()["text"])
        self.assertIsNone(self.ask("edit"))

    def test_edit_asks_once_more_then_stops_and_keeps_the_draft(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:edit:{token}")
        self.send("garbage", reply_to=self.ask("edit")["asked_msg_id"])
        self.assertIsNotNone(self.ask("edit"))  # asked once more
        self.send("garbage again", reply_to=self.ask("edit")["asked_msg_id"])
        self.assertEqual(self.last_text(), bot.EDIT_GIVE_UP)
        self.assertEqual(json.loads(self.draft()["json"])["title"], "Brand & Marketing Intern")
        self.assertIsNone(self.ask("edit"))

    def test_cancel_drops_the_draft_and_writes_nothing(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:cancel:{token}")
        self.assertIsNone(self.draft())
        self.assertEqual(self.count("source='manual'"), 0)
        self.assertEqual(self.api.toasts()[-1], "Отменил")
        self.assertIn("Не записал", self.last_edit()["text"])
        self.assertEqual(self.last_edit()["reply_markup"]["inline_keyboard"], [])

    def test_expired_draft_is_not_saved(self):
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        self.con.execute("update manual_drafts set created_at=? where token=?", (old, token))
        self.con.commit()
        self.press(f"m:save:{token}")
        self.assertEqual(self.api.toasts()[-1], "Черновик устарел, начни заново: /add")
        self.assertEqual(self.count("source='manual'"), 0)
        self.assertIsNone(self.draft())

    def test_link_is_stored_and_nothing_touches_the_network(self):
        boom = AssertionError("network call from /add")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("urllib.request.urlopen", side_effect=boom))
            stack.enter_context(mock.patch("socket.create_connection", side_effect=boom))
            stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=boom))
            jid = self.add_and_save(line="Acme Labs / Brand & Marketing Intern", url=URL)
        row = self.job(jid)
        self.assertEqual(row["url"], URL)
        self.assertIn("Открыть объявление", json.dumps(bot.collapsed_markup(row, jid), ensure_ascii=False))


class Candidates(Base):
    def test_a_similar_job_is_offered_with_this_or_new_instead_of_save(self):
        self.seed("s1")  # same employer, similar title, another key: a candidate, not the same row
        self.send(f"/add {LINE} {URL}")
        p = self.api.of("sendMessage")[-1]
        self.assertIn("Похоже, это уже есть: <b>Brand Marketing Intern Summer</b> · Acme Labs", p["text"])
        labels = [b["callback_data"].split(":")[1] for b in buttons(p)]
        self.assertIn("this", labels)
        self.assertIn("new", labels)
        self.assertNotIn("save", labels)
        self.assertEqual(json.loads(self.draft()["json"])["candidate"], "s1")

    def test_an_exact_key_candidate_offers_no_new_button(self):
        self.seed("s1", title="Brand & Marketing Intern")  # the same dedupe key as the line
        self.send(f"/add {LINE} {URL}")
        p = self.api.of("sendMessage")[-1]
        labels = sorted(b["callback_data"].split(":")[1] for b in buttons(p))
        self.assertEqual(labels, sorted(["this", "cancel", "yday", "date", "edit"]))
        self.assertNotIn("new", labels)
        self.assertNotIn("save", labels)

    def test_a_stale_new_on_an_exact_candidate_never_makes_a_second_row(self):
        self.seed("s1", title="Brand & Marketing Intern")
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:new:{self.draft()['token']}")  # an old copy of the confirmation
        self.assertEqual(self.count("1=1"), 1)
        self.assertIn("manual_applied", self.job("s1")["data"])
        self.assertEqual(self.api.toasts()[-1], "Эта вакансия уже была в боте - отметил отклик на ней")

    def test_new_on_a_link_match_with_another_title_creates_a_row(self):
        link = "https://jobs.example.at/job/123456"  # a job number in the link: the same posting by link
        self.seed("s1", title="Growth Lead", url=link)
        self.send(f"/add {LINE} {link}")
        p = self.api.of("sendMessage")[-1]
        labels = [b["callback_data"].split(":")[1] for b in buttons(p)]
        self.assertIn("new", labels)  # not an exact key, so 'Новая' is offered
        self.press(f"m:new:{self.draft()['token']}")
        self.assertEqual(self.count("1=1"), 2)  # force_new: a new row even though the link matches
        self.assertIsNone(self.job("s1")["status"])
        self.assertEqual(self.api.toasts()[-1], "Записал отклик")

    def test_this_attaches_the_application_to_the_existing_job(self):
        self.seed("s1")
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:this:{self.draft()['token']}")
        row = self.job("s1")
        self.assertEqual((row["verdict"], row["status"]), ("interested", "applied"))
        self.assertIn("manual_applied", row["data"])
        self.assertEqual(self.count("1=1"), 1)  # no new row
        self.assertEqual(self.api.toasts()[-1], "Эта вакансия уже была в боте - отметил отклик на ней")
        self.assertEqual(self.job("s1")["card_msg_id"], self.last_edit()["message_id"])
        self.assertIn("(вручную)", self.last_edit()["text"])

    def test_attached_card_keeps_its_edit_button(self):
        self.seed("s1")
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:this:{self.draft()['token']}")
        self.assertIn('"callback_data": "e:s1"', json.dumps(self.last_edit()["reply_markup"], ensure_ascii=False))

    def test_new_creates_a_second_row_and_leaves_the_candidate_alone(self):
        self.seed("s1")
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:new:{self.draft()['token']}")
        self.assertEqual(self.count("1=1"), 2)
        self.assertEqual(self.count("source='manual'"), 1)
        self.assertIsNone(self.job("s1")["status"])
        self.assertEqual(self.api.toasts()[-1], "Записал отклик")

    def test_same_job_again_is_unchanged(self):
        self.add_and_save()
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:this:{self.draft()['token']}")
        self.assertEqual(self.count("source='manual'"), 1)
        self.assertEqual(self.api.toasts()[-1], "Эта вакансия уже в откликах - ничего не менял")

    def test_removed_application_is_revived(self):
        jid = self.add_and_save()
        self.press(f"m:gone:{jid}")
        self.press(f"m:gonedo:{jid}")
        self.send(f"/add {LINE} {URL}")
        # the removed row is no candidate any more, so the confirmation offers 'Сохранить' again
        self.assertIn("m:save:", json.dumps(self.api.of("sendMessage")[-1]["reply_markup"]))
        self.press(f"m:save:{self.draft()['token']}")
        self.assertEqual(self.api.toasts()[-1], "Вернул отклик")
        self.assertEqual(self.job(jid)["closed"], 0)
        self.assertEqual(self.job(jid)["status"], "applied")


class Gone(Base):
    def test_gone_asks_then_removes_the_manual_row(self):
        jid = self.add_and_save()
        self.press(f"m:gone:{jid}")
        flat = json.dumps(self.last_edit()["reply_markup"], ensure_ascii=False)
        self.assertIn(f"m:gonedo:{jid}", flat)
        self.assertIn(f"m:gonekeep:{jid}", flat)
        self.assertEqual(self.job(jid)["closed"], 0)  # still there after the first press
        self.press(f"m:gonedo:{jid}")
        row = self.job(jid)
        self.assertEqual((row["closed"], row["closed_by"], row["status"], row["verdict"]),
                         (1, "manual_removed", None, None))
        self.assertIn("Убрал: отклик не отправлял", self.last_edit()["text"])
        self.assertEqual(self.last_edit()["reply_markup"]["inline_keyboard"], [])
        self.assertEqual(self.api.toasts()[-1], "Убрал отклик")
        lines, _ = bot.applied_list(self.con)
        self.assertFalse(any("Brand" in line for line in lines))

    def test_keep_brings_the_card_back_unchanged(self):
        jid = self.add_and_save()
        self.press(f"m:gone:{jid}")
        self.press(f"m:gonekeep:{jid}")
        self.assertEqual(self.job(jid)["status"], "applied")
        self.assertIn(f"m:gone:{jid}", json.dumps(self.last_edit()["reply_markup"]))

    def test_attached_application_removal_restores_the_verdict_from_before_the_attach(self):
        self.seed("s1")  # no verdict before the attach
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:this:{self.draft()['token']}")
        self.press("m:gonedo:s1")
        row = self.job("s1")
        self.assertEqual((row["verdict"], row["status"], row["applied_at"]), (None, None, None))
        self.assertNotIn("manual_applied", row["data"])
        flat = json.dumps(self.last_edit()["reply_markup"], ensure_ascii=False)
        self.assertIn("v:interested:s1", flat)  # back to the card of a job without a mark
        self.assertNotIn("m:gone:", flat)

    def test_removal_puts_a_not_interested_mark_back(self):
        self.seed("s1", verdict="not_interested")
        self.send(f"/add {LINE} {URL}")
        self.press(f"m:this:{self.draft()['token']}")
        self.assertEqual(self.job("s1")["verdict"], "interested")  # the attach changed it
        self.press("m:gonedo:s1")
        self.assertEqual(self.job("s1")["verdict"], "not_interested")


class Replies(Base):
    def test_reply_to_a_question_is_the_line_and_never_a_note(self):
        self.send("/add")
        self.send(LINE, reply_to=self.ask("new")["asked_msg_id"])
        self.assertIsNotNone(self.draft())
        self.assertIsNone(self.ask("new"))
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_a_command_wins_over_a_reply_and_closes_the_question(self):
        self.send("/add")
        ask_id = self.ask("new")["asked_msg_id"]
        self.send("/stats", reply_to=ask_id)
        self.assertTrue(self.last_text().startswith("Воронка"))
        self.assertIsNone(self.ask("new"))  # any command closes the open /add question
        self.assertIsNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_cancel_command_closes_the_question(self):
        self.send("/add")
        self.send("/cancel")
        self.assertIsNone(self.ask("new"))
        self.send(LINE)  # no question is open any more: a plain line is a note
        self.assertIsNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 1)

    def test_unknown_command_closes_the_question(self):
        self.send("/add")
        self.send("/nonsense")
        self.assertIsNone(self.ask("new"))
        self.assertIn(bot.UNKNOWN_COMMAND, self.last_text())

    def test_plain_text_without_link_line_or_separator_stays_a_note(self):
        self.send("/add")
        self.send("спасибо за пример")
        self.assertIsNone(self.draft())
        self.assertIsNotNone(self.ask("new"))  # the question stays open
        self.assertEqual(self.count("1=1", "notes"), 1)

    def test_cancel_word_without_a_reply_cancels_the_open_question(self):
        self.send("/add")
        self.send("отмена")  # exactly a cancel word, while the question is open (S8-6 a)
        self.assertIsNone(self.ask("new"))
        self.assertEqual(self.count("1=1", "notes"), 0)
        self.assertIsNone(self.draft())

    def test_cancel_word_with_no_open_question_is_a_note(self):
        self.send("отмена")
        self.assertEqual(self.count("1=1", "notes"), 1)

    def test_a_line_break_a_separator_or_a_link_routes_a_plain_message(self):
        self.send("/add")
        self.send("Acme\nEngineer")
        self.assertIsNotNone(self.draft())
        self.con.execute("delete from manual_drafts")
        self.con.commit()
        self.send("/add")  # a command closes the earlier question; a new one is asked
        self.send("Acme Labs | Brand Marketing Intern https://acme.example/careers/x")
        self.assertIsNotNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_a_dash_in_a_wish_is_a_note_not_a_line(self):  # S8-6 c: ' - ' no longer routes a message
        self.send("/add")
        self.send("Acme Labs - Brand Marketing Intern")
        self.assertIsNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 1)

    def test_a_reply_to_a_closed_question_says_so_and_is_no_note(self):  # S8-6 b
        self.send("/add")
        ask_id = self.ask("new")["asked_msg_id"]
        self.send(LINE, reply_to=ask_id)  # answered: the draft exists
        self.send("ещё одна строка", reply_to=ask_id)
        self.assertEqual(self.last_text(), bot.ASK_CLOSED)
        self.assertEqual(self.count("1=1", "notes"), 0)
        self.assertEqual(self.con.execute("select kind from manual_requests where asked_msg_id=?",
                                          (ask_id,)).fetchone()[0], "done")  # kept, not deleted

    def test_a_command_closes_the_question_and_a_late_reply_says_so(self):
        self.send("/add")
        ask_id = self.ask("new")["asked_msg_id"]
        self.send("/stats")
        self.send("Acme Labs / Brand", reply_to=ask_id)
        self.assertEqual(self.last_text(), bot.ASK_CLOSED)
        self.assertIsNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_the_day_of_last_year_is_shown_with_its_year(self):  # S8-6 d
        last_year = TODAY.replace(year=TODAY.year - 1)
        text = bot.day_word(last_year.isoformat())
        self.assertEqual(text, f"{last_year.day} {MONTHS[last_year.month - 1]} {last_year.year}")
        self.assertEqual(bot.day_word(TODAY.isoformat()), "сегодня")

    def test_a_link_alone_is_routed_and_asked_once_more(self):
        self.send("/add")
        self.send(URL)  # has a link, no separator: /add takes it and asks for the line once more
        self.assertIsNone(self.draft())
        self.assertEqual(self.ask("new")["token"], "again")
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_a_plain_line_within_10_minutes_goes_to_add(self):
        self.send("/add")
        self.send(LINE)
        self.assertIsNotNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_a_plain_line_after_10_minutes_is_a_note(self):
        self.send("/add")
        old = (datetime.now() - timedelta(minutes=11)).isoformat(timespec="seconds")
        self.con.execute("update manual_requests set at=?", (old,))
        self.con.commit()
        self.send(LINE)
        self.assertIsNone(self.draft())
        self.assertEqual(self.count("1=1", "notes"), 1)

    def test_cancel_word_cancels_the_question(self):
        self.send("/add")
        self.send("отмена", reply_to=self.ask("new")["asked_msg_id"])
        self.assertIsNone(self.draft())
        self.assertIsNone(self.ask("new"))
        self.assertEqual(self.count("1=1", "notes"), 0)

    def test_a_bad_line_is_asked_once_more_then_the_bot_stops(self):
        self.send("/add")
        self.send("nonsense", reply_to=self.ask("new")["asked_msg_id"])
        self.assertEqual(self.ask("new")["token"], "again")
        self.send("nonsense again", reply_to=self.ask("new")["asked_msg_id"])
        self.assertEqual(self.last_text(), bot.ADD_GIVE_UP)
        self.assertIsNone(self.draft())
        self.assertIsNone(self.ask("new"))
        self.assertEqual(self.count("1=1", "notes"), 0)


class Lists(Base):
    def test_applied_list_marks_manual_rows(self):
        self.add_and_save()
        lines, _ = bot.applied_list(self.con)
        self.assertTrue(any("(вручную)" in line for line in lines))

    def test_status_menu_has_no_reset_for_a_manual_row(self):
        jid = self.add_and_save()
        flat = json.dumps(bot.status_menu(self.job(jid), jid), ensure_ascii=False)
        self.assertNotIn("s:reset:", flat)
        self.assertIn(f"m:gone:{jid}", flat)

    def test_help_lists_add(self):
        self.assertIn("/add - добавить отклик вручную", bot.HELP)
        self.assertIn(("add", "добавить отклик вручную"), bot.COMMANDS)


class Round3(Base):
    """S8-1 zombie cards, S8-3 tombstones of finished drafts."""

    def test_an_old_k_button_does_not_bring_a_removed_application_back(self):  # S8-1
        jid = self.add_and_save()
        self.press(f"m:gonedo:{jid}")
        before = len(self.api.of("sendMessage"))
        self.press(f"k:{jid}")
        self.assertEqual(self.api.toasts()[-1], bot.REMOVED_NOTE)
        sent = self.api.of("sendMessage")[before:]
        self.assertEqual([p["text"] for p in sent], [bot.REMOVED_NOTE])  # no card, only the note
        self.assertNotIn("reply_markup", sent[0])

    def test_old_status_and_verdict_buttons_change_nothing_on_a_removed_application(self):  # S8-1
        jid = self.add_and_save()
        self.press(f"m:gonedo:{jid}")
        edits = len(self.api.of("editMessageText"))
        for data in (f"s:replied:{jid}", f"v:not_interested:interested:{jid}", f"e:{jid}"):
            self.press(data)
            self.assertEqual(self.api.toasts()[-1], bot.REMOVED_NOTE, data)
        row = self.job(jid)
        self.assertEqual((row["status"], row["verdict"], row["closed_by"]), (None, None, "manual_removed"))
        self.assertEqual(len(self.api.of("editMessageText")), edits)  # the old card is left as it is

    def test_refresh_card_skips_a_removed_application(self):  # S8-1
        jid = self.add_and_save()
        self.press(f"m:gonedo:{jid}")
        edits = len(self.api.of("editMessageText"))
        bot.refresh_card({}, self.con, jid, CHAT)
        self.assertEqual(len(self.api.of("editMessageText")), edits)

    def test_a_late_press_after_cancel_says_the_draft_was_cancelled(self):  # S8-3
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:cancel:{token}")
        self.press(f"m:save:{token}")
        self.assertEqual(self.api.toasts()[-1], "Черновик отменён")

    def test_a_late_press_after_save_says_already_saved_on_any_button(self):  # S8-3
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:save:{token}")
        self.press(f"m:cancel:{token}")
        self.assertEqual(self.api.toasts()[-1], "Уже сохранено")

    def test_a_late_press_after_expiry_keeps_saying_it_expired(self):  # S8-3
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        self.con.execute("update manual_drafts set created_at=? where token=?", (old, token))
        self.con.commit()
        self.press(f"m:save:{token}")
        self.press(f"m:save:{token}")
        self.assertEqual(self.api.toasts()[-2:], [bot.EXPIRED_NOTE, bot.EXPIRED_NOTE])
        self.assertEqual(self.count("source='manual'"), 0)

    def test_tombstones_and_closed_questions_are_removed_after_24_hours(self):  # S8-3 / S8-6 b
        self.send(f"/add {LINE} {URL}")
        token = self.draft()["token"]
        self.press(f"m:cancel:{token}")
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        self.con.execute("update manual_drafts set created_at=? where token=?", (old, token))
        self.con.execute("update manual_requests set at=?", (old,))
        self.con.commit()
        self.send("hello")  # any text runs the cleanup
        self.assertEqual(self.count("1=1", "manual_drafts"), 0)
        self.assertEqual(self.count("1=1", "manual_requests"), 0)


class Parsing(unittest.TestCase):
    def test_company_and_title_split_on_newline_then_separators(self):
        self.assertEqual(bot.parse_manual(f"{LINE} {URL}"), ("Acme Labs", "Brand & Marketing Intern", URL))
        self.assertEqual(bot.parse_manual("Acme | Engineer\nhttps://x.at/job/1"), ("Acme", "Engineer", "https://x.at/job/1"))
        self.assertEqual(bot.parse_manual("Acme\nEngineer"), ("Acme", "Engineer", ""))
        self.assertEqual(bot.parse_manual("Acme - Senior - Lead"), ("Acme", "Senior - Lead", ""))
        self.assertEqual(bot.parse_manual("Acme / Title https://x.at/j/9."), ("Acme", "Title", "https://x.at/j/9"))

    def test_both_parts_are_required(self):
        for text in ("Acme", "https://x.at/1", "Acme /", "/ Engineer", "", "Acme Engineer"):
            self.assertIsNone(bot.parse_manual(text), text)

    def test_day_rules(self):
        today = date(2026, 10, 26)
        self.assertEqual(bot.parse_day("26.10", today), "2026-10-26")
        self.assertEqual(bot.parse_day("25.10", today), "2026-10-25")
        self.assertEqual(bot.parse_day("28.11", today), "2025-11-28")  # ahead this year: last year
        self.assertEqual(bot.parse_day("1.10.26", today), "2026-10-01")
        self.assertEqual(bot.parse_day("26.10.2026", today), "2026-10-26")
        for text, message in (("28.11.2026", bot.DATE_FUTURE), ("31.02", bot.DATE_BAD), ("abc", bot.DATE_BAD),
                              ("29.02", bot.DATE_BAD)):
            with self.assertRaises(ValueError) as ctx:
                bot.parse_day(text, today)
            self.assertEqual(str(ctx.exception), message, text)


if __name__ == "__main__":
    unittest.main()
