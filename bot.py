"""Always-on Telegram listener (systemd service): records Misha's button presses in
data/state.db and updates the buttons under each vacancy; re-sends unmarked cards from
the витрина 5 at a time ("Показать ещё 5" or /vitrina). Tracks applications on cards marked Интересно
(Отправил резюме -> Ответили -> Интервью -> Оффер / Отказ / Нет ответа) and answers /interesting, /applied, /rejected
(a button per job brings its card back), /stats, /report. Long polling, no open ports. Only the chat in TELEGRAM_CHAT_ID is served.

QA 09.10.2026 hardening: .env is re-read when it changes (a wiped and restored .env needs no restart) and no
token ever reaches a log; a press on an old copy of a card is checked against the current state instead of
overwriting it; every card edit ignores 'message is not modified', retries without a rejected link and falls
back to a fresh card; a busy database is retried instead of dropping the press.

Stage 8: /add records an application Misha sent by hand (store.add_manual); its confirmation becomes the card."""

import html
import json
import re
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from html import unescape as html_unescape
from pathlib import Path

import report
import store
from digest import (BORDER, LABELS, MONTHS, MORE_BUTTON, REASONS, STATUS_LABELS, card_item, job_message, plural,
                    reason_buttons, ru_date, verdict_buttons)

# IPv6 from this server to Telegram stalls now and then (a press hung for 2+ minutes on 07.10.2026);
# IPv4 answers in ~0.1 s. Resolve IPv4 only.
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **kw: [r for r in _orig_getaddrinfo(host, *a, **kw) if r[0] == socket.AF_INET] \
    or _orig_getaddrinfo(host, *a, **kw)

ENV_FILE = Path(__file__).parent / ".env"
REASON_LABELS = {k: dict(v) for k, v in REASONS.items()}
VITRINA_BATCH = 5
VITRINA_TRIES = 10  # cards tried per press: if Telegram rejects everything, give up instead of blocking ~100 s
PAUSE = 1.1  # Telegram: about one message per second in a private chat
RETRY_PAUSE = 1  # before one retry of a card edit after a network error; tests set 0
LIMIT = 3800  # Telegram rejects messages over 4096 characters; lists are split below that
MAX_BUTTONS = 100  # Telegram's cap on inline buttons in one message
BUTTON_TEXT = 60  # list buttons 'Company · Title' are cut to this many characters
TITLE_MAX, COMPANY_MAX = 200, 100  # a hostile 5000-char title must not make a card unsendable (QA security-13)
MAX_LINK = 1000  # an escaped link longer than this is shown as plain text in lists (QA botux-10, security-11)
NOTE_MAX = 4000
ENV_WAIT = 30  # seconds between .env re-reads while the token or chat id is missing
DB_TRIES = 3  # a press that meets a locked database is retried, not dropped (QA data-11)
COMMANDS = [("vitrina", "Витрина: карточки без отметки"), ("interesting", "Интересно, резюме ещё не отправлено"),
            ("applied", "Куда отправил резюме и что ответили"), ("rejected", "отказы и без ответа"),
            ("stats", "Цифры: воронка и отметки"), ("report", "Отчёт файлом"), ("add", "добавить отклик вручную")]
HELP = ("Каждое утро я присылаю новые вакансии, а под карточками записываю твои отметки.\n\nКоманды:\n" +
        "\n".join(f"/{c} - {d}" for c, d in COMMANDS) +
        "\n\nЛюбой другой текст я записываю как пожелание к отбору.")
UNKNOWN_COMMAND = "Не знаю такую команду. Есть: " + ", ".join(f"/{c}" for c, _ in COMMANDS) + "."
STALE = "Карточка устарела, открой свежую через /interesting"
CANCEL_WORDS = {"отмена", "отменить", "отмени", "cancel", "стоп", "не надо", "нет"}
# /add (stage 8): the questions the bot asks and the rules of a draft and of a question
ADD_ASK = "Пришли строку «Компания / Должность» и ссылку на вакансию, если есть. Ответь на это сообщение (или «отмена»)."
ADD_AGAIN = "Не понял. Пришли строку как «Компания / Должность», ссылку можно дописать. Ответь на это сообщение (или «отмена»)."
ADD_GIVE_UP = "Не получилось разобрать строку. Напиши /add и пришли её так: Компания / Должность"
EDIT_ASK = "Пришли строку целиком: «Компания / Должность» и ссылку, если есть. Ответь на это сообщение (или «отмена»)."
EDIT_GIVE_UP = "Не получилось разобрать строку, название не поменял. Нажми «Изменить» ещё раз."
DATE_ASK = "Какого числа отправил резюме? Напиши ДД.ММ, например 15.10, ответом на это сообщение."
DATE_BAD = "Не понял дату. Напиши ДД.ММ, например 15.10, или ДД.ММ.ГГГГ."
DATE_FUTURE = "Эта дата ещё не наступила. Напиши день не позже сегодняшнего."
ADD_PLACEHOLDER = "Компания / Должность"
DRAFT_TTL = timedelta(hours=24)  # an unsaved confirmation is dead after a day; finished drafts and closed asks keep 24 h
ASK_TTL = timedelta(minutes=10)  # a line that is not a reply goes to an open /add question this long (S8-6 c)
SAVE_ACTIONS = ("save", "this", "new")
REMOVED_NOTE = "Этот отклик убран. Добавить снова: /add"  # S8-1: a card of a removed application is a zombie
ASK_CLOSED = "Этот вопрос уже закрыт, начни заново: /add"  # S8-6 b: a reply to a question already answered or closed
EXPIRED_NOTE = "Черновик устарел, начни заново: /add"
DONE_NOTES = {"saved": "Уже сохранено", "cancelled": "Черновик отменён", "expired": EXPIRED_NOTE}  # S8-3 tombstones
OUTCOME_TOAST = {"created": "Записал отклик",
                 "attached": "Эта вакансия уже была в боте - отметил отклик на ней",
                 "unchanged": "Эта вакансия уже в откликах - ничего не менял",
                 "revived": "Вернул отклик"}
# Status buttons a card shows for each status (collapsed_markup): a press outside these comes from an old copy.
NEXT_STATUS = {None: {"applied"}, "applied": {"replied", "rejected", "no_answer"}, "replied": {"interview", "rejected"},
               "interview": {"offer", "rejected"}}
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
SECRET = re.compile(r"(?<=/bot)[^/\s'\"]+|\b\d{5,}:[A-Za-z0-9_-]{8,}")
ZERO_WIDTH = re.compile("[­​-‏‪-‮⁠-⁤﻿]")


# ---------- secrets, logs, .env ----------

def mask(text):
    """No part of a bot token in any log line (QA ops-05: http.client puts the request path into its errors)."""
    return SECRET.sub("***", str(text))


class _Masked:
    def __init__(self, stream):
        self.stream = stream

    def write(self, s):
        return self.stream.write(mask(s))

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def mask_streams():
    """Route stdout/stderr (prints and tracebacks) through mask()."""
    if not isinstance(sys.stdout, _Masked):
        sys.stdout = _Masked(sys.stdout)
    if not isinstance(sys.stderr, _Masked):
        sys.stderr = _Masked(sys.stderr)


def log(text):
    print(mask(text), file=sys.stderr, flush=True)


def read_env(path=None):
    """.env as a dict, read fresh. A missing file is {} (the caller waits for it). Quotes around a value and an
    inline '# comment' after the token or chat id are dropped; a token that is not '<digits>:<key>' is left out
    with a log line that never prints it, so it can never end up in a URL and then in an error text."""
    path = Path(path or ENV_FILE)
    env = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return env
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                v = re.sub(r"\s+#.*$", "", v)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1].strip()
            env[k] = v
    token = env.get("TELEGRAM_BOT_TOKEN")
    if token is not None and not TOKEN_RE.match(token):
        log("TELEGRAM_BOT_TOKEN in .env is malformed (expected '<digits>:<key>'); the value is not shown")
        env.pop("TELEGRAM_BOT_TOKEN")
    return env


def env_mtime(path=None):
    try:
        return Path(path or ENV_FILE).stat().st_mtime_ns
    except OSError:
        return None


# ---------- Telegram ----------

def dash(text):
    """Plain hyphens in everything the bot shows (Misha's rule; titles and Claude's text bring U+2013/U+2014)."""
    return text.replace("—", "-").replace("–", "-")


