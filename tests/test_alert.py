"""QA round 2 alert fixes (ops2-01, pipeline2-04, data2-05, ops2-07, pipeline2-07, ops2-08).
scripts/alert.py sends one plain Telegram line without the token; run.sh routes failures there; a failed backup
is its own note line; the header shows at most 12 lines and keeps the rest queued. No network: urlopen is faked,
health runs on a scratch folder."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import health  # noqa: E402
import store  # noqa: E402
from scripts import alert  # noqa: E402

TOKEN = "123456:SECRET-TOKEN-XYZ"


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.body


class Alert(unittest.TestCase):
    """ops2-01: a plain line to Telegram even when digest.py cannot run."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=4242 # chat\n", encoding="utf-8")
        self.patch = mock.patch.object(alert, "ROOT", self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def run_main(self, argv, urlopen):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", urlopen), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = alert.main(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_sends_one_plain_text_message(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append((req, timeout))
            return FakeResponse(b'{"ok": true, "result": {}}')

        rc, log = self.run_main(["alert.py", "Подборка сегодня", "не отправилась"], fake_urlopen)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        req, timeout = calls[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(req.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        body = urllib.parse.parse_qs(req.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], ["4242"])
        self.assertEqual(body["text"], ["Подборка сегодня не отправилась"])
        self.assertNotIn("parse_mode", body)
        self.assertNotIn(TOKEN, log)

    def test_failed_send_exits_zero_and_hides_the_token(self):
        def broken(req, timeout=None):
            raise urllib.error.URLError(f"connection refused for bot{TOKEN}")

        rc, log = self.run_main(["alert.py", "текст"], broken)
        self.assertEqual(rc, 0)
        self.assertIn("telegram alert not sent", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn("SECRET", log)

    def test_missing_env_exits_zero(self):
        (self.root / ".env").unlink()

        def never(req, timeout=None):
            raise AssertionError("no request without .env")

        rc, log = self.run_main(["alert.py", "текст"], never)
        self.assertEqual(rc, 0)
        self.assertIn("telegram alert not sent", log)

    def test_empty_text_sends_nothing(self):
        def never(req, timeout=None):
            raise AssertionError("empty text must not be sent")

        self.assertEqual(self.run_main(["alert.py"], never)[0], 0)

    def test_run_sh_routes_failures_to_the_alert_script(self):
        """ops2-01: digest crash, skipped collect and a failed score.py each send the Telegram line."""
        text = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertTrue(os.access(ROOT / "run.sh", os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "run.sh")]).returncode, 0)
        self.assertIn('tg_alert() { python3 scripts/alert.py "$1"', text)
        collect = text.split("  collect)")[1].split("  send)")[0]
        self.assertIn("lock still held", collect)
        self.assertIn('tg_alert "Утренний сбор вакансий пропущен', collect)
        self.assertIn("step python3 score.py || stop_alert score.py", collect)
        send = text.split("  send)")[1].split("esac")[0]
        self.assertIn('elif [ "$digest_rc" -ne 0 ]; then', send)
        self.assertIn('tg_alert "Подборка сегодня не отправилась (ошибка на шаге отправки). Проверю и дошлю."', send)


class RunShCollectStop(unittest.TestCase):
    """A collect step that stops the run sends exactly one Telegram line (product follow-up)."""

    def test_every_stopping_step_goes_through_one_guarded_alert(self):
        text = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertTrue(os.access(ROOT / "run.sh", os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(ROOT / "run.sh")]).returncode, 0)
        collect = text.split("  collect)")[1].split("  send)")[0]
        self.assertIn("step python3 prefilter.py || { rc=$?; stop_alert prefilter.py; exit", collect)
        self.assertIn("step python3 prepare.py || { rc=$?; stop_alert prepare.py; exit", collect)
        self.assertIn("step python3 score.py || stop_alert score.py", collect)
        self.assertIn("trap 'on_err $LINENO' ERR", text)
        self.assertIn('[ "$ALERTED" = 0 ] || return 0', text)
        self.assertEqual(text.count("ALERTED=1"), 1)  # the only place that marks the run as alerted
        self.assertIn('tg_alert "Утренний сбор вакансий пропущен', collect)  # lock skip exits before any step
        self.assertEqual(collect.count("tg_alert"), 1)


