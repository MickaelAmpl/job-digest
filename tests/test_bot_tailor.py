"""Task 7: 'Адаптировать резюме' button, the detached worker and the pasted-text flow, with a fake Telegram api,
a fake spawn and a fake tailor_job (no Claude, no Chrome, no network)."""
import copy
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import store  # noqa: E402
from cv.tailor import library, llm, pdfcheck, run, worker  # noqa: E402
from tests.fakes import FakeRun, envelope  # noqa: E402

BASE = json.loads((ROOT / "cv" / "source" / "content" / "en.json").read_text(encoding="utf-8"))
LIB = json.loads(library.LIBRARY.read_text(encoding="utf-8"))
CHAT = 4242
AD = ("We are looking for a Junior Product Owner (20 h/week) to manage the backlog in Jira, write product specs "
      "and run market research. English required, German a plus. ") * 3


class FakeApi:
    """bot.api stand-in: records calls, answers ok with a growing message_id."""

    def __init__(self):
        self.calls, self.next_id = [], 100

    def __call__(self, env, method, **params):
        self.calls.append((method, params))
        self.next_id += 1
        return {"ok": True, "result": {"message_id": self.next_id}}

    def texts(self, method="sendMessage"):
        return [p.get("text") for m, p in self.calls if m == method]

    def of(self, method):
        return [p for m, p in self.calls if m == method]


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.saved = (store.DB, bot.api, worker.spawn, worker.API, worker.TAILOR, worker.ENV, worker.RETRY_PAUSE,
                      os.environ.get("JOBDIGEST_CV_DIR"))
        store.DB = t / "state.db"
        os.environ["JOBDIGEST_CV_DIR"] = str(t / "cv")
        self.con = store.connect()
        for jid, verdict, desc in (("j1", "interested", AD), ("j2", "not_interested", AD), ("j3", None, AD),
                                   ("j4", "interested", "")):
            self.con.execute("insert into jobs (id, key, company, title, verdict, data) values (?,?,?,?,?,?)",
                             (jid, jid, "Raiffeisen Bank International (RBI)", "Student Job - Agile IT Delivery", verdict,
                              json.dumps({"description": desc})))
        self.con.commit()
        self.api = FakeApi()
        bot.api = worker.API = self.api
        worker.ENV = {"TELEGRAM_BOT_TOKEN": "test"}
        worker.RETRY_PAUSE = 0
        self.spawned = []
        # kind is recorded as a fifth item, after the four the older tests read (bot now passes kind= to spawn)
        worker.spawn = lambda job_id, chat_id, token, text=None, kind="cv": self.spawned.append(
            (job_id, chat_id, token, text, kind))

    def tearDown(self):
        self.con.close()
        (store.DB, bot.api, worker.spawn, worker.API, worker.TAILOR, worker.ENV, worker.RETRY_PAUSE, cv) = self.saved
        if cv is None:
            os.environ.pop("JOBDIGEST_CV_DIR", None)
        else:
            os.environ["JOBDIGEST_CV_DIR"] = cv
        self.tmp.cleanup()

    def press(self, data, message_id=7, markup=None):
        msg = {"message_id": message_id, "chat": {"id": CHAT}}
        if markup is not None:
            msg["reply_markup"] = {"inline_keyboard": markup}
        bot.handle_callback({}, self.con, {"id": "cq", "data": data, "from": {"id": CHAT}, "message": msg})

    def row(self, jid):
        return self.con.execute("select * from jobs where id=?", (jid,)).fetchone()

    def fake_result(self, status="ok", **kw):
        """A tailor_job result with a real PDF file, result.json and a cv_versions row, as run.save writes them."""
        vdir = worker.cv_dir() / "j1" / str(len(self.spawned) + len(self.api.calls) + 1)
        vdir.mkdir(parents=True, exist_ok=True)
        pdf = vdir / "Alex-Morgan-CV-RBI-Agile.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")
        out = {"status": status, "pdf": str(pdf), "coverage": [0.33, 0.67], "changes": [], "gaps": [], "reverted": [],
               "counts": {"terms": 3, "claimable": 3, "before": 1, "after": 2},
               "claimable": ["Jira", "backlog", "market research"],
               "answers": "- Work permit: student, 20 h/week\n- Salary: leave empty if optional", "reason": ""}
        out.update(kw)
        (vdir / "result.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        self.con.execute("insert into cv_versions (job_id, created_at, pdf_path, status) values (?,?,?,?)",
                         ("j1", store.now(), str(pdf), status))
        self.con.commit()
        return out


