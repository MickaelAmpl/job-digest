"""Send the morning digest to Telegram: a header, then 'strong' (score >= 60) and
'borderline' (50-59) vacancies, one message per vacancy with buttons, then one compact
list of what Claude rejected, then one line about the витрина (delivered cards still
unmarked) with a button to re-send them. Reads data/state.db: everything scored and not
yet delivered in the store.WINDOW_DAYS window, so a failed morning is delivered by the next run.
A job counts as delivered only when Telegram accepted its message.
On Sunday (Europe/Vienna) one more message: the week's summary (weekly_summary).
A broken morning is said in the header (health.py), never shown as a quiet day; a digest that does not
get through leaves an alert the next one shows. Two runs at once: the second exits (own lock, ops-11).
Usage: python3 digest.py [--dry-run] [--today=YYYY-MM-DD]   (--today: tests only, e.g. to fake a Sunday)"""

import fcntl
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
from datetime import date, datetime, timedelta
from pathlib import Path

import health
import store

ROOT = Path(__file__).parent
STRONG, BORDER = 60, 50
MAX_RETRY_AFTER = 60   # Telegram's 429 'retry_after': never sleep longer than this on one message (ops-12)
MAX_DEAD_SENDS = 3     # this many messages in a row lost to network errors: Telegram is unreachable, stop
EXIT_UNDELIVERED = 3   # the digest did not fully get through; it has written its own alert (run.sh knows)
MORE_BUTTON = [[{"text": "Показать ещё 5", "callback_data": "w:more"}]]

# IPv6 from the server to Telegram stalls now and then (a press hung 2+ min on 07.10.2026); use IPv4.
_orig_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = lambda host, *a, **kw: [r for r in _orig_getaddrinfo(host, *a, **kw) if r[0] == socket.AF_INET] \
    or _orig_getaddrinfo(host, *a, **kw)

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
HOURS = {"part-time": "part-time", "full-time": "полный день", "unknown": "часы не указаны"}
GERMAN = {"none": "немецкий не нужен", "plus": "немецкий будет плюсом", "ad_in_german": "объявление на немецком",
          "required": "нужен немецкий", "unknown": "про немецкий не сказано"}

# Verdicts agreed with Misha on 07.10.2026. Each has one meaning; the future CV-tailoring tool starts from "interested".
#   interested     - wants to apply
#   not_interested - could do it, does not want it (role, company, industry)
#   cant_fit       - wants it, but does not meet the requirements
#   bad_terms      - the conditions do not work (hours, city, pay, contract)
REASONS = {
    "cant_fit": [("skill", "нет навыка"), ("exp", "мало опыта"), ("german", "нужен немецкий"),
                 ("edu", "нужно профильное образование")],
    "bad_terms": [("fulltime", "полный день"), ("city", "не в Вене"), ("pay", "мало денег"),
                  ("temp", "временный контракт")],
}


LABELS = {"interested": "Интересно", "not_interested": "Не интересно", "cant_fit": "Не подхожу",
          "bad_terms": "Не подходит"}
# Application statuses (store.STATUSES), agreed with Misha 08.10.2026.
STATUS_LABELS = {"applied": "Отправил резюме", "replied": "Ответили", "interview": "Интервью", "offer": "Оффер",
                 "rejected": "Отказ", "no_answer": "Нет ответа"}
STALE_DAYS = 14  # Sunday summary: a CV sent this long ago with no answer is worth closing as 'Нет ответа'
# A manual application as the counts see it: added by hand (source 'manual', not removed), or a bot card Misha attached
# an application to (data.manual_applied), with a status. report.py marks exactly these rows '(вручную)'.
# json_valid keeps one broken data value from breaking /stats or the Sunday summary.
MANUAL_APPLIED = ("status is not null and ((source='manual' and coalesce(closed_by,'')!='manual_removed')"
                  " or (case when json_valid(data) then json_extract(data,'$.manual_applied') end)=1)")


def ru_date(iso):
    """'2026-10-08T07:30:00' -> '8 октября'; '' for an empty or broken value."""
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {MONTHS[d.month - 1]}"