def api(env, method, **params):
    """A Bot API call. A (filename, bytes[, mime]) value is sent as a file upload (multipart), e.g. sendDocument."""
    for k in ("text", "caption"):
        if isinstance(params.get(k), str):
            params[k] = dash(params[k])
    fields = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/{method}"
    if any(isinstance(v, tuple) for v in fields.values()):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in fields.items():
            if isinstance(v, tuple):
                name, content, mime = v if len(v) == 3 else (*v, "text/html; charset=utf-8")
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                             f"Content-Type: {mime}\r\n\r\n".encode() + content + b"\r\n")
            else:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        req = urllib.request.Request(url, data=b"".join(parts) + f"--{boundary}--\r\n".encode(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    # Long polling waits up to 50 s by design; every other call must be quick or give up (an upload gets longer).
    with urllib.request.urlopen(req, timeout=60 if method in ("getUpdates", "sendDocument") else 10) as r:
        return json.load(r)


def tg_reason(e):
    """Telegram's own words for an HTTP error (body + reason), read once."""
    if not isinstance(e, urllib.error.HTTPError):
        return ""
    if not hasattr(e, "_tg_body"):
        try:
            e._tg_body = (e.read() or b"")[:300].decode("utf-8", "replace")
        except Exception:
            e._tg_body = ""
    return f"{e._tg_body} {e.reason or ''}".strip()


def describe_error(e):
    return f"HTTP {e.code}: {tg_reason(e)}" if isinstance(e, urllib.error.HTTPError) else f"{type(e).__name__}: {e}"


def not_modified(e):
    """Telegram's 400 for an edit that changes nothing (a double press): the message already shows it."""
    return isinstance(e, urllib.error.HTTPError) and e.code == 400 and "not modified" in tg_reason(e).lower()


def answer(env, cq, text=""):
    """Stop the spinner. Never raises: a timeout here must not abandon the press (QA ops-04, botux-02)."""
    try:
        api(env, "answerCallbackQuery", callback_query_id=cq["id"], text=text)
    except Exception as e:  # 'query is too old' after a late press, a timeout, a reset
        log(f"answerCallbackQuery failed: {describe_error(e)}")


def say(env, chat_id, text):
    """A plain message that must not break the caller: a failure is logged, the bot goes on. True when sent."""
    try:
        return bool(api(env, "sendMessage", chat_id=chat_id, text=text).get("ok"))
    except Exception as e:
        log(f"sendMessage failed: {describe_error(e)}")
        return False


def _without_link(markup):
    return [markup[1:]] if markup and markup[0] and markup[0][0].get("url") else []


def edit_message(env, msg, text=None, markup=None):
    """Edit a message in place; True when it now shows `text`/`markup`. 'message is not modified' counts as done
    (QA botux-12); a refused link is retried without the link row (QA botux-03, security-15); a network error
    or a 429/5xx is retried once (QA botux-02)."""
    markup = markup or []
    params = dict(chat_id=msg["chat"]["id"], message_id=msg["message_id"])
    for m in [markup] + _without_link(markup):
        for attempt in (1, 2):
            try:
                if text is not None:
                    api(env, "editMessageText", text=text, parse_mode="HTML", disable_web_page_preview="true",
                        reply_markup={"inline_keyboard": m}, **params)
                else:
                    api(env, "editMessageReplyMarkup", reply_markup={"inline_keyboard": m}, **params)
                return True
            except Exception as e:
                if not_modified(e):
                    return True
                log(f"edit of message {msg['message_id']} failed (try {attempt}): {describe_error(e)}")
                http = isinstance(e, urllib.error.HTTPError)
                if http and e.code < 500 and e.code != 429:
                    break  # refused as it is: next, the same without the link row
                if attempt == 1:
                    time.sleep(RETRY_PAUSE)
    return False


def send_card(env, con, chat_id, job_id, text, markup):
    """The card as a NEW message (with the link row, then without); it becomes card_msg_id. True when sent."""
    for m in [markup] + _without_link(markup):
        try:
            res = api(env, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                      disable_web_page_preview="true", reply_markup={"inline_keyboard": m})
        except Exception as e:
            log(f"card send failed for {job_id}: {describe_error(e)}")
            continue
        if res.get("ok"):
            con.execute("update jobs set card_msg_id=? where id=?", (res["result"]["message_id"], job_id))
            con.commit()
            return True
    return False


# ---------- card views ----------

def link_row(row):
    """The same link the digest used: the original posting if enrich found one, never a StepStone tracking link."""
    url = (store.usable_url(row["open_url"]) or store.usable_url(row["url"])) if row else ""
    return [[{"text": "Открыть объявление", "url": url}]] if url else []


def cap(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def marked_reasons(verdict, job_id, chosen):
    rows = reason_buttons(verdict, job_id)
    for r in rows[:-1]:
        key = r[0]["callback_data"].split(":")[2]
        if key in chosen:
            r[0]["text"] = "[x] " + r[0]["text"]
    return rows


def verdict_rows(job_id, current=None):
    """The four verdict buttons. Opened with 'Изменить' on a marked card they carry the mark they were opened from
    (v:<new>:<from>:<id>), so a press can tell a fresh 'Изменить' from an old copy of the card (QA botux-01)."""
    rows = verdict_buttons(job_id)
    if current:
        for r in rows:
            for b in r:
                kind, new, jid = b["callback_data"].split(":")
                b["callback_data"] = f"{kind}:{new}:{current}:{jid}"
    return rows


def collapsed_text(row):
    """One line that replaces the vacancy once Misha has decided: the message visually 'goes away'.
    On an interested job with an application status the line says where it is: 'Отправил резюме · 8 октября'."""
    try:
        reasons = json.loads(row["reasons"] or "[]")
    except ValueError:
        reasons = []
    text = "Отмечено: " + LABELS.get(row["verdict"], "?")
    reasons = [REASON_LABELS.get(row["verdict"], {}).get(r) for r in reasons if isinstance(r, str)]
    if any(reasons):  # a reason that does not belong to this verdict is not printed as a raw key (QA botux-06)
        text += " (" + ", ".join(r for r in reasons if r) + ")"
    status = row["status"] if row["verdict"] == "interested" else None
    if status == "applied":
        text = f"{STATUS_LABELS[status]} · {ru_date(row['applied_at'])}"
    elif status:
        text = f"{STATUS_LABELS.get(status, status)} · {ru_date(row['status_at'])}"
        if row["applied_at"]:
            text += f" (резюме {ru_date(row['applied_at'])})"
    if status and store.is_manual(row):  # stage 8: an application entered by hand
        text += " (вручную)"
    return (f"<i>{html.escape(text)}</i>\n{html.escape(cap(row['title'], TITLE_MAX))} · "
            f"{html.escape(cap(row['company'], COMPANY_MAX))}")


def full_text(row):
    """The card text as delivered (stored msg); a missing or oversized one becomes title and company."""
    msg = row["msg"]
    if msg and len(msg) <= LIMIT - len(PROMPT):
        return msg
    return f"<b>{html.escape(cap(row['title'], TITLE_MAX))}</b>\n{html.escape(cap(row['company'], COMPANY_MAX))}"


PROMPT = "\n\n<i>Почему? Отметь причины (можно несколько или ни одной) и нажми «Готово».</i>"


def gone_row(row, job_id):
    """'Это не отправлял' under a manual application (stage 8): removes the row after one more press."""
    return [[{"text": "Это не отправлял", "callback_data": f"m:gone:{job_id}"}]] if store.is_manual(row) else []


def _manual_source(row):
    """True for a row Misha entered by hand (source 'manual'); a plain dict without the column is not one."""
    try:
        return (row["source"] or "") == "manual"
    except (KeyError, IndexError):
        return False


def _drop_hidden(rows):
    """Rows without the buttons that are None (the 'Изменить' of a manual application, stage 8)."""
    return [[b for b in r if b] for r in rows if any(r)]


def collapsed_markup(row, job_id):
    """Buttons under a collapsed card. Interested jobs get 'Адаптировать резюме' with 'Написать письмо' (one row) and
    the next steps of the application; any other verdict only 'Изменить' (a status kept from an earlier 'Интересно'
    stays in the DB, hidden). A manual application (stage 8) has no CV or letter: it was sent already, and its verdict
    is not changed by hand, so it has no 'Изменить'."""
    manual = store.is_manual(row)
    edit = None if _manual_source(row) else {"text": "Изменить", "callback_data": f"e:{job_id}"}
    gone = gone_row(row, job_id)
    if row["verdict"] != "interested":
        return _drop_hidden(link_row(row) + [[edit]]) + gone
    b = lambda text, action: {"text": text, "callback_data": f"s:{action}:{job_id}"}
    tailor = [] if manual else [
        [{"text": "Адаптировать резюме", "callback_data": f"c:tailor:{job_id}"},  # stage 5
         {"text": "Написать письмо", "callback_data": f"c:letter:{job_id}"}]]    # stage 6
    menu = b("Изменить статус", "menu")
    steps = {None: [[b("Отправил резюме", "applied")], [edit]],
             # 'Изменить статус' added beside the spec's 'Изменить' so a mistaken 'Отправил' can be undone
             "applied": [[b("Ответили", "replied"), b("Отказ", "rejected"), b("Нет ответа", "no_answer")], [menu, edit]],
             "replied": [[b("Интервью", "interview"), b("Отказ", "rejected")], [menu]],
             "interview": [[b("Оффер", "offer"), b("Отказ", "rejected")], [menu]]}
    # offer / rejected / no_answer: final
    return link_row(row) + tailor + _drop_hidden(steps.get(row["status"], [[menu]])) + gone


def status_menu(row, job_id):
    """'Изменить статус': all six statuses (the current one ticked) and 'Сбросить'. Pressing the current
    status changes nothing and just closes the menu. A manual application has no 'not sent' state, so no 'Сбросить':
    'Это не отправлял' removes it instead (stage 8)."""
    b = lambda s: {"text": ("[x] " if row["status"] == s else "") + STATUS_LABELS[s], "callback_data": f"s:{s}:{job_id}"}
    reset = [] if store.is_manual(row) else [[{"text": "Сбросить", "callback_data": f"s:reset:{job_id}"}]]
    return link_row(row) + [[b("applied"), b("replied")], [b("interview"), b("offer")], [b("rejected"), b("no_answer")]] \
        + reset + gone_row(row, job_id)


def current_view(row, job_id):
    """(text, markup) of the card in the job's current state: collapsed once marked, else the full card."""
    if row["verdict"]:
        return collapsed_text(row), collapsed_markup(row, job_id)
    return full_text(row), link_row(row) + verdict_rows(job_id)


def verdict_view(row, job_id):
    """After a verdict press: the reason prompt (chosen reasons ticked) for cant_fit / bad_terms, else collapsed."""
    if row["verdict"] in REASONS:
        try:
            chosen = json.loads(row["reasons"] or "[]")
        except ValueError:
            chosen = []
        return full_text(row) + PROMPT, link_row(row) + marked_reasons(row["verdict"], job_id, chosen)
    return collapsed_text(row), collapsed_markup(row, job_id)


# ---------- витрина ----------

def _shown_at(r):
    return max(r["sent_at"] or "", r["vitrina_at"] or "")


def vitrina_rows(con):
    """Unmarked cards that may come back: the витрина minus cards whose current score fell under 50 (QA data-9)."""
    return [r for r in store.vitrina(con) if (store.score_of(r) or {}).get("score", 0) >= BORDER]


def _save_cycle(con, cycle):
    con.execute("insert or replace into meta (k, v) values ('vitrina_cycle', ?)", (json.dumps(cycle),))
    con.commit()


def vitrina_cycle(con, msg=None, new=False):
    """{start, msgs}: one round of 'Показать ещё 5'. A press on this round's own 'Осталось' message continues it;
    /vitrina starts a new round now; the digest's 'В витрине ещё N' line starts one that skips the cards that
    digest has just sent. A card counts as shown in the round once it was sent or re-sent after `start`
    (QA botux-04: the same cards came back and 'Осталось' never went down)."""
    try:
        cycle = json.loads(con.execute("select v from meta where k='vitrina_cycle'").fetchone()[0])
        cycle = cycle if isinstance(cycle, dict) and isinstance(cycle.get("start"), str) else None
    except (TypeError, ValueError):
        cycle = None
    mid = (msg or {}).get("message_id")
    if not new and cycle and mid and mid in (cycle.get("msgs") or []):
        return cycle
    start = store.now()
    if not new and msg and str(msg.get("text") or "").startswith("В витрине") and msg.get("date"):
        try:
            start = (datetime.fromtimestamp(msg["date"]) - timedelta(minutes=30)).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    cycle = {"start": start, "msgs": []}
    _save_cycle(con, cycle)
    return cycle


def show_vitrina(env, con, chat_id, call=None, msg=None, new=False):
    """Re-send the next VITRINA_BATCH unmarked cards not shown yet in this round (least recently shown first,
    then best score) with the usual buttons, then how many are left. A card counts as re-sent only when
    Telegram accepted it. Returns the ids sent. `call` is the Telegram call, swapped for a fake in tests."""
    call = call or api
    cycle = vitrina_cycle(con, msg, new)
    rows = sorted((r for r in vitrina_rows(con) if _shown_at(r) < cycle["start"]),
                  key=lambda r: (_shown_at(r), -(r["score"] or 0)))
    sent, tried, network = [], 0, 0
    for r in rows:
        if len(sent) == VITRINA_BATCH or tried == VITRINA_TRIES or network >= 2:
            break
        item = card_item(r)
        if not item:
            continue
        job = dict(item[0], title=cap(item[0].get("title"), TITLE_MAX), company=cap(item[0].get("company"), COMPANY_MAX),
                   location=cap(item[0].get("location"), COMPANY_MAX))
        text, buttons = job_message(job, *item[1:])
        tried += 1
        for markup in [buttons] + _without_link(buttons):  # bad link: no link row
            try:
                ok = call(env, "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                          disable_web_page_preview="true", reply_markup={"inline_keyboard": markup}).get("ok")
            except Exception as e:  # HTTP refusal, or a network error (QA botux-11): logged, the round goes on
                log(f"vitrina send failed for {r['id']}: {describe_error(e)}")
                ok = False
                if not isinstance(e, urllib.error.HTTPError):
                    network += 1
                    break
            if ok:
                con.execute("update jobs set vitrina_at=?, msg=? where id=?", (store.now(), text, r["id"]))
                con.commit()
                sent.append(r["id"])
                network = 0
                break
        time.sleep(PAUSE)
    left = len(rows) - len(sent)
    try:
        if left:
            res = call(env, "sendMessage", chat_id=chat_id, text=f"Осталось в витрине: {left}",
                       reply_markup={"inline_keyboard": MORE_BUTTON})
            if res.get("ok"):
                cycle["msgs"] = (cycle.get("msgs") or [])[-20:] + [res["result"]["message_id"]]
                _save_cycle(con, cycle)
        else:
            total = len(vitrina_rows(con))
            text = "Витрина разобрана" + (f": все {plural(total, 'карточка', 'карточки', 'карточек')} без отметки "
                                          "уже показаны. /vitrina - показать их заново." if total else "")
            call(env, "sendMessage", chat_id=chat_id, text=text)
    except Exception as e:
        log(f"vitrina footer failed: {describe_error(e)}")
    return sent


# ---------- CV tailoring (stage 5) ----------

# The texts per worker kind: 'cv' (stage 5) and 'letter' (stage 6). Both share one lock per job.
TAILOR_NOTES = {
    "cv": {"refused": "Резюме адаптируется только для вакансий с отметкой «Интересно»", "ok": "Готовлю резюме",
           "start": "Готовлю резюме, 1-3 минуты",
           "failed": "Не получилось запустить адаптацию резюме ({}). Попробуй ещё раз."},
    "letter": {"refused": "Письмо пишется только для вакансий с отметкой «Интересно»", "ok": "Пишу письмо",
               "start": "Пишу письмо, около минуты",
               "failed": "Не получилось запустить письмо ({}). Попробуй ещё раз."},
}
BUSY_NOTE = "Уже готовлю, подожди"


def prepare_tailor(con, job_id, kind="cv"):
    """(lock token, note): the token when a worker may start now; None with the refusal or 'busy' note.
    Checked before the press is answered, so the toast already says what happens. CV and letter runs of one job
    take the same lock, so they never overlap."""
    notes = TAILOR_NOTES[kind]
    row = con.execute("select verdict from jobs where id=?", (job_id,)).fetchone()
    if not row:
        return None, "Эта вакансия не найдена в базе"
    if row["verdict"] != "interested":
        return None, notes["refused"]
    try:
        from cv.tailor import worker  # imported here: a broken tailoring module must not stop the bot
        token = worker.acquire(job_id)
    except Exception as e:
        log(f"tailor lock failed for {job_id}: {type(e).__name__}: {e}")
        return None, notes["failed"].format(type(e).__name__)
    return (token, notes["ok"]) if token else (None, BUSY_NOTE)


def launch_tailor(env, chat_id, job_id, token, text=None, kind="cv"):
    """Spawn the detached worker (it sends the result itself) and say so; a failed spawn frees the lock and says that."""
    from cv.tailor import worker
    notes = TAILOR_NOTES[kind]
    try:
        worker.spawn(job_id, chat_id, token, text, kind=kind)
    except Exception as e:
        worker.release(job_id, token)
        log(f"tailor start failed for {job_id}: {type(e).__name__}: {e}")
        say(env, chat_id, notes["failed"].format(type(e).__name__))
        return
    print(f"tailor worker started for {job_id} ({kind})", flush=True)
    say(env, chat_id, notes["start"])


def start_tailor(env, con, chat_id, job_id, text=None):
    """The pasted-text path: no button to answer, every outcome is a message."""
    token, note = prepare_tailor(con, job_id)
    if token:
        launch_tailor(env, chat_id, job_id, token, text)
    else:
        say(env, chat_id, note)


def _removed(row):
    """A manual application Misha removed ('Это не отправлял'): its old cards must not come back (S8-1)."""
    return (row["closed_by"] or "") == "manual_removed"


def refresh_card(env, con, job_id, chat_id):
    """After 'Отправил резюме' on the CV message: the job's own card shows the applied state too. Its message id
    is known once any button on it was pressed (the tailor button always is); unknown or a failed edit: skip."""
    row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    if not row or not row["card_msg_id"] or _removed(row):
        return
    if not edit_message(env, {"chat": {"id": chat_id}, "message_id": row["card_msg_id"]},
                        collapsed_text(row), collapsed_markup(row, job_id)):
        log(f"card refresh skipped for {job_id}")  # too old or deleted


def version_answers(con, version_id):
    """The knockout answer sheet saved with that CV version (result.json next to its PDF), or ''."""
    row = con.execute("select pdf_path from cv_versions where id=?", (version_id,)).fetchone()
    try:
        return json.loads((Path(row["pdf_path"]).parent / "result.json").read_text(encoding="utf-8")).get("answers") or ""
    except (TypeError, OSError, ValueError):
        return ""


def link_version(con, job_id, version_id):
    """Which CV version went out with 'Отправил резюме' pressed under that CV message (QA data-13, spec stage 5:
    'which CV went to which company stays traceable'). Kept in jobs.applied_cv (and the newest 'applied' row of
    status_log.cv_version) once store.py has those columns, and always in data/cv/<job>/applied.jsonl.
    Returns True when the recorded version changed."""
    row = con.execute("select job_id from cv_versions where id=?", (version_id,)).fetchone()
    if not row or row["job_id"] != job_id:
        return False
    from cv.tailor import worker
    path = worker.cv_dir() / job_id / "applied.jsonl"
    try:
        last = json.loads(path.read_text(encoding="utf-8").splitlines()[-1]).get("version")
    except (OSError, ValueError, IndexError, AttributeError):
        last = None
    try:
        prev = con.execute("select applied_cv from jobs where id=?", (job_id,)).fetchone()[0]
        if prev == version_id:
            return False
        con.execute("update jobs set applied_cv=? where id=?", (version_id, job_id))
        try:
            con.execute("update status_log set cv_version=? where rowid=(select max(rowid) from status_log"
                        " where job_id=? and status='applied')", (version_id, job_id))
        except sqlite3.OperationalError:
            pass
        con.commit()
    except sqlite3.OperationalError as e:
        if "no such column" not in str(e):
            raise
        if last == version_id:
            return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"version": version_id, "at": store.now()}) + "\n")
    except OSError as e:
        log(f"applied version not written for {job_id}: {e}")
    return True


def handle_cv(env, con, cq):
    """c:tailor:<job> starts the CV worker, c:letter:<job> the cover-letter worker (same lock and steps, kind
    'letter'), c:ans:<version> sends that version's answer sheet, c:applied:<job>[:<version>] marks
    'Отправил резюме' from the CV message and records that version."""
    action, _, arg = cq["data"][2:].partition(":")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id") or cq["from"]["id"]
    print(f"press {cq['data']}", flush=True)
    note, then = "", None
    if action in ("tailor", "letter"):
        kind = "letter" if action == "letter" else "cv"
        if msg.get("message_id"):  # the press came from the job's card: remember it for refresh_card
            con.execute("update jobs set card_msg_id=? where id=?", (msg["message_id"], arg))
            con.commit()
        token, note = prepare_tailor(con, arg, kind)
        answer(env, cq, note)  # never raises: the lock is never left without a worker (QA ops-04)
        if token:
            launch_tailor(env, chat_id, arg, token, kind=kind)
        else:
            say(env, chat_id, note)  # a toast disappears: the refusal or 'busy' also stays in the chat
        return
    if action == "ans":
        sheet = version_answers(con, arg)
        note = "Шпаргалка" if sheet else "Шпаргалка не найдена"
        then = lambda: say(env, chat_id, ("Шпаргалка для анкеты\n\n" + cap(sheet, LIMIT - 30)) if sheet else
                           "Шпаргалка для этой версии не найдена. Нажми «Адаптировать резюме» ещё раз.")
    elif action == "applied":
        job_id, _, ver = arg.partition(":")
        row = con.execute("select verdict, status from jobs where id=?", (job_id,)).fetchone()
        if not row:
            note = "Эта вакансия не найдена в базе"
        elif row["verdict"] != "interested":
            note = "Статус ставится только на вакансии с отметкой «Интересно»"
        elif row["status"] not in (None, "applied"):
            note = "Статус уже: " + STATUS_LABELS.get(row["status"], row["status"])
        else:
            changed = store.set_status(con, job_id, "applied")
            linked = link_version(con, job_id, int(ver)) if ver.isdigit() else False
            note = ("Записал: " + STATUS_LABELS["applied"] if changed else
                    "Записал: отправлена эта версия резюме" if linked else "Без изменений")
            markup = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
            keep = [r for r in markup if not any(str(b.get("callback_data", "")).startswith("c:applied:") for b in r)]

            def then():
                if msg.get("message_id") and keep != markup:
                    edit_message(env, msg, None, keep)
                if changed:
                    refresh_card(env, con, job_id, chat_id)
    else:
        note = "Неизвестная кнопка"
    answer(env, cq, note)
    if then:
        then()


def resend_card(env, con, cq):
    """k:<job> from a /interesting or /applied list: the job's card again as a NEW message at the bottom of the chat,
    in its current state (the old card far up stays as it is). Interested: the collapsed card with 'Адаптировать резюме'
    and the status buttons; another verdict: its collapsed card; no verdict: the full card with the verdict buttons.
    The new message becomes card_msg_id, so 'Отправил резюме' from a CV message refreshes this one."""
    job_id = cq["data"][2:]
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id") or cq["from"]["id"]
    print(f"press {cq['data']}", flush=True)
    row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    note = "Эта вакансия не найдена в базе" if not row else (REMOVED_NOTE if _removed(row) else "")
    answer(env, cq, note)  # answer first: the spinner stops at once
    if note:  # an unknown job, or a removed application: no card comes back (S8-1)
        say(env, chat_id, note)
        return
    if not send_card(env, con, chat_id, job_id, *current_view(row, job_id)):
        say(env, chat_id, "Не получилось показать карточку, попробуй ещё раз.")


# ---------- card buttons ----------

def _json_dict(text):
    """A stored JSON object as a dict; anything else is an empty dict."""
    try:
        d = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _liveness(row):
    """data.liveness of a row as a dict (empty when the liveness check never wrote one)."""
    liveness = _json_dict(row["data"]).get("liveness")
    return liveness if isinstance(liveness, dict) else {}


def reopen_liveness(env, con, cq):
    """o:<job_id> on the Sunday list of vacancies the weekly liveness check closed (stage 9): Misha brings one back to
    'Интересно'. The check never closes it again (data.liveness.reopened_at). Only a row the check closed can be
    reopened; a second press on a reopened row is harmless."""
    job_id = cq["data"][2:]
    msg = cq.get("message") or {}
    print(f"press {cq['data']}", flush=True)
    row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    reopened = False
    if row is None:
        note = "Эта вакансия не найдена в базе"
    elif (row["closed_by"] or "") == "liveness":
        data = _json_dict(row["data"])
        liveness = _liveness(row)
        liveness["reopened_at"] = store.now()  # the rest of data.liveness (at, reason, url) stays
        data["liveness"] = liveness
        con.execute("update jobs set closed=0, closed_by=null, data=? where id=?",
                    (json.dumps(data, ensure_ascii=False), job_id))
        con.commit()
        note, reopened = "Вернул в «Интересно»", True
    elif not row["closed"] and "reopened_at" in _liveness(row):
        note, reopened = "Уже вернул в «Интересно»", True
    else:
        note = "Эту вакансию закрыл не я - не трогаю"
    answer(env, cq, note)
    if reopened and msg.get("message_id"):  # the pressed button goes; the other buttons of the list stay
        markup = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
        keep = [r for r in ([b for b in row_ if b.get("callback_data") != f"o:{job_id}"] for row_ in markup) if r]
        if keep != markup:
            edit_message(env, msg, None, keep)


def handle_callback(env, con, cq):
    data = cq.get("data") or ""
    if data.startswith("c:"):
        handle_cv(env, con, cq)
        return
    if data.startswith("k:"):
        resend_card(env, con, cq)
        return
    if data.startswith("m:"):  # /add (stage 8): before the job-id path below, its data is not a job id
        manual_button(env, con, cq)
        return
    if data.startswith("o:"):  # stage 9: the Sunday list of closed vacancies; before the job-id path below
        reopen_liveness(env, con, cq)
        return
    msg = cq.get("message") or {}
    if data == "w:more":
        print("press w:more", flush=True)
        answer(env, cq, "Отправляю")  # answer first: the spinner stops at once, sending 5 cards takes a few seconds
        show_vitrina(env, con, (msg.get("chat") or {}).get("id") or cq["from"]["id"], msg=msg)
        return
    parts = data.split(":")
    kind, job_id = parts[0], parts[-1]
    print(f"press {data}", flush=True)
    row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    text, markup, note, fallback = None, None, "", None
    if not row:
        note = "Эта вакансия не найдена в базе"
    elif _removed(row) and kind in ("v", "r", "s", "e"):  # S8-1: an old card of a removed application changes nothing
        note = REMOVED_NOTE
    elif kind == "v" and len(parts) in (3, 4):
        verdict, opened_from = parts[1], (parts[2] if len(parts) == 4 else None)
        if verdict not in LABELS:
            note = "Неизвестная кнопка"
        elif row["verdict"] == verdict:  # a double press, or the mark it already has: nothing to write (QA data-10)
            note = "Без изменений"
            text, markup = verdict_view(row, job_id)
        elif row["verdict"] != opened_from:  # an old copy of the card: its buttons predate the current mark
            note = STALE
            text, markup = current_view(row, job_id)
        else:
            con.execute("update jobs set verdict=?, reasons='[]', verdict_at=? where id=?", (verdict, store.now(), job_id))
            con.commit()
            row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
            text, markup = verdict_view(row, job_id)
            note = LABELS[verdict] + ": отметь причины" if verdict in REASONS else "Записал: " + LABELS[verdict]
    elif kind == "r" and len(parts) == 4:
        verdict, reason = parts[1], parts[2]
        if row["verdict"] != verdict:  # reasons belong to the verdict they were offered for (QA botux-06)
            note = STALE
            text, markup = current_view(row, job_id)
        elif reason == "done":
            text, markup = collapsed_text(row), collapsed_markup(row, job_id)
            note = "Записал"
        elif reason not in REASON_LABELS.get(verdict, {}):
            note = "Неизвестная кнопка"
        else:
            try:
                chosen = json.loads(row["reasons"] or "[]")
            except ValueError:
                chosen = []
            chosen = [c for c in chosen if c != reason] if reason in chosen else chosen + [reason]
            con.execute("update jobs set reasons=? where id=?", (json.dumps(chosen), job_id))
            con.commit()
            markup = link_row(row) + marked_reasons(verdict, job_id, chosen)
            fallback = full_text(row) + PROMPT
    elif kind == "e":
        text, markup = full_text(row), link_row(row) + verdict_rows(job_id, row["verdict"])
    elif kind == "s" and len(parts) == 3 and row["verdict"] != "interested":  # statuses only follow 'Интересно'
        note = "Статус ставится только на вакансии с отметкой «Интересно»"
        text, markup = current_view(row, job_id)
    elif kind == "s" and len(parts) == 3 and parts[1] == "menu":
        markup = status_menu(row, job_id)
        fallback = collapsed_text(row)
    elif kind == "s" and len(parts) == 3 and (parts[1] in store.STATUSES or parts[1] == "reset"):
        status = None if parts[1] == "reset" else parts[1]
        in_menu = "s:reset:" in json.dumps(msg.get("reply_markup") or {})
        if status != row["status"] and not in_menu and status not in NEXT_STATUS.get(row["status"], set()):
            note = STALE  # e.g. 'Ответили' on an old copy while the job is at 'Интервью' (QA botux-01)
            text, markup = current_view(row, job_id)
        else:
            changed = store.set_status(con, job_id, status)
            row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
            note = ("Записал: " + (STATUS_LABELS[status] if status else "статус сброшен")) if changed else "Без изменений"
            # also when nothing changed: closes an open menu and repairs a card left stale by a failed edit (QA botux-02)
            text, markup = collapsed_text(row), collapsed_markup(row, job_id)
    else:
        note = "Неизвестная кнопка"
    # Answer first: it stops the spinner. A late press makes it fail ("query is too old") - still edit the message.
    answer(env, cq, note)
    if msg.get("message_id") and (text is not None or markup is not None):
        if not edit_message(env, msg, text, markup):
            # the press is recorded, but the card could not show it: a fresh card at the bottom (QA botux-02/11)
            if row and send_card(env, con, msg["chat"]["id"], job_id, text if text is not None else fallback or
                                 collapsed_text(row), markup or []):
                log(f"card {job_id}: edit failed, sent a fresh card")


# ---------- lists and commands ----------

def _safe_line(line, as_html):
    """One list line, never longer than LIMIT and never cut inside a tag or an entity."""
    if len(line) <= LIMIT:
        return line
    if not as_html:
        return line[:LIMIT - 3] + "..."
    plain = html_unescape(re.sub(r"<[^>]*>", "", line))
    return html.escape(plain[:LIMIT - 200]) + "..."


def send_long(env, chat_id, lines, html=True, buttons=None):
    """Send lines as one message, or several split at line breaks, each under Telegram's 4096-character limit.
    `buttons` (one per line, or None) go under the part that holds their line, one per row, at most MAX_BUTTONS
    a message: a list press then lands on the same job as the line next to it. A part Telegram refuses as HTML
    goes again as plain text (QA botux-10). Returns True when every part was sent."""
    buttons = buttons or [None] * len(lines)
    chunk, kb, ok = "", [], [True]
    as_html = html

    def send():
        extra = {"reply_markup": {"inline_keyboard": [[b] for b in kb]}} if kb else {}
        tries = [({"parse_mode": "HTML"}, chunk)] if as_html else []
        tries.append(({}, html_unescape(re.sub(r"<[^>]*>", "", chunk)) if as_html else chunk))
        for mode, text in tries:
            try:
                api(env, "sendMessage", chat_id=chat_id, text=text, disable_web_page_preview="true", **mode, **extra)
                return
            except urllib.error.HTTPError as e:
                log(f"list part refused: {describe_error(e)}")
            except Exception as e:  # network: no point in a plain-text retry
                log(f"list part failed: {describe_error(e)}")
                break
        ok[0] = False

    for line, button in zip(lines, buttons):
        line = _safe_line(line, as_html)
        if chunk and (len(chunk) + len(line) + 1 > LIMIT or (button and len(kb) == MAX_BUTTONS)):
            send()
            time.sleep(PAUSE)
            chunk, kb = "", []
        chunk = f"{chunk}\n{line}" if chunk else line
        if button:
            kb.append(button)
    if chunk:
        send()
    return ok[0]


def job_line(row, extra=""):
    """'<a>Title</a> · Company · extra' for the /interesting and /applied lists (HTML)."""
    url = store.usable_url(row["open_url"]) or store.usable_url(row["url"])
    title = html.escape(dash((row["title"] or "")[:80]))
    link = html.escape(url, quote=True) if url else ""
    if link and len(link) <= MAX_LINK:  # a huge escaped link would cut the line inside its tag
        title = f'<a href="{link}">{title}</a>'
    return " · ".join(x for x in (title, html.escape(dash((row["company"] or "")[:40])), extra) if x)


def list_button(row):
    """'Company · Title' under a /interesting or /applied line: brings that job's card back (k:<id>), since old
    cards are far up in the chat. Plain hyphens, cut to BUTTON_TEXT characters."""
    text = " · ".join(x for x in ((row["company"] or "").strip(), (row["title"] or "").strip()) if x) or "Вакансия"
    text = dash(text)
    if len(text) > BUTTON_TEXT:
        text = text[:BUTTON_TEXT - 3].rstrip() + "..."
    return {"text": text, "callback_data": f"k:{row['id']}"}


def interesting_list(con, limit=30):
    """/interesting: marked Интересно, CV not sent yet, oldest mark first. Only open vacancies get a line and a button;
    a closed one (the posting is gone: 'feed' or 'enrich', or closed by a missing row) is counted in one plain last
    line instead, since its button only led to a dead link. A duplicate is neither listed nor counted: its original is.
    (lines, buttons): one button or None per line."""
    base = "verdict='interested' and status is null and coalesce(closed_by,'') != 'duplicate'"
    rows = con.execute(f"select * from jobs where {base} and coalesce(closed,0)=0 order by verdict_at").fetchall()
    gone = con.execute(f"select count(*) from jobs where {base} and coalesce(closed,0)=1").fetchone()[0]
    lines = [f"<b>Интересно, резюме ещё не отправлено: {len(rows)}</b>" if rows else
             "Нет вакансий с отметкой «Интересно» без отправленного резюме."]
    buttons = [None]
    lines += [job_line(r, f"оценка {r['score']}" if r["score"] is not None else "") for r in rows[:limit]]
    buttons += [list_button(r) for r in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"ещё {len(rows) - limit}")
        buttons.append(None)
    if gone:
        lines.append(f"Закрылись: {gone} (вакансии больше нет на сайте)")
        buttons.append(None)
    return lines, buttons


LIVE_STATUSES = ("applied", "replied", "interview", "offer")  # /applied: the application is still in play
CLOSED_STATUSES = ("rejected", "no_answer")  # /rejected (stage 10, Misha 03.11): shown apart from the live ones


def _applications(con, statuses):
    """Interested jobs with one of these application statuses, the oldest sent first."""
    marks = ",".join("?" * len(statuses))
    return con.execute(f"select * from jobs where verdict='interested' and status in ({marks})"
                       " order by applied_at is null, applied_at", statuses).fetchall()


def _grouped(rows, statuses, limit, today):
    """(lines, buttons) of rows grouped by status in the given order: the date sent and days since, one button per row;
    at most `limit` rows, the rest as 'ещё N'."""
    lines, buttons, shown = [], [], 0
    for status in statuses:
        group = [r for r in rows if r["status"] == status]
        if not group or shown >= limit:
            continue
        lines.append(f"\n<b>{html.escape(STATUS_LABELS[status])} ({len(group)})</b>")
        buttons.append(None)
        for r in group[:limit - shown]:
            days = report.days_since(r["applied_at"], today)
            when = (f"отправил {ru_date(r['applied_at'])}, {plural(days, 'день', 'дня', 'дней')} назад"
                    if days is not None else "дата отправки не записана")
            if store.is_manual(r):  # stage 8: sent by hand, not through a card
                when += " (вручную)"
            lines.append(job_line(r, when))
            buttons.append(list_button(r))
            shown += 1
    if len(rows) > shown:
        lines.append(f"\nещё {len(rows) - shown}")
        buttons.append(None)
    return lines, buttons


def applied_list(con, limit=40, today=None):
    """/applied: the live applications (Отправил резюме, Ответили, Интервью, Оффер), grouped in chain order. Rejected and
    no-answer ones are not listed here; a plain last line points to /rejected. (lines, buttons): one button or None per
    line."""
    today = today or date.today()
    live, closed = _applications(con, LIVE_STATUSES), _applications(con, CLOSED_STATUSES)
    if not live and not closed:
        return ["Пока ни одного отправленного резюме. Отметь «Отправил резюме» под карточкой с «Интересно»."], [None]
    if live:
        lines, buttons = [f"<b>Отправленные резюме: {len(live)}</b>"], [None]
        more_lines, more_buttons = _grouped(live, LIVE_STATUSES, limit, today)
        lines, buttons = lines + more_lines, buttons + more_buttons
    else:
        lines, buttons = ["Живых откликов нет."], [None]
    if closed:
        lines.append(f"Отказы и без ответа: {len(closed)} - /rejected")
        buttons.append(None)
    return lines, buttons


def rejected_list(con, limit=40, today=None):
    """/rejected: the rejected and no-answer applications, in the same line format and with the same card buttons as
    /applied. (lines, buttons): one button or None per line."""
    today = today or date.today()
    rows = _applications(con, CLOSED_STATUSES)
    if not rows:
        return ["Отказов нет."], [None]
    more_lines, more_buttons = _grouped(rows, CLOSED_STATUSES, limit, today)
    return [f"<b>Отказы и без ответа: {len(rows)}</b>"] + more_lines, [None] + more_buttons


def stats_lines(con):
    """/stats: the funnel, the marks, top reasons, the lists and the витрина (last line, as before)."""
    st = report.stats(con)
    lines = ["Воронка"] + [f"{label}: {n}" for label, n in report.funnel_rows(st)]
    lines += [f"Отказ: {st['rejected']} · Нет ответа: {st['no_answer']}", "", "Отметки"]
    lines += [f"{LABELS.get(v, 'Без отметки')}: {n}" for v, n in sorted(st["verdicts"].items(), key=lambda kv: -kv[1])]
    for verdict, counter in st["reasons"].items():
        if counter:
            top = ", ".join(f"{REASON_LABELS[verdict].get(k, k)} {n}" for k, n in counter.most_common(3))
            lines.append(f"Чаще всего «{LABELS[verdict]}»: {top}")
    listed = con.execute("select count(*) from jobs where sent_at is not null and listed=1").fetchone()[0]
    return lines + ["", f"В списках «Отсеял Claude»: {listed}", f"В витрине без отметки: {len(vitrina_rows(con))}"]


def send_report(env, con, chat_id):
    """/report: the HTML report as a file. If building or sending fails for any reason (Telegram refuses it,
    a timeout, a bug in the report), log it and say so instead of staying silent."""
    try:
        name = f"report-{date.today():%Y-%m-%d}.html"
        res = api(env, "sendDocument", chat_id=chat_id, document=(name, report.build(con).encode("utf-8")),
                  caption="Отчёт по поиску. Открой в браузере: таблицы сортируются, есть фильтр.")
        ok = res.get("ok")
        if not ok:
            log(f"sendDocument refused: {str(res)[:300]}")
    except Exception as e:  # HTTP error, timeout, network, or a bug while building the report
        log(f"/report failed: {type(e).__name__}: {e}")
        ok = False
    if not ok:
        say(env, chat_id, "Не получилось отправить отчёт, попробуй ещё раз через минуту.")


def command_of(text):
    """'/Vitrina@job_bot extra' -> '/vitrina'; None for anything that is not a slash command."""
    if not text.startswith("/") or len(text) < 2:
        return None
    return text.split()[0].split("@")[0].lower()


def asked_job(con, msg_id):
    """The job whose 'Описания вакансии нет' message (newest or any earlier one) Misha replied to, or None."""
    if not msg_id:
        return None
    try:
        from cv.tailor import worker
        return worker.asked_job(con, msg_id)
    except Exception as e:  # a broken tailoring module: still find the newest ask
        log(f"ask lookup failed: {type(e).__name__}: {e}")
        row = con.execute("select job_id from cv_requests where asked_msg_id=?", (msg_id,)).fetchone()
        return row["job_id"] if row else None


def ad_text(env, con, chat_id, job_id, text):
    """A reply to 'Описания вакансии нет': the ad text starts the CV; 'отмена' stops the question (QA botux-08)."""
    print(f"ad text for {job_id}: {len(text)} chars", flush=True)
    if not text:
        say(env, chat_id, "Пришли текст объявления сообщением")
        return
    if text.lower().strip(" .!") in CANCEL_WORDS:
        say(env, chat_id, "Хорошо, текст объявления больше не жду.")
        return
    try:
        from cv.tailor import worker
        if len(text) < worker.MIN_TEXT:  # checked here: a worker would only ask again
            worker.ask_for_text(api, env, chat_id, job_id, text)
            return
    except Exception as e:
        log(f"ask for ad text failed: {type(e).__name__}: {e}")
        say(env, chat_id, "Текст объявления слишком короткий. Пришли полный текст ответом на сообщение «Описания вакансии нет».")
        return
    start_tailor(env, con, chat_id, job_id, text)


# ---------- /add: an application sent by hand (stage 8) ----------
# store.add_manual does the writing; here: the parsing, the confirmation (a draft in manual_drafts until 'Сохранить'),
# the questions the bot asks (manual_requests: a reply to asked_msg_id is the answer) and 'Это не отправлял'.

URL_IN_TEXT = re.compile(r"https?://\S+", re.I)
SEPARATORS = (" / ", " | ", " - ")  # how parse_manual splits a line
LINE_MARKS = (" / ", " | ")  # what makes a non-reply message a /add line (' - ' is left out on purpose, S8-6 c)
DAY_IN_TEXT = re.compile(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}|\d{2}))?")