class Button(Env):
    def test_button_only_on_interested_cards(self):
        for status in (None,) + store.STATUSES:
            self.con.execute("update jobs set status=? where id='j1'", (status,))
            markup = bot.collapsed_markup(self.row("j1"), "j1")
            self.assertIn("c:tailor:j1", json.dumps(markup), status)
        for jid in ("j2",):
            self.assertNotIn("c:tailor:", json.dumps(bot.collapsed_markup(self.row(jid), jid)))

    def test_callback_data_fits_64_bytes(self):
        jid = store.job_id("some company#some very long title of a job ad that goes on and on")
        rows = bot.collapsed_markup({"verdict": "interested", "status": None, "open_url": None, "url": None}, jid)
        rows += worker.result_buttons(jid, 10 ** 12)
        datas = [b["callback_data"] for r in rows for b in r if "callback_data" in b]
        self.assertTrue(any(d.startswith("c:tailor:") for d in datas))
        self.assertTrue(all(len(d.encode()) <= 64 for d in datas), datas)


class Press(Env):
    def test_press_spawns_once_second_press_waits(self):
        self.press("c:tailor:j1")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][:2], ("j1", CHAT))
        self.assertIn("Готовлю резюме, 1-3 минуты", self.api.texts())
        self.press("c:tailor:j1")
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.api.texts()[-1], "Уже готовлю, подожди")
        worker.release("j1", self.spawned[0][2])  # the worker finished
        self.press("c:tailor:j1")
        self.assertEqual(len(self.spawned), 2)

    def test_stale_lock_is_taken_over(self):
        token = worker.acquire("j1")
        old = time.time() - worker.LOCK_STALE - 5
        os.utime(worker.lock_path("j1"), (old, old))
        self.press("c:tailor:j1")
        self.assertEqual(len(self.spawned), 1)
        worker.release("j1", token)  # the dead worker's token no longer owns the lock
        self.assertTrue(worker.lock_path("j1").exists())

    def test_not_interested_or_unknown_job_refused(self):
        self.press("c:tailor:j2")
        self.press("c:tailor:nope")
        self.assertEqual(self.spawned, [])
        self.assertFalse(worker.lock_path("j2").exists())

    def test_spawn_failure_releases_lock_and_says_so(self):
        def boom(*a, **kw):
            raise OSError("no python")
        worker.spawn = boom
        self.press("c:tailor:j1")
        self.assertIn("Не получилось запустить адаптацию резюме", self.api.texts()[-1])
        self.assertFalse(worker.lock_path("j1").exists())

    def test_real_spawn_is_detached_with_server_env(self):
        seen = {}

        class Popen:
            def __init__(self, args, **kw):
                seen.update(kw, args=args)

            def poll(self):
                return None
        saved, worker.subprocess.Popen = worker.subprocess.Popen, Popen
        chrome = Path(self.tmp.name) / "google-chrome"
        chrome.write_text("")
        saved_chrome, worker.SERVER_CHROME = worker.SERVER_CHROME, str(chrome)
        try:
            self.saved[2]("j1", CHAT, "tok", "the ad text")  # the real spawn
        finally:
            worker.subprocess.Popen, worker.SERVER_CHROME = saved, saved_chrome
        self.assertTrue(seen["start_new_session"])
        self.assertEqual(seen["args"][1:6], ["-m", "cv.tailor.worker", "j1", str(CHAT), "--lock"])
        text_file = Path(seen["args"][seen["args"].index("--text") + 1])
        self.assertEqual(text_file.read_text(encoding="utf-8"), "the ad text")
        if not os.environ.get("CHROME_BIN"):
            self.assertEqual(seen["env"]["CHROME_BIN"], str(chrome))
        self.assertEqual(Path(seen["cwd"]), ROOT)