def plural(n, one, few, many):
    """Russian plural form: plural(5, 'день', 'дня', 'дней') -> '5 дней'."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def weekly_summary(con, ref):
    """Sunday message: the last 7 days before `ref` (a datetime) and CVs left without an answer for STALE_DAYS."""
    since = (ref - timedelta(days=7)).isoformat(timespec="seconds")
    stale_before = (ref - timedelta(days=STALE_DAYS)).isoformat(timespec="seconds")
    one = lambda sql, *p: con.execute(sql, p).fetchone()[0]
    # Cards and marks are the bot's only (store.NOT_MANUAL); applications include the manual ones.
    cards = one(f"select count(*) from jobs where sent_at >= ? and coalesce(listed,0)=0 and {store.NOT_MANUAL}", since)
    marked = one(f"select count(*) from jobs where verdict is not null and verdict_at >= ? and {store.NOT_MANUAL}", since)
    interested = one(f"select count(*) from jobs where verdict='interested' and verdict_at >= ? and {store.NOT_MANUAL}",
                     since)
    applied = one("select count(*) from jobs where verdict='interested' and status is not null and applied_at >= ?", since)
    replied = one("select count(distinct l.job_id) from status_log l join jobs j on j.id=l.job_id"
                  " where j.verdict='interested' and j.status is not null and l.at >= ?"
                  " and l.status in ('replied','interview','offer')", since)
    # manual applications by the day they were sent (applied_at), not by when they were typed in
    manual = one(f"select count(*) from jobs where {MANUAL_APPLIED} and applied_at >= ?", since)
    stale = one("select count(*) from jobs where verdict='interested' and status='applied' and applied_at < ?", stale_before)
    start = (ref - timedelta(days=6)).date()
    lines = [f"<b>Итоги недели</b> ({start.day} {MONTHS[start.month - 1]} - {ref.day} {MONTHS[ref.month - 1]})",
             f"Прислано карточек: {cards}",
             f"Отмечено: {marked}, из них Интересно: {interested}",
             f"Отправил резюме: {applied}",
             f"Ответили: {replied}",
             f"Добавлено вручную: {manual}"]
    if stale:
        lines.append(f"\nОтправил резюме больше {STALE_DAYS} дней назад без ответа: {stale} - "
                     "отметь «Нет ответа» или «Ответили». Список: /applied")
    rep = liveness_report(con, ref)
    if rep:
        lines += [""] + cut_to(liveness_lines(rep), SUMMARY_MAX - len("\n".join(lines)) - 2)
    return "\n".join(lines)


LIVE_LINES = 10      # the summary lists at most this many closed and this many 'check yourself' vacancies
LIVE_BUTTONS = 10    # the 'Вернуть' message: one button per closed vacancy, at most this many
LIVE_LINK_MAX = 300  # a longer link is shown as plain text: it would eat the message limit
SUMMARY_MAX = 3800   # Telegram takes 4096 characters per message; the liveness lines are cut before that


def liveness_report(con, ref):
    """The weekly liveness check of 'Интересно' (liveness.py writes store.liveness_checks): the newest check of each
    vacancy in the 24 h before `ref`. None when nothing was checked. A database without the table has no checks."""
    since = (ref - timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        rows = con.execute("select c.job_id, c.result, c.reason, c.url, j.title, j.company, j.open_url, j.url as job_url,"
                           " j.closed_by from liveness_checks c left join jobs j on j.id=c.job_id"
                           " where c.checked_at >= ? order by c.id", (since,)).fetchall()
    except sqlite3.OperationalError:  # the table comes with store.connect(); no table = nothing was checked
        return None
    latest = {r["job_id"]: r for r in rows}  # a vacancy checked twice counts once, by its newest check
    if not latest:
        return None
    rep = {"checked": len(latest), "closed": [], "unknown": []}
    for r in latest.values():
        if r["result"] not in ("closed", "unknown"):  # open and skipped are counted, not listed
            continue
        link = next((u for u in (store.usable_url(r["url"]), store.usable_url(r["open_url"]),
                                 store.usable_url(r["job_url"])) if u), "")
        rep["closed" if r["result"] == "closed" else "unknown"].append({
            "job_id": r["job_id"], "title": (r["title"] or "")[:80], "company": (r["company"] or "")[:40],
            "reason": (r["reason"] or "")[:80], "link": link if len(link) <= LIVE_LINK_MAX else "",
            "closed_by": r["closed_by"]})
    return rep


def liveness_lines(rep):
    """The Sunday block: the counts, then the closed and the 'check yourself' vacancies, each with its link."""
    e = html.escape

    def line(label, it):
        title = e(it["title"] or "без названия")
        text = f'<a href="{e(it["link"], quote=True)}">{title}</a>' if it["link"] else title
        reason = f" ({e(it['reason'])})" if it["reason"] else ""
        return hyphens(f"{label}: {text} · {e(it['company'])}{reason}")

    lines = [f"Проверил «Интересно»: {rep['checked']}. Закрылись: {len(rep['closed'])}. "
             f"Не смог проверить: {len(rep['unknown'])}."]
    lines += [line("закрылась", it) for it in rep["closed"][:LIVE_LINES]]
    lines += [line("проверь сам", it) for it in rep["unknown"][:LIVE_LINES]]
    return lines


def cut_to(lines, room):
    """As many of `lines` as fit in `room` characters; the rest is named in one line, never dropped silently."""
    kept, used = [], 0
    for n, ln in enumerate(lines):
        if used + len(ln) + 1 > room:
            return kept + [f"…ещё {len(lines) - n} строк не поместились в сообщение"]
        kept.append(ln)
        used += len(ln) + 1
    return kept


def closed_message(con, ref):
    """The message after the Sunday summary: one 'Вернуть' button per vacancy the weekly check closed and that is still
    closed by it (a vacancy Misha reopened gets no button). None when the check closed nothing. The button sends
    o:<job id>, which bot.py answers by reopening the vacancy."""
    rep = liveness_report(con, ref)
    if not rep:
        return None
    buttons = []
    for it in rep["closed"]:
        if it["closed_by"] != "liveness":
            continue
        label = hyphens(f"Вернуть: {it['company']} · {it['title']}")
        buttons.append([{"text": label if len(label) <= 64 else label[:63] + "…",
                         "callback_data": f"o:{it['job_id']}"}])
        if len(buttons) == LIVE_BUTTONS:
            break
    if not buttons:
        return None
    text = "<b>Закрылись из «Интересно»</b>\nЕсли бот ошибся, нажми «Вернуть» - вакансия снова будет в «Интересно»."
    return text, buttons


def load_env():
    """KEY=value lines; surrounding quotes and a trailing ' # comment' are not part of the value."""
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            v = re.sub(r"\s+#.*$", "", v.strip()).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def redact(text, env):
    """Error text without the bot token: http.client puts the request path (with the token) in some
    exceptions, and logs get pasted into handoffs (ops-05)."""
    token = env.get("TELEGRAM_BOT_TOKEN") or ""
    text = str(text)
    if token:
        text = text.replace(token, "<token>").replace(token.split()[0] if token.split() else token, "<token>")
    return re.sub(r"bot\d+:[\w-]+", "bot<token>", text)


class Unreachable(Exception):
    """Telegram did not answer several messages in a row: stop sending, the rest goes next run."""


_dead = [0]


def send(env, text, buttons=None):
    """True only when Telegram accepted the message. One retry for network errors and 429/5xx.
    After MAX_DEAD_SENDS messages in a row that got no answer at all, raises Unreachable."""
    payload = {"chat_id": env["TELEGRAM_CHAT_ID"], "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": "true"}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    data = urllib.parse.urlencode(payload).encode()
    url = f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/sendMessage"
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
                ok = bool(json.load(r).get("ok"))
            _dead[0] = 0
            time.sleep(1.1)  # Telegram allows about one message per second in a private chat
            return ok
        except urllib.error.HTTPError as e:
            _dead[0] = 0
            body = e.read()[:300]
            print(f"telegram rejected a message (try {attempt}): HTTP {e.code} {redact(body, env)!r}", file=sys.stderr)
            if e.code == 429:
                try:
                    wait = int(json.loads(body).get("parameters", {}).get("retry_after", 5)) + 1
                except Exception:
                    wait = 6
                time.sleep(max(1, min(wait, MAX_RETRY_AFTER)))
            elif e.code < 500:
                return False  # a malformed message will not get better on retry
        except Exception as e:  # timeout, connection reset, DNS
            print(f"telegram send failed (try {attempt}): {type(e).__name__}: {redact(e, env)}", file=sys.stderr)
            if attempt == 2:
                _dead[0] += 1
                if _dead[0] >= MAX_DEAD_SENDS:
                    raise Unreachable(f"{_dead[0]} messages in a row without an answer")
        time.sleep(3)
    return False


def verdict_buttons(job_id):
    return [[{"text": "Интересно", "callback_data": f"v:interested:{job_id}"},
             {"text": "Не интересно", "callback_data": f"v:not_interested:{job_id}"}],
            [{"text": "Не подхожу", "callback_data": f"v:cant_fit:{job_id}"},
             {"text": "Не подходит", "callback_data": f"v:bad_terms:{job_id}"}]]


def reason_buttons(verdict, job_id):
    """Shown after 'cant_fit' or 'bad_terms'; optional, several can be picked."""
    rows = [[{"text": label, "callback_data": f"r:{verdict}:{key}:{job_id}"}] for key, label in REASONS[verdict]]
    return rows + [[{"text": "Готово", "callback_data": f"r:{verdict}:done:{job_id}"}]]


def hyphens(text):
    """Plain hyphens only in what Misha reads: feed and model text brings U+2013/U+2014 (botux-09 rule)."""
    return text.replace("\u2014", "-").replace("\u2013", "-")


def posted_line(posted, today=None):
    """'Опубликована 16 июня (115 дн. назад)' for a known posting date; the age only when it is a month+."""
    try:
        d = date.fromisoformat(str(posted)[:10])
    except (TypeError, ValueError):
        return ""
    age = ((today or date.today()) - d).days
    if age < 0 or age > 3650:
        return ""
    return f"Опубликована {d.day} {MONTHS[d.month - 1]}" + (f" ({age} дн. назад)" if age >= 30 else "")


GERMAN_FACT = {"none": "немецкий не нужен", "basic": "немецкий базовый", "native": "родной немецкий"}


def facts_line(facts):
    """One short Russian line from a paraphrase's facts, for a card whose search gave no summary:
    'Немецкий B2, часы: 20 ч, уровень: junior', or the first two tasks when none of those is stated."""
    if not isinstance(facts, dict):
        return ""
    parts = []
    german = str(facts.get("german") or "")
    if german and german != "unknown":
        parts.append(GERMAN_FACT.get(german, f"немецкий {german}"))
    if facts.get("hours") and facts["hours"] != "unknown":
        parts.append(f"часы: {str(facts['hours'])[:40]}")
    if facts.get("level") and facts["level"] != "unknown":
        parts.append(f"уровень: {str(facts['level'])[:20]}")
    if parts:
        line = ", ".join(parts)
        return line[:1].upper() + line[1:]
    tasks = [str(t)[:120] for t in (facts.get("tasks") or []) if isinstance(t, str)][:2]
    return ("Задачи: " + "; ".join(tasks)) if tasks else ""


def job_message(job, score, open_url=""):
    """Card text and buttons. Every field is capped, so a 5000-char hostile title can never push the card
    past Telegram's limit (security-13)."""
    e = html.escape
    location = (job.get("location") or "").split(" (")[0][:100]
    lines = [f"<b>{e((job.get('title') or '')[:200])}</b>",
             " · ".join(x for x in (e((job.get("company") or "")[:100]), e(location), HOURS.get(score.get("hours"), ""))
                        if x),
             f"Оценка {score['score']} · {GERMAN.get(score.get('german'), '')}",
             e((score.get("why_ru") or "")[:300])]
    when = posted_line(job.get("posted"))
    if when:
        lines.insert(2, when)  # realdata-12: an old posting is not 'new' just because we saw it today
    gaps = [str(g)[:80] for g in score.get("skill_gaps") or [] if g and "unknown" not in str(g).lower()]
    if gaps:
        lines.append("Не хватает: " + e(", ".join(gaps[:3])))
    if job.get("promoted") == 2:
        lines.insert(0, "<i>Раньше была только строкой в списке закрытых. Компания снова показывает вакансию - подходит.</i>")
    elif job.get("promoted") == 3:
        lines.insert(0, "<i>Раньше была в списке отсеянных. Пересчитал по новому правилу (часы больше не снижают оценку) - подходит.</i>")
    elif job.get("promoted"):
        lines.insert(0, "<i>Вчера была в списке отсеянных: описания не было. Сегодня нашёл оригинал - подходит.</i>")
    elif job.get("title_only") and job.get("enrich_error") == "auth":  # the Claude login expired: say why (stage 11)
        lines.append("<i>Описание не искал: Claude на сервере разлогинился - поищу после повторного входа.</i>")
    elif job.get("title_only") and job.get("searched") is False:
        lines.append("<i>Описание ещё не искал (не хватило времени утром), оценка по названию - поищу завтра.</i>")
    elif job.get("title_only"):
        lines.append("<i>Описание не нашёл, оценка только по названию - открой объявление.</i>")
    paraphrase = job.get("description_source") == "paraphrase"
    summary = job.get("summary_ru") or (facts_line(job.get("paraphrase")) if paraphrase else "")
    if summary:  # found by enrich.py on the open web: the posting itself, in short
        # a paraphrase is the facts the search read, not the posting text: say so (has_desc 2)
        head = "О вакансии (пересказ)" if paraphrase else "О вакансии"
        lines.append(f"\n<b>{head}</b>\n" + e(str(summary)[:900]))
    buttons = [[{"text": "Открыть объявление", "url": open_url}]] if open_url else []
    return hyphens("\n".join(lines)), buttons + verdict_buttons(job["id"])