def parse_manual(text):
    """'Компания / Должность' with an optional link -> (company, title, url), or None when a part is missing.
    The first http(s) token anywhere is the link: it is stored and never opened, so /add makes no network call.
    The rest splits on the first newline, else on ' / ', ' | ' or ' - '."""
    text = text or ""
    m = URL_IN_TEXT.search(text)
    url = m.group(0).rstrip(".,;)") if m else ""
    rest = (text[:m.start()] + " " + text[m.end():] if m else text).strip()
    if "\n" in rest:
        company, title = rest.split("\n", 1)
    else:
        sep = next((s for s in SEPARATORS if s in rest), None)
        if sep is None:
            return None
        company, title = rest.split(sep, 1)
    company, title = " ".join(company.split()), " ".join(title.split())
    return (company, title, url) if company and title else None


def parse_day(text, today=None):
    """'ДД.ММ' -> 'YYYY-MM-DD': this year, or last year when that day is still ahead (28.11 typed in October is
    last November). 'ДД.ММ.ГГГГ' is taken as written and refused when it is in the future. ValueError when unclear."""
    today = today or date.today()
    m = DAY_IN_TEXT.fullmatch(text.strip(" ."))
    if not m:
        raise ValueError(DATE_BAD)
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            d = date(int(year) + (2000 if len(year) == 2 else 0), month, day)
        else:
            d = date(today.year, month, day)
            if d > today:
                d = d.replace(year=today.year - 1)
    except ValueError:  # 31.02, or 29.02 of a year without it
        raise ValueError(DATE_BAD) from None
    if d > today:
        raise ValueError(DATE_FUTURE)
    return d.isoformat()