class Worker(Env):
    def run_worker(self, result, pasted=None):
        calls = []

        def tailor(job_id, text=None):
            calls.append((job_id, text))
            if isinstance(result, BaseException):
                raise result
            return result
        worker.TAILOR = tailor
        token = worker.acquire("j1")
        worker.main("j1", CHAT, pasted, token)
        self.assertFalse(worker.lock_path("j1").exists())  # released in every case
        return calls

    def test_need_text_then_reply_runs_worker_with_text(self):
        self.run_worker({"status": "need_text", "reason": "нет текста вакансии"})
        ask = self.api.of("sendMessage")[-1]
        self.assertEqual(ask["text"], worker.ASK_TEXT)
        self.assertIn("Описания вакансии нет. Пришли текст объявления ответом на это сообщение", ask["text"])
        self.assertTrue(ask["reply_markup"]["force_reply"])
        asked_id = self.api.next_id
        self.assertEqual(self.con.execute("select asked_msg_id from cv_requests where job_id='j1'").fetchone()[0], asked_id)
        bot.handle_message({}, self.con, {"chat": {"id": CHAT}, "text": AD, "reply_to_message": {"message_id": asked_id}})
        self.assertEqual(self.spawned[-1][0], "j1")
        self.assertEqual(self.spawned[-1][3], AD.strip())
        self.assertEqual(self.api.texts()[-1], "Готовлю резюме, 1-3 минуты")
        notes = self.con.execute("select count(*) from notes").fetchone()[0]
        self.assertEqual(notes, 0)  # the ad text is not saved as a note
        worker.release("j1", self.spawned[-1][2])
        calls = self.run_worker(self.fake_result(), pasted=self.spawned[-1][3])
        self.assertEqual(calls, [("j1", AD.strip())])

    def test_short_pasted_text_asks_again(self):
        self.run_worker({"status": "need_text"}, pasted="too short")
        self.assertIn("слишком короткий", self.api.texts()[-1])

    def test_reply_to_another_message_is_a_note(self):
        bot.handle_message({}, self.con, {"chat": {"id": CHAT}, "text": "wrong score", "reply_to_message": {"message_id": 5}})
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.con.execute("select count(*) from notes").fetchone()[0], 1)

    def test_ok_sends_pdf_and_summary(self):
        changes = [
            {"field": "summary", "before": "", "after": "", "reason": "summary variant B_research (swap: metrics -> KPIs)"},
            {"field": "experience[0].bullets[1]", "before": "", "after": LIB["roles"][0]["bullets"]["pm2"],
             "reason": "library bullet pm2"},
            {"field": "experience[0].bullets[4]", "before": "Worked directly with the owners; built",
             "after": "Worked directly with stakeholders; built", "reason": "synonym swap in pm6 (swap: the owners -> stakeholders)"},
            {"field": "experience[0]", "before": LIB["roles"][0]["bullets"]["pm6"], "after": "", "reason": "dropped pm6 for this ad"},
            {"field": "experience[0].order", "before": "pm1, pm3", "after": "pm3, pm1", "reason": "bullets reordered"},
            {"field": "skills[2]", "before": "", "after": "Tools: Jira (Kanban), Confluence — Notion",
             "reason": "skills picked and ordered for the ad"},
            {"field": "experience[2]", "before": "x", "after": "", "reason": "dropped ra2 for this ad"},
            {"field": "experience[3].order", "before": "", "after": "", "reason": "bullets reordered"},
            {"field": "experience[1].bullets[0]", "before": "", "after": "y", "reason": "library bullet fi4"},
            {"field": "experience[1].bullets[1]", "before": "", "after": "z", "reason": "library bullet fi2"}]
        gaps = [{"requirement": "Scrum Master certification", "why": "none", "ru": "Сертификат Scrum Master"}] + \
               [{"requirement": f"Requirement {n}", "why": ""} for n in range(7)]
        self.run_worker(self.fake_result(changes=changes, gaps=gaps))
        doc = self.api.of("sendDocument")[0]
        name, data, mime = doc["document"]
        self.assertEqual((name, mime), ("Alex-Morgan-CV-RBI-Agile.pdf", "application/pdf"))
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn("Student Job - Agile IT Delivery", doc["caption"])
        msg = self.api.of("sendMessage")[-1]
        text = msg["text"]
        self.assertTrue(text.startswith("Требований в вакансии: 3. Ты закрываешь фактами: 3 (Jira, backlog, market "
                                        "research). В резюме было 1 из 3, стало 2 из 3."), text)
        for want in ("- Summary: вариант B (исследования), замена «metrics» -> «KPIs»",
                     "- PM: добавлен пункт «Turned market research",
                     "- PM: «the owners» -> «stakeholders»",
                     "- PM: убран пункт «Worked directly with the owners",
                     "- PM: порядок пунктов изменён",
                     "- Навыки: Tools - Jira (Kanban), Confluence - Notion",
                     "- Research Analyst: убран пункт «x»",
                     "- Founder: порядок пунктов изменён",
                     "- и ещё 2",
                     "Чего нет (честно не закрыть):",
                     "- Сертификат Scrum Master",
                     "- Requirement 4",
                     "- и ещё 2"):
            self.assertIn(want, text)
        self.assertNotIn("Requirement 5", text)
        self.assertNotIn("—", text)
        self.assertEqual(sum(1 for l in text.splitlines() if l.startswith("- ") and ":" in l and "Чего" not in l), 8)
        buttons = [b for r in msg["reply_markup"]["inline_keyboard"] for b in r]
        self.assertEqual([b["text"] for b in buttons], ["Шпаргалка для анкеты", "Отправил резюме"])

    def test_coverage_unknown(self):
        self.run_worker(self.fake_result(coverage=[None, None], counts={"terms": 0, "claimable": 0, "before": 0, "after": 0},
                                         reason="не удалось выделить ключевые слова"))
        self.assertIn("Требования вакансии: не удалось посчитать (не удалось выделить ключевые слова)", self.api.texts()[-1])

    def test_answer_sheet_and_applied_buttons(self):
        self.run_worker(self.fake_result())
        markup = self.api.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"]
        ans, applied = markup[0][0]["callback_data"], markup[1][0]["callback_data"]
        self.press(ans)
        self.assertIn("- Work permit: student, 20 h/week", self.api.texts()[-1])
        self.press(applied, markup=markup)
        self.assertEqual(self.row("j1")["status"], "applied")
        edit = self.api.of("editMessageReplyMarkup")[-1]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"], [markup[0]])  # the sheet button stays
        self.press(applied)
        self.assertEqual(self.api.of("answerCallbackQuery")[-1]["text"], "Без изменений")

    def test_fallback_message(self):
        self.run_worker(self.fake_result("fallback", reason="адаптированная версия не влезла на одну страницу"))
        self.assertEqual(len(self.api.of("sendDocument")), 1)
        self.assertEqual(self.api.texts()[-1], "Не получилось адаптировать: адаптированная версия не влезла на одну "
                                               "страницу. Прислал базовое резюме.")

    def test_library_error_and_crash_are_never_silent(self):
        self.run_worker(library.LibraryError("library.json: bad"))
        self.assertIn("библиотека формулировок", self.api.texts()[-1])
        self.run_worker(ValueError("boom"))
        self.assertIn("Не получилось адаптировать резюме: внутренняя ошибка (ValueError)", self.api.texts()[-1])
        self.run_worker({"status": "ok", "pdf": "/nonexistent.pdf", "coverage": [0.1, 0.2]})
        self.assertIn("внутренняя ошибка (FileNotFoundError)", self.api.texts()[-1])

    def flaky_upload(self, error):
        flaky, fails = FakeApi(), {"n": 1}

        def api(env, method, **params):
            if method == "sendDocument" and fails["n"]:
                fails["n"] -= 1
                raise error
            return flaky(env, method, **params)
        worker.API = api
        self.run_worker(self.fake_result())
        return flaky

    def test_upload_retried_only_when_it_surely_did_not_land(self):
        import socket
        import urllib.error
        for err in (urllib.error.URLError(ConnectionRefusedError(61, "refused")),
                    urllib.error.HTTPError("u", 502, "bad gateway", {}, None),
                    urllib.error.HTTPError("u", 429, "too many", {}, None)):
            flaky = self.flaky_upload(err)
            self.assertEqual(len(flaky.of("sendDocument")), 1, err)
            self.assertTrue(flaky.texts()[-1].startswith("Требований в вакансии"), err)
        for err in (socket.timeout("timed out"), urllib.error.URLError(TimeoutError("timed out")),
                    ConnectionResetError(54, "reset")):
            flaky = self.flaky_upload(err)
            self.assertEqual(flaky.of("sendDocument"), [], err)  # maybe delivered: no second PDF
            self.assertIn("внутренняя ошибка (RuntimeError)", flaky.texts()[-1], err)

    def test_caption_and_file_name_plain(self):
        self.con.execute("update jobs set title=?, company=? where id='j1'",
                         ("Product Owner \u2013 <b>Payments</b>", "RBI \u2014 Group"))
        self.con.commit()
        r = self.fake_result()
        pdf = Path(r["pdf"]).with_name("Alex-Morgan-CV-RBI\u2013Payments.pdf")
        Path(r["pdf"]).rename(pdf)
        self.run_worker(dict(r, pdf=str(pdf)))
        doc = self.api.of("sendDocument")[0]
        self.assertEqual(doc["caption"], "Product Owner - Payments · RBI - Group")
        self.assertEqual(doc["document"][0], "Alex-Morgan-CV-RBI-Payments.pdf")

    def test_skill_group_order_change_described(self):
        content = copy.deepcopy(BASE)
        content["skills"] = [content["skills"][2]] + content["skills"][:2] + content["skills"][3:]
        sel = library.base_selection(BASE, LIB)
        ch = library.changes(BASE, LIB, sel, content)
        self.assertEqual([c["field"] for c in ch], ["skills.order"])
        self.assertEqual(worker.describe(ch[0], worker.role_names(BASE)), "Навыки: порядок групп изменён")

    def test_lock_outlives_the_run_deadline(self):
        self.assertEqual(worker.RUN_DEADLINE, run.RUN_DEADLINE)
        self.assertGreaterEqual(worker.LOCK_STALE, run.RUN_DEADLINE + 120)
        self.assertEqual(worker.MIN_TEXT, run.MIN_DESC)