CLOSED_HEAD = "<b>Закрыты и дубли</b>"


def rejected_chunks(items, closed=()):
    """Compact list of what Claude scored below 50, so Misha can catch a wrong rejection, then the jobs
    closed since they were found (removed by the company, closed per the original, or a duplicate of
    another job - so no job disappears without a line). Returns [(text, [job ids in it])], each under
    Telegram's 4096-character limit."""
    e = html.escape
    out, ids = [], []
    chunk = ("<b>Отсеял Claude</b>\nЕсли что-то здесь зря, напиши об этом в этот чат - я прочитаю и поправлю оценку."
             if items else "")
    for n, (job, score, url) in enumerate([*items, *closed]):
        title = e((job.get("title") or "")[:80])
        if url and len(url) <= 500:
            title = f'<a href="{e(url, quote=True)}">{title}</a>'
        if job.get("closed"):
            if job.get("closed_by") == "duplicate":
                why = "Дубль: та же вакансия, что " + (f"«{job['dup_title'][:60]}»" if job.get("dup_title") else "другая в подборке")
            elif job.get("closed_by") == "feed":
                why = "Снята: компания убрала вакансию со своего сайта"
            else:
                why = "Закрыта: оригинал объявления пишет, что вакансия больше не принимает заявки"
        else:
            why = score.get("why_ru") or ""
        if job.get("title_only") and not job.get("closed"):
            why = "Описания нет, оценка по названию - поищу завтра ещё раз. " + why
        line = (f"{score['score']} · {title} · {e((job.get('company') or '')[:40])}\n"
                f"<i>{e(why[:140])}</i>")
        if n == len(items):  # first closed one: its own heading
            line = CLOSED_HEAD + "\n\n" + line
        line = hyphens(line)
        if len(chunk) + len(line) + 2 > 3800:
            out.append((chunk, ids))
            chunk, ids = "", []
        chunk += "\n\n" + line
        ids.append(job["id"])
    if ids:
        out.append((chunk.strip(), ids))
    if not out and closed:
        out.append((chunk.strip(), []))
    return out