def day_word(day):
    """'сегодня', 'вчера' or the Russian date of a 'YYYY-MM-DD' day; a day outside the current year keeps its year
    ('31 декабря 2025'), so a typed '31.12' in October cannot pass for this year's date (S8-6 d)."""
    today = date.today()
    d = date.fromisoformat(day)
    if d.year != today.year:
        return f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    if day == today.isoformat():
        return "сегодня"
    return "вчера" if day == (today - timedelta(days=1)).isoformat() else ru_date(day)


def _title_line(title, company):
    return f"<b>{html.escape(cap(title, TITLE_MAX))}</b> · {html.escape(cap(company, COMPANY_MAX))}"


def draft_view(con, token, d):
    """(text, markup) of the confirmation. Looked up again on every render: when the store finds the same job, its best
    hit is shown with [Это она] [Новая] instead of [Сохранить]; that hit's id is kept in the draft as d['candidate']."""
    hits = store.find_application_candidates(con, d["company"], d["title"], d.get("url") or "")
    best = hits[0] if hits else None
    d["candidate"] = best["id"] if best else None
    text = f"{_title_line(d['title'], d['company'])} · Отправил резюме: {day_word(d['day'])}"
    url = d.get("url") or ""
    if url and len(url) <= MAX_LINK:
        text += f'\n<a href="{html.escape(url, quote=True)}">Открыть объявление</a>'
    if best:
        text += f"\n\nПохоже, это уже есть: {_title_line(best['title'], best['company'])}"
    b = lambda label, action: {"text": label, "callback_data": f"m:{action}:{token}"}
    dates = [b("Вчера", "yday"), b("Другая дата", "date")]
    if not best:
        return text, [[b("Сохранить", "save")], dates, [b("Изменить", "edit"), b("Отмена", "cancel")]]
    if store.is_exact_candidate(con, d["company"], d["title"], best):  # the same key: 'Новая' cannot make a second row
        return text, [[b("Это она", "this"), b("Отмена", "cancel")], dates, [b("Изменить", "edit")]]
    return text, [[b("Это она", "this"), b("Новая", "new")], dates, [b("Изменить", "edit"), b("Отмена", "cancel")]]