class QA7(Env):
    def toast(self):
        return self.api.of("answerCallbackQuery")[-1]["text"]

    def test_refusal_is_toast_and_message(self):
        self.press("c:tailor:j2")
        self.assertEqual(self.toast(), "Резюме адаптируется только для вакансий с отметкой «Интересно»")
        self.assertEqual(self.api.texts()[-1], "Резюме адаптируется только для вакансий с отметкой «Интересно»")
        self.press("c:tailor:nope")
        self.assertEqual(self.api.texts()[-1], "Эта вакансия не найдена в базе")
        self.assertEqual(self.spawned, [])

    def test_double_press_toast_says_busy(self):
        self.press("c:tailor:j1")
        self.assertEqual(self.toast(), "Готовлю резюме")
        self.press("c:tailor:j1")
        self.assertEqual(self.toast(), "Уже готовлю, подожди")
        self.assertEqual(len(self.spawned), 1)

    def ask(self):
        worker.TAILOR = lambda job_id, text=None: {"status": "need_text"}
        worker.main("j4", CHAT, None, worker.acquire("j4"))
        return self.api.next_id

    def reply(self, asked, **m):
        bot.handle_message({}, self.con, dict({"chat": {"id": CHAT}, "reply_to_message": {"message_id": asked}}, **m))

    def test_reply_command_photo_and_short_text(self):
        asked = self.ask()
        self.reply(asked, text="/stats")
        self.assertEqual(self.spawned, [])
        self.assertTrue(self.api.texts()[-1].startswith("Воронка"))  # handled as the command
        self.reply(asked, photo=[{"file_id": "x"}])
        self.assertEqual(self.api.texts()[-1], "Пришли текст объявления сообщением")
        self.reply(asked, text="Junior PO, Vienna, 20h")
        self.assertEqual(self.spawned, [])
        self.assertIn("слишком короткий", self.api.texts()[-1])
        new_ask = self.api.next_id
        self.assertEqual(self.con.execute("select asked_msg_id from cv_requests where job_id='j4'").fetchone()[0], new_ask)
        self.reply(new_ask, text=AD)
        self.assertEqual([s[0] for s in self.spawned], ["j4"])
        self.assertEqual(self.con.execute("select count(*) from notes").fetchone()[0], 0)

    def test_applied_refreshes_the_job_card(self):
        self.press("c:tailor:j1", message_id=55)  # the card
        worker.release("j1", self.spawned[0][2])
        markup = worker.result_buttons("j1", 1)
        self.press("c:applied:j1", message_id=90, markup=markup)
        edits = self.api.of("editMessageText")
        self.assertEqual(edits[-1]["message_id"], 55)
        self.assertIn("Отправил резюме", edits[-1]["text"])
        self.assertIn("s:replied:j1", json.dumps(edits[-1]["reply_markup"]))

    def test_unknown_card_skipped_silently(self):
        self.press("c:applied:j1", message_id=90, markup=worker.result_buttons("j1", 1))
        self.assertEqual(self.row("j1")["status"], "applied")
        self.assertEqual(self.api.of("editMessageText"), [])


