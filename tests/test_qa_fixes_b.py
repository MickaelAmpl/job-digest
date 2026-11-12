"""QA 09.10.2026 fixes on the bot and the CV tailoring side (developer B): worker lock liveness, .env reload and
token masking, replies to older ad-text asks, stored pasted text, salary and answer-sheet hardening, honest
summary lines, stale card presses, edit fallbacks, the витрина round, commands and free text. Fake Telegram,
fake claude, fake renderer - no network."""
import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import store  # noqa: E402
from cv.tailor import checks, library, llm, requirements, run, tailor, worker  # noqa: E402
from tests import test_run  # noqa: E402
from tests.fakes import FakeRun  # noqa: E402
from tests.test_bot_tailor import AD, BASE, CHAT, Env  # noqa: E402

TOKEN = "123456789:" + "A" * 35


def http_error(code, body=b"", reason="Bad Request"):
    return urllib.error.HTTPError("u", code, reason, {}, io.BytesIO(body))


# ---------- worker lock (ops-04, ops-08, cvtailor-08) ----------

class Lock(Env):
    def setUp(self):
        super().setUp()
        worker._children.clear()
        worker._notices.clear()

    def tearDown(self):
        worker._children.clear()
        worker._notices.clear()
        super().tearDown()

    def write_lock(self, job, **data):
        path = worker.lock_path(job)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict({"token": "old", "started": time.time()}, **data)))
        return path

    def age(self, path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t))

    def test_lock_records_pid_and_state(self):
        token = worker.acquire("j1")
        lock = json.loads(worker.lock_path("j1").read_text())
        self.assertEqual((lock["token"], lock["pid"], lock["state"]), (token, os.getpid(), "pending"))
        self.assertIsNone(worker.acquire("j1"))  # the bot itself is alive and the spawn is due any moment

    def test_pending_lock_whose_worker_never_started_is_taken_over(self):
        path = self.write_lock("j1", pid=os.getpid(), state="pending")
        self.age(path, worker.PENDING_STALE + 1)
        self.assertIsNotNone(worker.acquire("j1"))

    def test_dead_worker_pid_is_taken_over_at_once(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.write_lock("j1", pid=p.pid, state="running")
        self.assertIsNotNone(worker.acquire("j1"))

    def test_live_worker_without_heartbeat_is_taken_over(self):
        path = self.write_lock("j1", pid=os.getpid(), state="running")
        self.assertIsNone(worker.acquire("j1"))
        self.age(path, worker.HEARTBEAT_STALE + 1)
        self.assertIsNotNone(worker.acquire("j1"))

    def test_hard_cap_from_start_even_with_heartbeat(self):
        self.write_lock("j1", pid=os.getpid(), state="running", started=time.time() - worker.LOCK_STALE - 1)
        self.assertIsNotNone(worker.acquire("j1"))

    def test_legacy_bare_token_lock(self):
        path = worker.lock_path("j1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("abc")
        self.assertIsNone(worker.acquire("j1"))
        worker.release("j1", "abc")
        self.assertFalse(path.exists())

    def test_answer_timeout_still_spawns_the_worker(self):
        real = self.api

        def flaky(env, method, **params):
            if method == "answerCallbackQuery":
                raise TimeoutError("timed out")
            return real(env, method, **params)
        bot.api = flaky
        self.press("c:tailor:j1")
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("Готовлю резюме, 1-3 минуты", real.texts())

    def test_killed_worker_is_reported_and_its_lock_freed(self):
        token = worker.acquire("j1")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        worker._children.append({"proc": proc, "job_id": "j1", "token": token, "chat_id": CHAT})
        worker.mark("j1", token, proc.pid, "spawned")
        self.assertIsNone(worker.acquire("j1"))  # alive: busy
        proc.kill()
        proc.wait()
        bot.report_dead_workers({})
        self.assertEqual(self.api.texts()[-1], worker.DIED_TEXT)
        self.assertFalse(worker.lock_path("j1").exists())
        self.assertIsNotNone(worker.acquire("j1"))

    def test_clean_exit_is_not_reported(self):
        token = worker.acquire("j1")
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        worker._children.append({"proc": proc, "job_id": "j1", "token": token, "chat_id": CHAT})
        worker.release("j1", token)  # the worker released it itself
        self.assertEqual(worker.notices(), [])

    def test_worker_marks_itself_running_and_heartbeats(self):
        saved = worker.HEARTBEAT
        worker.HEARTBEAT = 0.05
        seen = {}

        def slow(job_id, text=None):
            path = worker.lock_path(job_id)
            seen["lock"] = json.loads(path.read_text())
            old = time.time() - 100
            os.utime(path, (old, old))
            time.sleep(0.3)
            seen["age"] = time.time() - path.stat().st_mtime
            return {"status": "need_text"}
        worker.TAILOR = slow
        try:
            worker.main("j1", CHAT, None, worker.acquire("j1"))
        finally:
            worker.HEARTBEAT = saved
        self.assertEqual((seen["lock"]["pid"], seen["lock"]["state"]), (os.getpid(), "running"))
        self.assertLess(seen["age"], 50)  # touched by the heartbeat
        self.assertFalse(worker.lock_path("j1").exists())


# ---------- .env and secrets (ops-03, ops-05) ----------

class EnvFile(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"
        self.saved = (bot.ENV_FILE, bot.api, bot.time.sleep, bot.store.DB, bot.ENV_WAIT)
        bot.ENV_FILE = self.path
        bot.store.DB = Path(self.tmp.name) / "state.db"

    def tearDown(self):
        bot.ENV_FILE, bot.api, bot.time.sleep, bot.store.DB, bot.ENV_WAIT = self.saved
        self.tmp.cleanup()

    def test_read_env_quotes_comments_and_bad_token(self):
        self.path.write_text(f'TELEGRAM_BOT_TOKEN="{TOKEN}"\nTELEGRAM_CHAT_ID=42 # me\nOTHER=a # b\n')
        env = bot.read_env()
        self.assertEqual((env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"], env["OTHER"]), (TOKEN, "42", "a # b"))
        self.path.write_text("TELEGRAM_BOT_TOKEN=111111:FAKE QA TOKEN\nTELEGRAM_CHAT_ID=42\n")
        err = io.StringIO()
        saved, sys.stderr = sys.stderr, err
        try:
            env = bot.read_env()
        finally:
            sys.stderr = saved
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("FAKE", err.getvalue())
        self.path.unlink()
        self.assertEqual(bot.read_env(), {})

    def test_mask(self):
        text = "InvalidURL: URL can't contain control characters. '/bot111111:FAKE-QA-TOKEN ' and " + TOKEN
        out = bot.mask(text)
        self.assertNotIn("FAKE-QA-TOKEN", out)
        self.assertNotIn(TOKEN, out)
        buf = io.StringIO()
        masked = bot._Masked(buf)
        print("https://api.telegram.org/bot" + TOKEN + "/getUpdates", file=masked)
        self.assertNotIn("AAAA", buf.getvalue())

    def test_wiped_env_is_picked_up_without_restart(self):
        self.path.write_text("")
        calls, sleeps = [], []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) == 1:
                self.path.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=42\n")
                t = time.time() + 5
                os.utime(self.path, (t, t))

        def fake(env, method, **params):
            calls.append((method, env.get("TELEGRAM_BOT_TOKEN")))
            if method == "getUpdates":
                raise SystemExit
            return {"ok": True, "result": []}
        bot.time.sleep, bot.api = sleep, fake
        err = io.StringIO()
        saved, sys.stderr = sys.stderr, err
        try:
            with self.assertRaises(SystemExit):
                bot.main()
        finally:
            sys.stdout, sys.stderr = getattr(sys.stdout, "stream", sys.stdout), saved
        self.assertEqual(sleeps, [bot.ENV_WAIT])
        self.assertIn(("getUpdates", TOKEN), calls)
        self.assertEqual(err.getvalue().count("missing in .env"), 1)  # once, not every 5 s

    def test_keyerror_or_401_rereads_env(self):
        self.path.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=42\n")
        reads, sleeps = [], []
        real_read = bot.read_env
        bot.read_env = lambda path=None: reads.append(1) or real_read(path)
        n = {"polls": 0}

        def fake(env, method, **params):
            if method == "getUpdates":
                n["polls"] += 1
                if n["polls"] == 1:
                    raise KeyError("TELEGRAM_BOT_TOKEN")
                if n["polls"] == 2:
                    raise http_error(429, b'{"ok":false,"parameters":{"retry_after":3000}}')
                raise SystemExit
            return {"ok": True}
        bot.time.sleep, bot.api = sleeps.append, fake
        err = io.StringIO()
        saved, sys.stderr = sys.stderr, err
        try:
            with self.assertRaises(SystemExit):
                bot.main()
        finally:
            bot.read_env = real_read
            sys.stdout, sys.stderr = getattr(sys.stdout, "stream", sys.stdout), saved
        self.assertEqual(len(reads), 2)          # the KeyError forced a fresh read of the whole .env
        self.assertEqual(sleeps, [5, 60])        # backed off; 429 retry_after 3000 capped at 60
        self.assertNotIn("AAAA", err.getvalue())

    def test_backoff(self):
        self.assertEqual(bot.backoff(http_error(429, b'{"ok":false,"parameters":{"retry_after":3000}}')), 60)
        self.assertEqual(bot.backoff(http_error(429, b'{"ok":false,"parameters":{"retry_after":3}}')), 4)
        self.assertEqual(bot.backoff(http_error(404)), 60)
        self.assertEqual(bot.backoff(http_error(502)), 5)
        self.assertEqual(bot.backoff(TimeoutError()), 5)


# ---------- ad text replies and stored text (data-6, cvtailor-02, cvtailor-09, botux-08) ----------

class AdText(Env):
    def ask(self, job="j4"):
        worker.TAILOR = lambda job_id, text=None: {"status": "need_text"}
        worker.main(job, CHAT, None, worker.acquire(job))
        return self.api.next_id

    def reply(self, asked, text):
        bot.handle_message({}, self.con, {"chat": {"id": CHAT}, "text": text, "reply_to_message": {"message_id": asked}})

    def test_reply_to_an_older_ask_starts_the_cv(self):
        first, second = self.ask(), self.ask()
        self.assertNotEqual(first, second)
        self.reply(first, AD)
        self.assertEqual([s[0] for s in self.spawned], ["j4"])
        self.assertEqual(self.con.execute("select count(*) from notes").fetchone()[0], 0)

    def test_cancel_the_question(self):
        asked = self.ask()
        self.reply(asked, "отмена")
        self.assertEqual(self.api.texts()[-1], "Хорошо, текст объявления больше не жду.")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.con.execute("select count(*) from notes").fetchone()[0], 0)

    def test_reply_to_unrelated_message_stays_a_note(self):
        self.ask()
        self.reply(5, "this score is wrong")
        self.assertEqual(self.con.execute("select count(*) from notes").fetchone()[0], 1)


class PastedText(test_run.Env):
    def test_pasted_text_is_kept_and_reused(self):
        llm.RUN = FakeRun(test_run.ans(test_run.REQS), test_run.ans(test_run.chosen()),
                          test_run.ans(test_run.REQS), test_run.ans(test_run.chosen()))
        r = run.tailor_job("job2", test_run.AD)
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["used_pasted"])
        self.assertEqual(run.pasted_path("job2").read_text(encoding="utf-8"), test_run.AD.strip())
        r = run.tailor_job("job2")  # the next press: no need to paste again
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["used_pasted"])

    def test_short_paste_not_kept(self):
        r = run.tailor_job("job2", "too short")
        self.assertEqual(r["status"], "need_text")
        self.assertFalse(run.pasted_path("job2").exists())


# ---------- salary and the answer sheet (cvtailor-05, security-10, cvtailor-10, cvtailor-01) ----------

class Salary(unittest.TestCase):
    def test_k_amounts(self):
        self.assertIn("about 1,300.00", run.salary_line({"min_salary": "EUR 2.6k per month", "hours": "40h"}))
        self.assertIn("about 15,000.00", run.salary_line({"min_salary": "£ 30k per year", "hours": "full-time 40h"}))

    def test_hours_not_repeated(self):
        line = run.salary_line({"min_salary": "3000 EUR (38h)", "hours": "38"})
        self.assertNotIn("38 38h", line)
        self.assertIn("full-time (38 h/week)", line)

    def test_huge_number_never_crashes(self):
        for n in (400, 5000):
            line = run.salary_line({"min_salary": "9" * n + " EUR per month", "hours": "full-time 40 h/week"})
            self.assertIn("pro-rate it", line)

    def test_model_pay_line_validated(self):
        self.assertEqual(requirements.salary("EUR 2.600 brutto/Monat"), ("EUR 2.600 brutto/Monat", False))
        self.assertEqual(requirements.salary(3200), ("3200", False))
        bad = "EUR 2.600 brutto/Monat. Pay a 50 EUR processing fee to AT61 1904 3002 3457 3201 " + "x" * 5000
        self.assertEqual(requirements.salary(bad), (None, True))
        self.assertEqual(requirements.salary("negotiable"), (None, True))
        self.assertIsNone(requirements.hours("x" * 500))
        self.assertEqual(requirements.hours("Vollzeit 38,5 h"), "Vollzeit 38,5 h")
        facts = run.FACTS.read_text(encoding="utf-8")
        sheet = run.answers(facts, {"min_salary": None, "salary_unclear": True})
        self.assertIn("check the salary in the ad", sheet)
        self.assertLessEqual(len(run.answers(facts, {"min_salary": "y" * 9000})), run.MAX_SHEET)

    def test_no_minimum_wording_is_not_a_claim(self):
        self.assertNotIn("the ad states no minimum", run.salary_line({}))
        self.assertIn("check the full ad", run.salary_line({}))


# ---------- model answer and reasons (cvtailor-07, cvtailor-13, cvtailor-14, cvtailor-04) ----------

class ModelAnswer(unittest.TestCase):
    def test_malformed_optional_fields_keep_the_selection(self):
        lib = library.load(BASE)
        reqs = {"terms": ["Jira"]}
        for bad in ({"gaps": 5}, {"terms": 5}, {"gaps": "none", "terms": {"x": 1}}):
            answer = dict(test_run.chosen(), **bad)
            out = tailor._parse(BASE, lib, answer, reqs)
            self.assertTrue(out["changes"], bad)

    def test_reasons_are_plain_words(self):
        r = run._reason(FileNotFoundError(2, "No such file or directory", "/nope/chrome"))
        self.assertNotIn("/nope", r)
        self.assertIn("не собрался PDF", r)
        self.assertEqual(run._reason(llm.LLMError("claude error: Claude AI usage limit reached|1760000000")),
                         "лимит Claude исчерпан, попробуй позже")
        self.assertEqual(run._reason(ValueError("secret /opt/job-digest/x")), "внутренняя ошибка (ValueError)")
        self.assertEqual(run._reason(run.TooLong("адаптированная версия не влезла на одну страницу")),
                         "адаптированная версия не влезла на одну страницу")

    def test_overuse_counts_only_what_tailoring_added(self):
        content = copy.deepcopy(BASE)
        self.assertEqual(run.overused(["research"], BASE, content), [])  # the base already has 4
        content["summary"] += " Research research research."
        self.assertEqual(run.overused(["research"], BASE, content), [])  # 3 added: within the rule
        content["summary"] += " Research."
        self.assertEqual(run.overused(["research"], BASE, content), ["research"])
        self.assertEqual(run.overused(["Kubernetes"], BASE, dict(content, summary="Kubernetes " * 4)), ["Kubernetes"])
        self.assertEqual(run.overused(["Jira"], BASE, dict(content, summary=content["summary"] + " Jira Jira Jira")),
                         ["Jira"])  # base has Jira once: the spec's absolute 3 still holds

    def test_analyse_is_not_german(self):
        self.assertEqual(checks.german_words("analyse data"), [])


class SetupFiles(test_run.Env):
    def test_damaged_files_name_themselves(self):
        tmp = Path(self.tmp.name)
        (tmp / "facts.md").write_text("", encoding="utf-8")
        with self.assertRaises(library.LibraryError) as ctx:
            run.load_facts(tmp / "facts.md")
        self.assertEqual(ctx.exception.file, "facts.md")
        with self.assertRaises(library.LibraryError) as ctx:
            run.load_facts(tmp / "missing.md")
        self.assertEqual(ctx.exception.file, "missing.md")
        (tmp / "never_claim.json").write_text("{}", encoding="utf-8")
        saved = checks.NEVER_CLAIM
        checks.NEVER_CLAIM = tmp / "never_claim.json"
        try:
            with self.assertRaises(library.LibraryError) as ctx:
                run.load_rules()
        finally:
            checks.NEVER_CLAIM = saved
        self.assertEqual(ctx.exception.file, "never_claim.json")
        self.assertIn("файл cv/never_claim.json", worker.setup_error_text(ctx.exception))
        self.assertIn("библиотека формулировок (cv/library.json) повреждена", worker.setup_error_text(library.LibraryError("library.json: x")))


# ---------- summary message (cvtailor-03, cvtailor-06, cvtailor-11, cvtailor-13) ----------

class Summary(Env):
    def r(self, **kw):
        out = {"counts": {"terms": 4, "claimable": 1, "before": 1, "after": 1}, "claimable": ["roadmap"],
               "changes": [], "gaps": [], "reverted": [], "unmet": ["SQL", "Python", "Tableau"]}
        out.update(kw)
        return out

    def test_unmet_terms_listed_by_code(self):
        text = worker.summary_text(self.r(), worker.role_names(BASE))
        self.assertIn("Чего нет (честно не закрыть):", text)
        self.assertIn("Нет в фактах: SQL, Python, Tableau", text)
        text = worker.summary_text(self.r(gaps=[{"requirement": "SQL skills", "ru": "SQL"}]), worker.role_names(BASE))
        self.assertIn("Нет в фактах: Python, Tableau", text)

    def test_rejected_answer_is_not_called_best(self):
        rev = [{"field": "summary", "text": "zzz", "base": "", "why": "library: unknown summary id 'zzz'"},
               {"field": "experience", "text": "zzz", "base": "", "why": "library: no role selection"}]
        text = worker.summary_text(self.r(reverted=rev), worker.role_names(BASE))
        self.assertIn("выбор модели не прошёл проверки", text)
        self.assertIn("Отклонено проверками: 2", text)
        self.assertNotIn("уже лучший вариант", text)

    def test_ats_warnings(self):
        text = worker.summary_text(self.r(pdf_issues=["Type3 font: X"]), worker.role_names(BASE))
        self.assertIn("Внимание: проверка PDF для ATS нашла проблемы (1): Type3 font: X", text)
        text = worker.summary_text(self.r(pdf_issues=["pdf text checks skipped: pdfinfo not installed"]),
                                   worker.role_names(BASE))
        self.assertIn("poppler", text)

    def test_fallback_has_the_answer_sheet_button(self):
        self.run_fallback()
        msg = self.api.of("sendMessage")[-1]
        labels = [b["text"] for r in msg["reply_markup"]["inline_keyboard"] for b in r]
        self.assertEqual(labels, ["Шпаргалка для анкеты", "Отправил резюме"])

    def run_fallback(self):
        r = self.fake_result("fallback", reason="не собрался PDF (FileNotFoundError)")
        worker.TAILOR = lambda job_id, text=None: r
        worker.main("j1", CHAT, None, worker.acquire("j1"))


# ---------- stale presses (botux-01, botux-06, data-10) ----------

class Stale(Env):
    def setUp(self):
        super().setUp()
        self.saved_pause, bot.RETRY_PAUSE = bot.RETRY_PAUSE, 0

    def tearDown(self):
        bot.RETRY_PAUSE = self.saved_pause
        super().tearDown()

    def toast(self):
        return self.api.of("answerCallbackQuery")[-1]["text"]

    def test_old_copy_verdict_press_refused(self):
        self.con.execute("update jobs set status='interview' where id='j1'")
        self.con.commit()
        self.press("v:not_interested:j1")
        self.assertEqual(self.toast(), bot.STALE)
        self.assertEqual((self.row("j1")["verdict"], self.row("j1")["status"]), ("interested", "interview"))
        self.assertIn("Интервью", self.api.of("editMessageText")[-1]["text"])  # the old copy now shows the truth

    def test_change_via_izmenit_still_works(self):
        self.press("e:j1")
        datas = [b["callback_data"] for r in self.api.of("editMessageText")[-1]["reply_markup"]["inline_keyboard"]
                 for b in r if "callback_data" in b]
        self.assertIn("v:not_interested:interested:j1", datas)
        self.press("v:not_interested:interested:j1")
        self.assertEqual(self.row("j1")["verdict"], "not_interested")

    def test_old_copy_status_press_refused_menu_allowed(self):
        self.con.execute("update jobs set status='interview' where id='j1'")
        self.con.commit()
        self.press("s:replied:j1")
        self.assertEqual(self.toast(), bot.STALE)
        self.assertEqual(self.row("j1")["status"], "interview")
        self.press("s:replied:j1", markup=bot.status_menu(self.row("j1"), "j1"))
        self.assertEqual(self.row("j1")["status"], "replied")

    def test_reasons_only_for_their_verdict(self):
        self.press("r:cant_fit:skill:j1")
        self.assertEqual(self.toast(), bot.STALE)
        self.assertEqual(self.row("j1")["reasons"], "[]")

    def test_same_verdict_again_keeps_reasons(self):
        self.press("v:cant_fit:j3")
        self.press("r:cant_fit:skill:j3")
        at = self.row("j3")["verdict_at"]
        self.press("v:cant_fit:j3")
        self.assertEqual(json.loads(self.row("j3")["reasons"]), ["skill"])
        self.assertEqual(self.row("j3")["verdict_at"], at)
        self.assertEqual(self.toast(), "Без изменений")


# ---------- edits on a flaky network (botux-02, botux-03, botux-11, botux-12, security-15) ----------

class Edits(Env):
    def setUp(self):
        super().setUp()
        self.saved_pause, bot.RETRY_PAUSE = bot.RETRY_PAUSE, 0
        self.con.execute("update jobs set url='https://example.com/job/1' where id='j1'")
        self.con.commit()

    def tearDown(self):
        bot.RETRY_PAUSE = self.saved_pause
        super().tearDown()

    def wrap(self, fail):
        real = self.api

        def api(env, method, **params):
            err = fail(method, params)
            if err:
                raise err
            return real(env, method, **params)
        bot.api = api

    def test_answer_timeout_still_edits(self):
        self.wrap(lambda m, p: urllib.error.URLError("timed out") if m == "answerCallbackQuery" else None)
        self.press("s:applied:j1")
        self.assertEqual(self.row("j1")["status"], "applied")
        self.assertIn("Отправил резюме", self.api.of("editMessageText")[-1]["text"])

    def test_unchanged_press_repairs_the_card(self):
        store.set_status(self.con, "j1", "applied")  # DB changed, card edit lost earlier
        self.press("s:applied:j1")
        self.assertEqual(self.api.of("answerCallbackQuery")[-1]["text"], "Без изменений")
        self.assertIn("s:replied:j1", json.dumps(self.api.of("editMessageText")[-1]["reply_markup"]))

    def test_rejected_link_retried_without_link_row(self):
        self.wrap(lambda m, p: http_error(400, b"BUTTON_URL_INVALID") if m.startswith("edit") and
                  "Открыть объявление" in json.dumps(p, ensure_ascii=False) else None)
        self.press("v:interested:j3")
        self.assertEqual(self.row("j3")["verdict"], "interested")
        self.press("s:applied:j1")
        self.assertEqual(len(self.api.of("editMessageText")), 2)

    def test_not_modified_is_quiet(self):
        self.wrap(lambda m, p: http_error(400, b'{"description":"Bad Request: message is not modified"}')
                  if m.startswith("edit") else None)
        self.press("v:interested:j1")  # no exception
        self.assertEqual(self.api.of("sendMessage"), [])

    def test_edit_failing_sends_a_fresh_card(self):
        self.wrap(lambda m, p: urllib.error.URLError("reset") if m.startswith("edit") else None)
        self.press("s:applied:j1")
        sends = self.api.of("sendMessage")
        self.assertEqual(len(sends), 1)
        self.assertIn("s:replied:j1", json.dumps(sends[0]["reply_markup"]))
        self.assertEqual(self.row("j1")["card_msg_id"], self.api.next_id)


# ---------- витрина (botux-04, data-9) ----------

class Vitrina(Env):
    def setUp(self):
        super().setUp()
        self.saved_pause, bot.PAUSE = bot.PAUSE, 0
        self.con.execute("delete from jobs")
        for i in range(9):
            score = 30 if i == 8 else 70 - i
            self.con.execute("insert into jobs (id, key, company, title, first_seen, sent_at, score, score_json, data)"
                             " values (?,?,?,?,?,?,?,?,?)",
                             (f"v{i}", f"v{i}", "ACME", f"Title {i}", store.now(), "2026-10-09T07:30:00", score,
                              json.dumps({"score": score, "why_ru": "ok"}), json.dumps({"title": f"Title {i}"})))
        self.con.commit()

    def tearDown(self):
        bot.PAUSE = self.saved_pause
        super().tearDown()

    def sent_ids(self):
        return [p["reply_markup"]["inline_keyboard"][-1][0]["callback_data"].split(":")[-1]
                for p in self.api.of("sendMessage") if "v:" in json.dumps(p.get("reply_markup") or {})]

    def test_round_never_repeats_and_counts_down(self):
        bot.handle_message({}, self.con, {"chat": {"id": CHAT}, "text": "/vitrina"})
        self.assertEqual(len(self.sent_ids()), 5)
        self.assertEqual(self.api.texts()[-1], "Осталось в витрине: 3")
        more_id = self.api.next_id
        self.press("w:more", message_id=more_id)
        ids = self.sent_ids()
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 8)  # nothing re-sent
        self.assertNotIn("v8", ids)  # score 30: not a card any more
        self.assertIn("все 8 карточек без отметки уже показаны", self.api.texts()[-1])

    def test_digest_line_skips_the_cards_it_just_sent(self):
        self.con.execute("update jobs set sent_at=? where id in ('v0','v1')", (store.now(),))
        self.con.commit()
        msg = {"message_id": 999, "chat": {"id": CHAT}, "date": int(time.time()),
               "text": "В витрине ещё 6 вакансий без отметки за 14 дней"}
        bot.handle_callback({}, self.con, {"id": "cq", "data": "w:more", "from": {"id": CHAT}, "message": msg})
        self.assertFalse({"v0", "v1"} & set(self.sent_ids()))