def _send(env, chat_id, text, reply_markup=None, html_mode=False):
    """A message of the /add flow: its id, or None when Telegram refused it (logged, the press goes on)."""
    extra = {"reply_markup": reply_markup} if reply_markup else {}
    if html_mode:
        extra.update(parse_mode="HTML", disable_web_page_preview="true")
    try:
        res = api(env, "sendMessage", chat_id=chat_id, text=text, **extra)
    except Exception as e:
        log(f"/add message failed: {describe_error(e)}")
        return None
    return res["result"]["message_id"] if res.get("ok") else None


def _ask(env, con, chat_id, text, kind, token, placeholder):
    """A question the bot waits for: a reply to it is the answer. One open question per draft and kind."""
    msg_id = _send(env, chat_id, text, {"force_reply": True, "input_field_placeholder": placeholder})
    if msg_id is None:
        say(env, chat_id, "Не получилось задать вопрос, нажми ещё раз или напиши /add")
        return False
    if kind != "new":
        con.execute("delete from manual_requests where kind=? and token=?", (kind, token))
    con.execute("insert into manual_requests (asked_msg_id, kind, token, at) values (?,?,?,?)",
                (msg_id, kind, token, store.now()))
    con.commit()
    return True


def _consume(con, asked_id):
    """An answered question is marked 'done' and kept for 24 h: a second reply to it is not a note (S8-6 b)."""
    con.execute("update manual_requests set kind='done', at=? where asked_msg_id=? and kind!='done'",
                (store.now(), asked_id))
    con.commit()