def card_item(r):
    """(job, score, link) for one stored row, or None if its score is unusable. Shared with bot.py (витрина)."""
    score = store.score_of(r)  # the same filter the витрина counts use
    if not score:
        return None
    try:
        job = json.loads(r["data"] or "{}")
    except ValueError:
        return None
    job["id"] = r["id"]
    if r["summary"]:
        job["summary_ru"] = r["summary"]
    job["closed"] = bool(r["closed"])
    job["closed_by"] = r["closed_by"]
    job["promoted"] = r["promote"] or 0  # 1 = description found later, 2 = reopened after a 'closed' line,
    # 3 = rescored under the hours rule of 26.10.2026 (one-off)
    job["title_only"] = not r["has_desc"]  # has_desc 2 (paraphrase) is not title only
    if r["has_desc"] == 2:
        job["description_source"] = "paraphrase"
    keys = r.keys()
    if "enrich_tries" in keys:
        job["searched"] = bool(r["enrich_tries"])
    if "dup_of" in keys:
        job["dup_of"] = r["dup_of"]
    return job, score, store.usable_url(r["open_url"]) or store.usable_url(r["url"])


def pending(con):
    """Scored, undelivered jobs in the window (closed ones too: they go to the closed list once)."""
    cond, params = store.in_window(closed_ok=True)
    rows = con.execute(f"select * from jobs where scored_at is not null and {cond}"
                       " and (sent_at is null or (promote>=1 and score >= ?)) order by score desc",
                       params + [BORDER]).fetchall()
    return [x for x in map(card_item, rows) if x]