class EndToEnd(Env):
    """The real run.tailor_job (fake Claude, fake renderer) feeding the real summary message."""

    def test_real_result_shape(self):
        reqs = {"must": ["Jira"], "nice": [], "terms": ["Jira", "backlog", "market research"], "ad_language": "en",
                "min_salary": None, "hours": None}
        roles = [["pm1", "pm3", "pm4", "pm5", "pm2"], ["fi1", "fi2", "fi3"], ["ra1", "ra2", "ra3", "ra4"], ["fo3", "fo1", "fo2"]]
        answer = {"summary": "B_research", "roles": roles, "swaps": [], "skills": copy.deepcopy(BASE["skills"]),
                  "gaps": [{"requirement": "German C1", "why": "B1 only", "ru": "Немецкий C1"}],
                  "terms": [{"term": t, "claimable": True} for t in reqs["terms"]]}
        saved = (llm.RUN, run.BUILD, run.CHECK)
        llm.RUN = FakeRun(envelope(json.dumps(reqs)), envelope(json.dumps(answer)))

        def build(content_path, pdf_path, html_dir=None):
            Path(pdf_path).write_bytes(b"%PDF-1.4 /Type /Pages /Count 1")
            return Path(pdf_path)
        run.BUILD = build
        run.CHECK = lambda pdf, text=None: {"pages": 1, "size": 1, "text": "", "issues": []}
        worker.TAILOR = None
        try:
            token = worker.acquire("j1")
            worker.main("j1", CHAT, None, token)
        finally:
            llm.RUN, run.BUILD, run.CHECK = saved
        text = self.api.texts()[-1]
        self.assertIn("Требований в вакансии: 3. Ты закрываешь фактами: 3 (Jira, backlog, market research). "
                      "В резюме было 1 из 3, стало 2 из 3.", text)
        self.assertIn("Нет готовой формулировки для: backlog (можно добавить в библиотеку)", text)  # no unit has it
        self.assertIn("Summary: вариант B (исследования)", text)
        self.assertIn("PM: добавлен пункт «Turned market research", text)
        self.assertIn("PM: убран пункт «Worked directly with the owners", text)
        self.assertNotIn("Founder: порядок", text)  # fo3 pinned last: the order is the base order
        self.assertIn("- Немецкий C1", text)
        ans = self.api.of("sendMessage")[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.press(ans)
        self.assertIn("Salary", self.api.texts()[-1])


if __name__ == "__main__":
    unittest.main()