def _close_requests(con, token):
    """The questions of a draft that is finished (saved or cancelled) are closed too."""
    con.execute("update manual_requests set kind='done', at=? where token=? and kind!='done'", (store.now(), token))


def _finish(con, token, d, why):
    """A finished draft is not deleted: a tombstone (json 'done') answers a late press for 24 h (S8-3)."""
    con.execute("update manual_drafts set json=?, created_at=? where token=?",
                (json.dumps(dict(d, done=why), ensure_ascii=False), store.now(), token))


def _prune(con):
    """Tombstones and closed questions older than 24 h are removed for good."""
    cutoff = (datetime.now() - DRAFT_TTL).isoformat(timespec="seconds")
    con.execute("delete from manual_drafts where created_at < ? and json like '%\"done\"%'", (cutoff,))
    con.execute("delete from manual_requests where kind='done' and at < ?", (cutoff,))
    con.commit()


def _draft(con, token, action):
    """(draft row, '') for a live draft; (None, the answer) when it is saved, cancelled, expired or unknown."""
    row = con.execute("select * from manual_drafts where token=?", (token,)).fetchone()
    if row is None:  # a second 'Сохранить' lands here; the other buttons only see a closed draft
        return None, "Уже сохранено" if action in SAVE_ACTIONS else "Эта запись уже закрыта"
    d = json.loads(row["json"] or "{}")
    if d.get("done"):  # a tombstone: the late press gets the answer of how the draft ended
        return None, DONE_NOTES.get(d["done"], "Эта запись уже закрыта")
    try:
        fresh = datetime.fromisoformat(row["created_at"]) > datetime.now() - DRAFT_TTL
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        _finish(con, token, d, "expired")
        con.commit()
        return None, EXPIRED_NOTE
    return row, ""