class RunShBackup(unittest.TestCase):
    """pipeline2-04, data2-05, ops2-07: a failed backup is its own step with its own alert kind."""

    def test_backup_is_its_own_step_with_its_own_alert(self):
        text = (ROOT / "run.sh").read_text(encoding="utf-8")
        send = text.split("  send)")[1].split("esac")[0]
        self.assertIn("timeout 600 python3 scripts/backup_db.py", send)
        self.assertIn('python3 health.py backup-failed "$backup_rc"', send)
        self.assertLess(send.index("step python3 digest.py"), send.index("scripts/backup_db.py"))
        digest_part = send.split("# The backup is its own step")[0]
        self.assertNotIn("backup", digest_part)  # the digest branch never mentions the backup
        self.assertIn("health.py step", text)


class Health(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        (d / "data").mkdir()
        (d / "out").mkdir()
        self.out = d / "out"
        self.patches = [mock.patch.object(store, "DB", d / "data" / "state.db"),
                        mock.patch.object(store, "OUT", self.out)]
        for p in self.patches:
            p.start()
        # A clean morning: collect ran and finished, company feeds fresh and answered.
        health.start()
        health.record("collect", adzuna_calls=0, email_days=1, adzuna_days=1)
        health.finish()
        (self.out / "company_feeds.json").write_text(json.dumps({"asked": 1, "totals": {}}), encoding="utf-8")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.dir.cleanup()

    def test_clean_morning_has_no_lines(self):
        self.assertEqual(health.problems(), ([], []))

    def test_backup_failure_is_its_own_note_line(self):
        """pipeline2-04 / ops2-07: not under 'Сбои сбора', and not the 'digest did not go out' wording."""
        self.assertEqual(health.main(["health.py", "backup-failed", "1"]), 0)
        fails, notes = health.problems()
        self.assertEqual(fails, [])
        self.assertEqual(notes, [health.BACKUP_TEXT])
        self.assertEqual(health.BACKUP_TEXT, "Резервная копия базы не сделана (подборка ушла нормально).")
        self.assertEqual(health.pending_alerts()[0]["kind"], "backup")

    def test_backup_step_routed_by_name_is_a_backup_alert(self):
        """The same failure reaching step_failed (data2-05 repro) must not say the digest failed."""
        health.step_failed("send", 1, "python3 scripts/backup_db.py")
        fails, notes = health.problems()
        self.assertEqual((fails, notes), ([], [health.BACKUP_TEXT]))

    def test_digest_failure_is_still_a_failed_digest(self):
        health.step_failed("send", 1, "python3 digest.py")
        health.step_failed("send", 3, "python3 digest.py")  # digest wrote its own alert
        fails, notes = health.problems()
        self.assertEqual(len(fails), 1)
        self.assertIn("отправка подборки упала", fails[0])
        self.assertEqual(notes, [])
        self.assertEqual(len(health.pending_alerts()), 1)

    def test_fifteen_alerts_show_eleven_and_count_four(self):
        """pipeline2-07 / ops2-08: 12 lines max; the cut alerts are not cleared."""
        for i in range(15):
            health.alert(f"alert {i}")
        fails, notes = health.problems()
        self.assertEqual(len(fails), 12)
        self.assertTrue(fails[0].endswith("alert 0"))
        self.assertTrue(fails[10].endswith("alert 10"))
        self.assertEqual(fails[11], "и ещё 4")
        shown = health.pending_alerts()
        self.assertEqual(len(shown), 11)
        health.clear_alerts(shown)
        left = health.pending_alerts()
        self.assertEqual([a["text"] for a in left], [f"alert {i}" for i in range(11, 15)])
        fails, _ = health.problems()
        self.assertEqual(len(fails), 4)
        self.assertNotIn("и ещё", " ".join(fails))

    def test_backup_notes_capped_at_six_and_rest_stays_queued(self):
        for _ in range(7):
            health.backup_failed(1)
        fails, notes = health.problems()
        self.assertEqual(fails, [])
        self.assertEqual(len(notes), 6)
        self.assertEqual(notes[-1], "и ещё 2")
        health.clear_alerts(health.pending_alerts())
        self.assertEqual(len(health.pending_alerts()), 2)

    def test_clear_keeps_alerts_added_after_the_digest_read_the_queue(self):
        health.alert("old")
        shown = health.pending_alerts()
        health.alert("new while sending")
        health.clear_alerts(shown)
        self.assertEqual([a["text"] for a in health._load(health._alerts_path(), [])], ["new while sending"])


if __name__ == "__main__":
    unittest.main()