def vacancies(n):
    """Russian plural: 1 вакансия, 2 вакансии, 5 вакансий."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} вакансия"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} вакансии"
    return f"{n} вакансий"


def health_block(fails, notes):
    """The header's 'what went wrong' part: empty on a normal morning."""
    out = ""
    if fails:
        out += "\n\n<b>Сбои сбора</b> - поэтому вакансий может быть меньше, чем на рынке:\n" + \
               "\n".join("- " + html.escape(x) for x in fails[:12])
    if notes:
        out += "\n\n" + "\n".join(html.escape(x) for x in notes[:6])
    return out


def plain(text):
    """An HTML message as safe HTML without tags (the last-resort retry: cutting HTML leaves tags open)."""
    return html.escape(html.unescape(re.sub(r"<[^<>]*>", "", text)))[:3500]


def lock():
    """Own lock: a manual run while cron's digest is sending exits instead of sending everything twice."""
    store.DB.parent.mkdir(exist_ok=True)
    f = open(store.DB.parent / "digest.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main():
    dry = "--dry-run" in sys.argv
    env = {} if dry else load_env()  # a dry run sends nothing and needs no secrets
    if not dry and (not env.get("TELEGRAM_CHAT_ID") or not env.get("TELEGRAM_BOT_TOKEN")):
        health.alert("в .env пусто TELEGRAM_CHAT_ID или TELEGRAM_BOT_TOKEN - подборка не отправлена")
        sys.exit("TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN is empty in .env - nothing can be sent")
    held = None if dry else lock()
    if not dry and not held:
        print("another digest.py is sending right now - exiting, nothing sent twice")
        return 0
    try:
        return run(env, dry)
    finally:
        if held:
            held.close()


def run(env, dry):
    con = store.connect()
    items = pending(con)
    closed = [x for x in items if x[0].get("closed")]
    for job, _, _ in closed:
        if job.get("closed_by") == "duplicate" and job.get("dup_of"):
            row = con.execute("select title from jobs where id=?", (job["dup_of"],)).fetchone()
            job["dup_title"] = row["title"] if row else ""
    items = [x for x in items if not x[0].get("closed")]
    strong = [x for x in items if x[1]["score"] >= STRONG]
    border = [x for x in items if BORDER <= x[1]["score"] < STRONG]
    rejected = [x for x in items if x[1]["score"] < BORDER]
    unscored = sum(1 for x in items if x[1].get("unscored"))
    seen_today = None
    if store.fresh_out("prefiltered.json"):  # yesterday's count is not today's (ops-01)
        try:
            seen_today = len(json.loads((store.OUT / "prefiltered.json").read_text(encoding="utf-8")))
        except Exception:
            seen_today = None
    alerts = health.pending_alerts()
    fails, notes = health.problems()
    for line in fails + notes:
        print("HEALTH:", line)

    today = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--today=")), "")
    d = date.fromisoformat(today) if today else date.today()
    header = (f"<b>Подборка на {d.day} {MONTHS[d.month - 1]}</b>\n"
              f"Новых вакансий после фильтра по правилам: {len(items)}\n"
              f"Подходят: {len(strong)} сильных, {len(border)} пограничных\n"
              f"Отсеял Claude: {len(rejected)}" + (" (список в конце)" if rejected or closed else "") + "\n"
              + (f"Закрыты или дубли: {len(closed)} (в том же списке)\n" if closed else "") +
              ("Всего просмотрено в источниках: " + (str(seen_today) if seen_today is not None
                                                     else "нет данных за сегодня")))
    if items and unscored == len(items):
        header += ("\n\nСегодня Claude не смог оценить вакансии (скорее всего, слетел вход на сервере): "
                   "всё, что прошло фильтр по правилам, в пограничных. Переоценю завтра.")
    elif unscored:
        header += f"\n\nБез оценки Claude: {unscored} - они в пограничных, переоценю завтра."
    if not items:
        header += ("\n\nНовых вакансий сегодня нет." if not fails else
                   "\n\nНовых вакансий нет - но см. сбои ниже, это не обязательно тихий день.")
    header += health_block(fails, notes)

    plan = [(hyphens(header), None, [])]  # (text, buttons, job ids delivered by this message)
    texts = {}
    if strong:
        plan.append(("<b>Сильные</b>", None, []))
    for title, group in (("", strong), ("<b>Пограничные</b>\nТут стоит глянуть быстро: подходит частично.", border)):
        if group and title:
            plan.append((title, None, []))
        for job, score, url in group:
            text, buttons = job_message(job, score, url)
            texts[job["id"]] = text
            plan.append((text, buttons, [job["id"]]))
    plan += [(t, None, ids) for t, ids in rejected_chunks(rejected, closed)]

    delivered, header_ok, unreachable = set(), dry, False
    for n, (text, buttons, ids) in enumerate(plan):
        if dry:
            print(text, "\n", [b["text"] for row in (buttons or []) for b in row], "\n---")
            continue
        try:
            ok = send(env, text, buttons)
            if not ok and buttons and buttons[0][0].get("url"):
                ok = send(env, text, buttons[1:])  # Telegram rejected the link button (e.g. a malformed URL)
            if not ok and len(ids) == 1:
                ok = send(env, plain(text), buttons and [r for r in buttons if not r[0].get("url")])
        except Unreachable as e:
            print(f"telegram unreachable, stopping: {e}", file=sys.stderr)
            unreachable = True
            break
        if n == 0 and ok:
            header_ok = True
            health.clear_alerts(alerts)  # Misha has now seen them
        if ok:
            for jid in ids:  # a job is delivered only once Telegram accepted its message
                con.execute("update jobs set sent_at=?, msg=coalesce(?, msg), listed=?, promote=0 where id=?",
                            (store.now(), texts.get(jid), 0 if jid in texts else 1, jid))
                delivered.add(jid)
            con.commit()

    # Cards sent on earlier mornings and still unmarked: one line + a button, the bot re-sends them 5 at a time.
    # Jobs planned in this run are not counted: Misha has just got them.
    planned = {jid for _, _, ids in plan for jid in ids}
    left = sum(1 for r in store.vitrina(con) if r["id"] not in planned)
    weekly = 0
    if not unreachable:
        if left:
            text = f"В витрине ещё {vacancies(left)} без отметки за {store.WINDOW_DAYS} дней"
            if dry:
                print(text, "\n", [b["text"] for row in MORE_BUTTON for b in row], "\n---")
            else:
                try:
                    send(env, text, MORE_BUTTON)
                except Unreachable:
                    unreachable = True
        # Sunday: the week's summary, once per Sunday (a second run that day does not repeat it).
        sent_for = (con.execute("select v from meta where k='weekly_sent'").fetchone() or [""])[0]
        if not unreachable and d.weekday() == 6 and sent_for != d.isoformat():
            ref = datetime.combine(d, datetime.now().time())
            text = weekly_summary(con, ref)
            # 'Вернуть' message for what the weekly liveness check closed, or None. Not named `closed`: that is the
            # digest's list of closed vacancies, counted in `total` below.
            closed_msg = closed_message(con, ref)
            weekly = 1 + (closed_msg is not None)
            if dry:
                print(text, "\n---")
                if closed_msg:
                    print(closed_msg[0], [b["text"] for row in closed_msg[1] for b in row], "\n---")
            else:
                try:
                    if send(env, text):
                        con.execute("insert or replace into meta (k, v) values ('weekly_sent', ?)", (d.isoformat(),))
                        con.commit()  # the guard is set before the second message: a failed one never repeats the summary
                        if closed_msg:
                            send(env, closed_msg[0], closed_msg[1])
                except Unreachable:
                    unreachable = True
    total = len(items) + len(closed)
    print(f"messages planned: {len(plan) + (1 if left else 0) + weekly}, jobs delivered: {len(delivered)} of {total},"
          f" in the витрина: {left}")
    if dry:
        return 0
    if not header_ok or len(delivered) < total:
        # Never silence (ops-02): the next digest that gets through says so; run.sh sees a non-zero exit.
        health.alert(f"подборка за {d.day} {MONTHS[d.month - 1]} дошла не полностью: доставлено {len(delivered)} "
                     f"из {total} вакансий" + ("" if header_ok else ", заголовок не прошёл")
                     + (" (Telegram не отвечал)" if unreachable else "") + " - остальные придут со следующей подборкой")
        return EXIT_UNDELIVERED
    return 0


if __name__ == "__main__":
    sys.exit(main())