def _save_draft(con, token, d):
    con.execute("update manual_drafts set json=? where token=?", (json.dumps(d, ensure_ascii=False), token))
    con.commit()


def create_draft(env, con, chat_id, parsed):
    """The confirmation for a parsed line. Nothing is written until 'Сохранить' (or 'Это она' / 'Новая')."""
    company, title, url = parsed
    token = uuid.uuid4().hex[:10]
    d = {"company": company, "title": title, "url": url, "day": date.today().isoformat(), "candidate": None, "tries": 0}
    text, markup = draft_view(con, token, d)
    msg_id = _send(env, chat_id, text, {"inline_keyboard": markup}, html_mode=True)
    if msg_id is None:
        say(env, chat_id, "Не получилось показать, попробуй ещё раз: /add")
        return
    con.execute("insert into manual_drafts (token, chat, msg_id, created_at, json) values (?,?,?,?,?)",
                (token, str(chat_id), msg_id, store.now(), json.dumps(d, ensure_ascii=False)))
    con.commit()


def manual_start(env, con, chat_id, line):
    """/add or '/add <line>': the line is taken as it is; a line that does not parse is asked for once more."""
    if not line.strip():
        _ask(env, con, chat_id, ADD_ASK, "new", "", ADD_PLACEHOLDER)
        return
    parsed = parse_manual(line)
    if parsed:
        create_draft(env, con, chat_id, parsed)
    else:
        _ask(env, con, chat_id, ADD_AGAIN, "new", "again", ADD_PLACEHOLDER)


def looks_like_line(text):
    """A message that can be a /add line: a link, a line break, ' / ' or ' | ' (not ' - ': a wish with a dash is a note,
    S8-6 c). Anything else that is not a reply stays a note, even while an /add question is open."""
    return bool(URL_IN_TEXT.search(text)) or "\n" in text or any(s in text for s in LINE_MARKS)


def open_add_question(con):
    """The newest open /add question (kind 'new') of the last 10 minutes, or None."""
    cutoff = (datetime.now() - ASK_TTL).isoformat(timespec="seconds")
    return con.execute("select * from manual_requests where kind='new' and at>=? order by at desc limit 1",
                       (cutoff,)).fetchone()


def close_asks(con):
    """Any command closes the open /add questions: a command is never a line for them, and a late reply to one of
    them is answered 'закрыт', not saved as a note (S8-6 b)."""
    con.execute("update manual_requests set kind='done', at=? where kind='new'", (store.now(),))
    con.commit()


def manual_reply(env, con, chat_id, text, reply_to):
    """A text that answers one of the /add questions. A reply to its asked_msg_id is the answer (a reply to a question
    already answered or closed says so, S8-6 b). A message that is not a reply goes to the newest open question of the
    last 10 minutes only when it looks like a line, or when it is exactly a cancel word (S8-6 a, c). True when consumed:
    it is then never a note."""
    _prune(con)
    cancel = text.lower().strip(" .!") in CANCEL_WORDS
    if reply_to:
        req = con.execute("select * from manual_requests where asked_msg_id=?", (reply_to,)).fetchone()
        if req is None:
            return False
        if req["kind"] == "done":
            say(env, chat_id, ASK_CLOSED)
            return True
    else:
        req = open_add_question(con)
        if req is None or not (cancel or looks_like_line(text)):
            return False
    if cancel:
        _consume(con, req["asked_msg_id"])
        say(env, chat_id, "Хорошо, отменил.")
        return True
    if req["kind"] == "new":
        _add_reply(env, con, chat_id, text, req)
    elif req["kind"] == "date":
        _day_reply(env, con, chat_id, text, req)
    else:
        _edit_reply(env, con, chat_id, text, req)
    return True


def _add_reply(env, con, chat_id, text, req):
    _consume(con, req["asked_msg_id"])
    parsed = parse_manual(text)
    if parsed:
        create_draft(env, con, chat_id, parsed)
    elif req["token"] == "again":  # the second bad line: stop, with a hint
        say(env, chat_id, ADD_GIVE_UP)
    else:
        _ask(env, con, chat_id, ADD_AGAIN, "new", "again", ADD_PLACEHOLDER)


def _day_reply(env, con, chat_id, text, req):
    row, note = _draft(con, req["token"], "date")
    if row is None:
        _consume(con, req["asked_msg_id"])
        say(env, chat_id, note)
        return
    try:
        day = parse_day(text)
    except ValueError as e:  # the question stays open for another try
        say(env, chat_id, str(e))
        return
    _consume(con, req["asked_msg_id"])
    d = json.loads(row["json"] or "{}")
    d["day"] = day
    _rerender(env, con, None, row, d)


def _edit_reply(env, con, chat_id, text, req):
    row, note = _draft(con, req["token"], "edit")
    if row is None:
        _consume(con, req["asked_msg_id"])
        say(env, chat_id, note)
        return
    _consume(con, req["asked_msg_id"])
    d = json.loads(row["json"] or "{}")
    parsed = parse_manual(text)
    if parsed:  # the whole line is replaced, its link included
        d.update(company=parsed[0], title=parsed[1], url=parsed[2], tries=0)
        _rerender(env, con, None, row, d)
    elif d.get("tries"):  # the second bad line: the draft stays as it was
        d["tries"] = 0
        _save_draft(con, row["token"], d)
        say(env, chat_id, EDIT_GIVE_UP)
    else:
        d["tries"] = 1
        _save_draft(con, row["token"], d)
        _ask(env, con, chat_id, ADD_AGAIN, "edit", row["token"], ADD_PLACEHOLDER)


def _rerender(env, con, cq, row, d, note=""):
    """The same confirmation message with the draft's new state (a new day, a new line)."""
    text, markup = draft_view(con, row["token"], d)
    _save_draft(con, row["token"], d)
    if cq is not None:
        answer(env, cq, note)
    if not edit_message(env, {"chat": {"id": row["chat"]}, "message_id": row["msg_id"]}, text, markup):
        msg_id = _send(env, row["chat"], text, {"inline_keyboard": markup}, html_mode=True)  # the old message is gone
        if msg_id is not None:
            con.execute("update manual_drafts set msg_id=? where token=?", (msg_id, row["token"]))
            con.commit()


def _save(env, con, cq, row, d, attach_to, force_new=False):
    """'Сохранить', 'Это она' (attach_to = the candidate) or 'Новая' (force_new: a new row, never an attach): the
    application and the draft's removal in one transaction; the confirmation becomes the application's card."""
    job = {"company": d["company"], "title": d["title"], "url": d.get("url") or ""}
    try:
        _finish(con, row["token"], d, "saved")  # a tombstone: a late press says 'Уже сохранено' (S8-3)
        _close_requests(con, row["token"])
        jid, outcome = store.add_manual(con, job, d["day"], attach_to=attach_to, force_new=force_new)
        con.execute("update jobs set card_msg_id=? where id=?", (row["msg_id"], jid))
        card = con.execute("select * from jobs where id=?", (jid,)).fetchone()
        con.commit()
    except ValueError:  # a day past today, or the row to attach to is gone: the confirmation is shown afresh
        con.rollback()
        _rerender(env, con, cq, row, d, "Не получилось записать, попробуй ещё раз")
        return
    except Exception:
        con.rollback()
        raise
    answer(env, cq, OUTCOME_TOAST[outcome])
    text, markup = collapsed_text(card), collapsed_markup(card, jid)
    if not edit_message(env, {"chat": {"id": row["chat"]}, "message_id": row["msg_id"]}, text, markup):
        send_card(env, con, row["chat"], jid, text, markup)


def _cancel(env, con, cq, row, d):
    _finish(con, row["token"], d, "cancelled")  # a late press says 'Черновик отменён' (S8-3)
    _close_requests(con, row["token"])
    con.commit()
    answer(env, cq, "Отменил")
    edit_message(env, {"chat": {"id": row["chat"]}, "message_id": row["msg_id"]},
                 "<i>Не записал</i>\n" + _title_line(d["title"], d["company"]), [])