# ---------- commands, messages, lists (botux-08, botux-09, botux-10, data-11, data-12, security-11, security-13) ----------

class Messages(Env):
    def send(self, **m):
        bot.handle_message({}, self.con, dict({"chat": {"id": CHAT}}, **m))
        return self.api.texts()[-1]

    def notes(self):
        return self.con.execute("select count(*) from notes").fetchone()[0]

    def test_start_help_unknown_and_non_text(self):
        self.assertEqual(self.send(text="/start"), bot.HELP)
        self.assertEqual(self.send(text="/help"), bot.HELP)
        for cmd in ("/foo", "/Stats2", "/vitrina2"):
            self.assertEqual(self.send(text=cmd), bot.UNKNOWN_COMMAND)
        self.assertEqual(self.send(photo=[{"file_id": "x"}]), "Я понимаю только текст. Напиши сообщением.")
        self.assertEqual(self.send(text="​"), "Пустое сообщение, ничего не записал.")
        self.assertEqual(self.notes(), 0)

    def test_command_with_bot_name_and_case(self):
        self.assertTrue(self.send(text="/Stats@job_digest_bot").startswith("Воронка"))
        self.assertTrue(self.send(text="/interesting extra").startswith("<b>Интересно"))

    def test_long_note_says_it_was_cut(self):
        self.assertIn("первые 4000 символов из 5000", self.send(text="x" * 5000))

    def test_dashes_replaced_in_every_text(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            seen["data"] = req.data
            return Resp(b'{"ok": true}')
        saved = bot.urllib.request.urlopen
        bot.urllib.request.urlopen = urlopen
        try:
            self.saved[1]({"TELEGRAM_BOT_TOKEN": TOKEN}, "sendMessage", chat_id=1, text="PM – Data — AI")
        finally:
            bot.urllib.request.urlopen = saved
        self.assertIn(b"PM+-+Data+-+AI", seen["data"])

    def test_huge_link_does_not_break_the_list(self):
        self.con.execute("update jobs set url=? where id='j1'", ("https://example.com/" + "я" * 950,))
        self.con.commit()
        self.send(text="/interesting")
        text = self.api.texts()[-1]
        self.assertNotIn("<a href", text)
        self.assertLess(len(text), 4096)

    def test_list_refused_as_html_goes_as_plain_text(self):
        real = self.api

        def api(env, method, **params):
            if params.get("parse_mode") == "HTML":
                raise http_error(400, b"can't parse entities")
            return real(env, method, **params)
        bot.api = api
        self.send(text="/interesting")
        self.assertTrue(real.texts()[-1].startswith("Интересно, резюме ещё не отправлено"))

    def test_long_title_capped(self):
        row = dict(self.row("j1"), title="Product Manager " + "A" * 5000, company="B" * 6000)
        self.assertLess(len(bot.collapsed_text(row)), 400)
        self.assertLess(len(bot.full_text(dict(row, msg="<b>" + "x" * 5000 + "</b>"))), 400)

    def test_locked_database_is_retried(self):
        saved_sleep = bot.time.sleep
        bot.time.sleep = lambda s: None
        calls = {"n": 0}
        real = bot.handle_message

        def flaky(env, con, m):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(env, con, m)
        bot.handle_message = flaky
        try:
            bot.handle_update({}, self.con, {"message": {"chat": {"id": CHAT}, "text": "a wish"}}, str(CHAT))
        finally:
            bot.handle_message, bot.time.sleep = real, saved_sleep
        self.assertEqual(self.notes(), 1)

    def test_offset_survives_restart(self):
        bot._save_offset(self.con, 777)
        self.assertEqual(bot._load_offset(self.con), 777)


# ---------- which CV version was sent (data-13) ----------

class AppliedVersion(Env):
    def version(self):
        self.con.execute("insert into cv_versions (job_id, created_at, pdf_path, status) values ('j1', ?, '/x.pdf', 'ok')",
                         (store.now(),))
        self.con.commit()
        return self.con.execute("select max(id) from cv_versions").fetchone()[0]

    def log(self):
        path = worker.cv_dir() / "j1" / "applied.jsonl"
        return [json.loads(line)["version"] for line in path.read_text().splitlines()]

    def test_version_recorded_without_column(self):
        v1, v2 = self.version(), self.version()
        self.press(f"c:applied:j1:{v1}")
        self.assertEqual(self.row("j1")["status"], "applied")
        self.press(f"c:applied:j1:{v1}")
        self.assertEqual(self.api.of("answerCallbackQuery")[-1]["text"], "Без изменений")
        self.press(f"c:applied:j1:{v2}")
        self.assertEqual(self.api.of("answerCallbackQuery")[-1]["text"], "Записал: отправлена эта версия резюме")
        self.assertEqual(self.log(), [v1, v2])

    def test_version_recorded_in_columns_when_present(self):
        for table, col in (("jobs", "applied_cv"), ("status_log", "cv_version")):  # store.connect adds them since 09.10
            if col not in {r[1] for r in self.con.execute(f"pragma table_info({table})")}:
                self.con.execute(f"alter table {table} add column {col} integer")
        v1 = self.version()
        self.press(f"c:applied:j1:{v1}")
        self.assertEqual(self.row("j1")["applied_cv"], v1)
        self.assertEqual(self.con.execute("select cv_version from status_log where job_id='j1'").fetchone()[0], v1)

    def test_buttons_carry_the_version(self):
        self.assertEqual(worker.result_buttons("j1", 7)[-1][0]["callback_data"], "c:applied:j1:7")
        self.assertEqual(worker.result_buttons("j1", None)[-1][0]["callback_data"], "c:applied:j1")


if __name__ == "__main__":
    unittest.main()
