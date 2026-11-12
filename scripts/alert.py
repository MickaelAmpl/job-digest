"""One plain-text Telegram line from run.sh when a run fails (QA ops2-01): a broken DB or a crashed digest.py
must still reach Misha, so this file imports nothing from the app and never touches the DB.
Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from .env with the same parser as digest.load_env, forces IPv4
like digest.py, no parse_mode, 15 s timeout. Always exits 0: a failed alert is logged without the token.
CLI: python3 scripts/alert.py TEXT"""

import json
import re
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 15

# IPv6 from the server to Telegram stalls now and then (see digest.py); same IPv4-only lookup.
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **kw: [r for r in _orig_getaddrinfo(host, *a, **kw) if r[0] == socket.AF_INET] \
    or _orig_getaddrinfo(host, *a, **kw)


def load_env():
    """KEY=value lines; surrounding quotes and a trailing ' # comment' are not part of the value (as digest.py)."""
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            v = re.sub(r"\s+#.*$", "", v.strip()).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def redact(text, token):
    """The token never reaches a log line: urllib can put the request URL into an exception text."""
    text = str(text)
    if token:
        text = text.replace(token, "<token>")
    return re.sub(r"bot\d+:[\w-]+", "bot<token>", text)


def send(text):
    """True only when Telegram accepted the message. Never raises."""
    token = ""
    try:
        env = load_env()
        token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat:
            print("telegram alert not sent: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty in .env", file=sys.stderr)
            return False
        data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=TIMEOUT) as r:
            ok = bool(json.load(r).get("ok"))
        if not ok:
            print("telegram alert not accepted (no ok in the answer)", file=sys.stderr)
        return ok
    except Exception as e:  # missing .env, network, bad answer: logged, run.sh carries on
        print(f"telegram alert not sent: {type(e).__name__}: {redact(e, token)}", file=sys.stderr)
        return False


def main(argv):
    text = " ".join(argv[1:]).strip()
    if not text:
        print(__doc__, file=sys.stderr)
        return 0
    send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
