"""Stage 11 (05.11.2026): is Claude still logged in on this server? One tiny call with the same CLI and tool switch as
score.py, run by run.sh before collect. The OAuth session expired on 05.11 and the morning cards came out without
scores before anyone noticed. An auth failure sends ONE Telegram line (scripts/alert.py) and sets a health flag. Any
other failure (timeout, rate limit, crash) only goes to the log: rate limits recover by themselves. Exit code is always
0, because run.sh runs this as a step and a failed check must not stop the morning."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import alert  # noqa: E402  (imports nothing from the app)
import health  # noqa: E402

# The same tool switch as score.py: a check call runs no tools either.
TOOLS_OFF = ("Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,NotebookRead,MultiEdit,LS,Task,TodoWrite,WebFetch,WebSearch,"
             "KillShell,BashOutput")
TIMEOUT = 90
# The markers live in enrich.py (auth_error) so that enrich and this check never drift. This short list is used only
# when enrich cannot be imported: the check must still run.
FALLBACK_MARKERS = ("Failed to authenticate", "OAuth", "/login", "not logged in")
MESSAGE = ("Claude на сервере разлогинился: сегодня бот не сможет оценить вакансии и найти описания. Войди заново: "
           "открой терминал и выполни ssh -t vps claude, затем /login (ссылку для входа скопирую тебе в чат, если не "
           "копируется).")


def is_auth_error(text):
    """enrich.auth_error (the one marker list); a minimal built-in check, logged, if enrich does not import."""
    try:
        import enrich  # imported here: enrich is heavy, and a broken enrich must not stop this check
        return enrich.auth_error(text)
    except Exception as e:
        print(f"claude check: enrich not loaded ({type(e).__name__}), built-in auth check used")
        return any(m.lower() in str(text).lower() for m in FALLBACK_MARKERS)


def classify(returncode, stdout, stderr):
    """(status, detail). 'auth' when enrich.auth_error finds a marker in stdout, stderr or the JSON reply's result;
    'ok' for a clean reply; 'other' for anything else. The detail is the reason, never the raw output."""
    text = f"{stdout}\n{stderr}"
    envelope = {}
    try:
        envelope = json.loads(stdout) if stdout.strip() else {}
    except ValueError:
        envelope = {}
    if isinstance(envelope, dict):
        text += f"\n{envelope.get('result') or ''}"
    if is_auth_error(text):
        return "auth", "auth"
    if returncode == 0 and isinstance(envelope, dict) and envelope and not envelope.get("is_error"):
        return "ok", "ok"
    return "other", f"exit {returncode}"


def run_check():
    """(status, detail) from one tiny Claude call. Never raises."""
    try:
        with tempfile.TemporaryDirectory(prefix="claude-check-") as cwd:  # never the app folder with .env
            out = subprocess.run(["claude", "-p", "Reply with OK", "--model", "haiku", "--output-format", "json",
                                  "--disallowedTools", TOOLS_OFF],
                                 input="", capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
    except subprocess.TimeoutExpired:
        return "other", "timeout"
    except Exception as e:  # claude missing, cannot start: a plain failure, logged
        return "other", type(e).__name__
    return classify(out.returncode, out.stdout, out.stderr)


def main():
    try:
        status, detail = run_check()
        print(f"claude check: {status} ({detail})")
        health.record("claude", status=status, detail=detail[:100])  # a report flag; never raises
        if status == "auth":
            sent = alert.send(MESSAGE)
            print(f"claude check: telegram alert {'sent' if sent else 'not sent'}")
    except Exception as e:  # the check is a report: a failure here is logged, the run goes on
        print(f"claude check failed: {type(e).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