def manual_button(env, con, cq):
    """m:<action>:<token>: the buttons of the /add confirmation. Answered before anything else (the spinner stops)."""
    parts = (cq.get("data") or "").split(":", 2)
    if len(parts) != 3:
        answer(env, cq, "Неизвестная кнопка")
        return
    _, action, token = parts
    print(f"press {cq['data']}", flush=True)
    _prune(con)
    if action in ("gone", "gonedo", "gonekeep"):  # here the last part is a job id
        manual_gone(env, con, cq, action, token)
        return
    row, note = _draft(con, token, action)
    if row is None:
        answer(env, cq, note)
        return
    d = json.loads(row["json"] or "{}")
    if action in SAVE_ACTIONS:
        _save(env, con, cq, row, d, attach_to=d.get("candidate") if action == "this" else None,
              force_new=action == "new")
    elif action == "yday":
        d["day"] = (date.today() - timedelta(days=1)).isoformat()
        _rerender(env, con, cq, row, d, "Дата: вчера")
    elif action == "date":
        answer(env, cq, "Напиши дату")
        _ask(env, con, row["chat"], DATE_ASK, "date", token, "ДД.ММ")
    elif action == "edit":
        d["tries"] = 0
        _save_draft(con, token, d)
        answer(env, cq, "Пришли строку")
        _ask(env, con, row["chat"], EDIT_ASK, "edit", token, ADD_PLACEHOLDER)
    elif action == "cancel":
        _cancel(env, con, cq, row, d)
    else:
        answer(env, cq, "Неизвестная кнопка")


def manual_gone(env, con, cq, action, job_id):
    """'Это не отправлял' on a manual application: the first press asks, 'Да, не отправлял' removes the row
    (store.remove_manual), 'Нет, оставить' brings the card back as it was."""
    msg = cq.get("message") or {}
    row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    if row is None:
        answer(env, cq, "Эта вакансия не найдена в базе")
        return
    if not store.is_manual(row):
        answer(env, cq, "Этот отклик не ручной")
        return
    if action == "gone":
        answer(env, cq, "")
        text = collapsed_text(row) + "\n\n<i>Убрать этот отклик? Он пропадёт из списков откликов и статистики.</i>"
        markup = [[{"text": "Да, не отправлял", "callback_data": f"m:gonedo:{job_id}"},
                   {"text": "Нет, оставить", "callback_data": f"m:gonekeep:{job_id}"}]]
    elif action == "gonekeep":
        answer(env, cq, "Оставил")
        text, markup = current_view(row, job_id)
    else:
        removed = store.remove_manual(con, job_id)
        con.commit()
        row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
        answer(env, cq, "Убрал отклик" if removed else "Уже убрано")
        if row["closed_by"] == "manual_removed":
            text, markup = "<i>Убрал: отклик не отправлял</i>\n" + _title_line(row["title"], row["company"]), []
        else:  # an application attached to a card of Misha's keeps the card and its 'Интересно' mark
            text, markup = current_view(row, job_id)
    if msg.get("message_id"):
        edit_message(env, msg, text, markup)


def handle_message(env, con, m):
    raw = m.get("text")
    text = (raw or "").strip()
    chat_id = m["chat"]["id"]
    cmd = command_of(text)
    if cmd:  # any command, known or not, closes an open /add question (stage 8)
        close_asks(con)
    job_id = asked_job(con, (m.get("reply_to_message") or {}).get("message_id"))
    if job_id and not cmd:  # a reply to any 'Описания вакансии нет' for the job (QA data-6); a /command stays a command
        ad_text(env, con, chat_id, job_id, text)
        return
    if text and not cmd and manual_reply(env, con, chat_id, text, (m.get("reply_to_message") or {}).get("message_id")):
        return  # an answer to a /add question (stage 8): never a note
    if cmd == "/vitrina":
        show_vitrina(env, con, chat_id, new=True)
    elif cmd in ("/interesting", "/applied", "/rejected"):
        build = {"/interesting": interesting_list, "/applied": applied_list, "/rejected": rejected_list}[cmd]
        lines, buttons = build(con)
        if not send_long(env, chat_id, lines, buttons=buttons):
            say(env, chat_id, "Не получилось отправить список, попробуй ещё раз через минуту.")
    elif cmd == "/stats":
        if not send_long(env, chat_id, stats_lines(con), html=False):
            say(env, chat_id, "Не получилось отправить цифры, попробуй ещё раз через минуту.")
    elif cmd == "/report":
        send_report(env, con, chat_id)
    elif cmd == "/add":
        parts = text.split(None, 1)
        manual_start(env, con, chat_id, parts[1] if len(parts) > 1 else "")
    elif cmd in ("/start", "/help"):
        say(env, chat_id, HELP)
    elif cmd == "/cancel":
        say(env, chat_id, "Хорошо. Если я ждал текст объявления - больше не жду.")
    elif cmd:  # a typo or an unknown command is not a wish for the scoring notes (QA data-12)
        say(env, chat_id, UNKNOWN_COMMAND)
    elif raw is None:  # photo, voice, sticker, document (QA botux-08)
        say(env, chat_id, "Я понимаю только текст. Напиши сообщением.")
    elif not ZERO_WIDTH.sub("", text).strip():
        say(env, chat_id, "Пустое сообщение, ничего не записал.")
    else:
        # Free text = a correction or a wish ("this rejection was wrong"); kept for Claude to read and act on.
        reply_to = ((m.get("reply_to_message") or {}).get("text") or "")[:500]  # which vacancy/list he answered
        con.execute("insert into notes (at, text, reply_to) values (?, ?, ?)", (store.now(), text[:NOTE_MAX], reply_to))
        con.commit()
        print(f"note saved: {text[:80]!r}", flush=True)
        cut = f" Сохранил первые {NOTE_MAX} символов из {len(text)}." if len(text) > NOTE_MAX else ""
        say(env, chat_id, "Записал. Учту при следующей настройке отбора." + cut)


# ---------- the polling loop ----------

def handle_update(env, con, u, me):
    """One update; a locked database is retried with a pause instead of dropping the press (QA data-11)."""
    for attempt in range(DB_TRIES):
        try:
            if "callback_query" in u:
                cq = u["callback_query"]
                if str(cq["from"]["id"]) == me:
                    handle_callback(env, con, cq)
            elif "message" in u and str(u["message"]["chat"]["id"]) == me:
                handle_message(env, con, u["message"])
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            log(f"database is locked (try {attempt + 1} of {DB_TRIES}), retrying")
            time.sleep(min(10, 2 ** attempt))
    say(env, me, "База занята, нажатие не записано. Нажми ещё раз через минуту.")


def backoff(e):
    """Seconds to wait after a failed poll: honour 429 retry_after (capped), back off on a bad token or a second
    poller, a little on 5xx (QA ops-12: they were all retried after 1 s)."""
    if not isinstance(e, urllib.error.HTTPError):
        return 5
    if e.code == 429:
        tg_reason(e)  # reads the body once
        try:
            return min(60, int(json.loads(e._tg_body).get("parameters", {}).get("retry_after", 5)) + 1)
        except (ValueError, TypeError, AttributeError):
            return 10
    if e.code in (401, 404):
        return 60
    if e.code == 409:
        return 30
    return 5 if e.code >= 500 else 1


def _load_offset(con):
    try:
        row = con.execute("select v from meta where k='bot_offset'").fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError, TypeError):
        return 0


def _save_offset(con, offset):
    """Confirmed updates survive a restart, so a press is not replayed (QA ops-12)."""
    try:
        con.execute("insert or replace into meta (k, v) values ('bot_offset', ?)", (str(offset),))
        con.commit()
    except sqlite3.Error as e:
        log(f"offset not saved: {e}")


def report_dead_workers(env):
    """A CV worker that died without cleanup (killed, OOM) is reported, and its lock is already free (QA ops-08)."""
    try:
        from cv.tailor import worker
        for job_id, chat_id, kind in worker.notices():
            say(env, chat_id, worker.died_text(kind))
    except Exception as e:
        log(f"worker check failed: {type(e).__name__}: {e}")


def main():
    mask_streams()
    env, mtime, missing, con, offset = {}, False, None, None, 0
    while True:
        now_mtime = env_mtime()
        if now_mtime != mtime:  # .env changed, appeared or vanished: read it again, no restart needed (QA ops-03)
            mtime, env = now_mtime, read_env()
            print(f".env {'read' if now_mtime is not None else 'missing'}", flush=True)
            if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
                try:  # the command menu next to the input field; a failure here must not stop the listener
                    api(env, "setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
                except Exception as e:
                    log(f"setMyCommands failed: {describe_error(e)}")
        me = str(env.get("TELEGRAM_CHAT_ID") or "")
        gap = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k)]
        if gap:  # a wiped .env (happened on 08.10.2026) is visible in the journal once, not every 5 s
            if gap != missing:
                log(f"{' and '.join(gap)} missing in .env - not serving anyone; re-reading .env every {ENV_WAIT} s")
                missing = gap
            time.sleep(ENV_WAIT)
            continue
        if missing:
            print(".env complete again - serving", flush=True)
            missing = None
        if con is None:
            con = store.connect()
            offset = _load_offset(con)
        try:
            res = api(env, "getUpdates", offset=offset, timeout=50, allowed_updates=["callback_query", "message"])
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(env, con, u, me)
                except Exception as e:  # one bad update must not hold up the others
                    log(f"update {u.get('update_id')} failed: {describe_error(e)}")
                _save_offset(con, offset)
        except Exception as e:  # network blips must not kill the listener; systemd restarts it on a crash anyway
            log(f"bot error: {describe_error(e)}")
            if isinstance(e, KeyError) or (isinstance(e, urllib.error.HTTPError) and e.code in (401, 404)):
                mtime = False  # token missing or refused: read the whole .env again on the next turn (QA ops-03)
            time.sleep(backoff(e))
        report_dead_workers(env)


if __name__ == "__main__":
    main()
