"""Morning health: what went wrong with intake, so a broken morning never reads like a quiet market day
(pipeline-06, ops-01). Each stage records its outcome in out/health.json (one file per morning, reset by
run.sh collect); things that must reach Misha even if today's digest itself fails (a skipped or
undelivered digest, a crashed send) go to data/alerts.json and are shown by the next digest that gets
through. digest.py turns both into a short block in its header.
A failed state.db backup is its own alert kind ('backup'): the digest did go out, so it gets a separate note
line, never the 'Сбои сбора' list (pipeline2-04, ops2-07).
CLI (run.sh): python3 health.py start | finish | step MODE RC NAME... | alert TEXT | backup-failed RC"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import store

ROOT = Path(__file__).parent
HEALTH = "health.json"
ALERT_LINES = 12   # digest.py prints at most this many 'Сбои' lines (fails[:12]); the rest waits in alerts.json
NOTE_LINES = 6     # ... and at most this many note lines (notes[:6])
MAX_ALERTS = 100   # the stored queue; only alerts beyond this many unshown ones are dropped
BACKUP_TEXT = "Резервная копия базы не сделана (подборка ушла нормально)."
HISTORY = "health_history.json"  # one entry per past morning: the numbers the quiet-failure signals compare
HISTORY_DAYS = 7
# Quiet-failure signals (audit 16.10: enrich found 3 of 86 for a week and every step still said 'ok').
ENRICH_MIN_TODO = 5      # fewer jobs than this: too few to judge the search by
ENRICH_MIN_RATE = 0.30   # found / todo below this on two mornings in a row
ENRICH_MORNINGS = 2
KEPT_MORNINGS = 2        # new jobs that pass the rule: zero on this many mornings in a row
FEED_MORNINGS = 3        # one company feed failing this many mornings in a row
MAIL_SOURCES = (("linkedin", "LinkedIn"), ("stepstone", "StepStone"))
PAGES_MIN_PARAPHRASE = 3  # pages found nothing; paraphrases this many show the descriptions are only in the search text
PUSH_EVERY = 3          # a persistent signal goes to Telegram on day 1 of its streak, then every 3rd day


def _path():
    return store.OUT / HEALTH


def _history_path():
    return store.OUT / HISTORY


def _alerts_path():
    return store.DB.parent / "alerts.json"


def _load(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store.scrub(data), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load():
    """Today's health record, or {} if this morning's collect run never started."""
    data = _load(_path(), {})
    return data if data.get("day") == date.today().isoformat() else {}


def start():
    _save(_path(), {"day": date.today().isoformat(), "started": store.now(), "steps_failed": []})


def finish():
    data = load() or {"day": date.today().isoformat()}
    data["finished"] = store.now()
    _save(_path(), data)
    try:
        _remember_today()
        _push_signals()
    except Exception as e:  # health is a report: a broken signal never fails the morning
        print(f"health signals failed: {type(e).__name__}", file=sys.stderr)


def record(stage, **info):
    """One stage's outcome for today's digest (collect, enrich, score). Never raises: health is a report."""
    try:
        data = load() or {"day": date.today().isoformat()}
        data[stage] = dict(info, at=store.now())
        _save(_path(), data)
    except Exception as e:
        print(f"health record failed: {type(e).__name__}", file=sys.stderr)


def alert(text, kind=""):
    """Something the next digest must say even if it is days later. kind 'backup' = a note line, not a failure."""
    try:
        alerts = _load(_alerts_path(), [])
        entry = {"at": store.now(), "text": str(text)[:300]}
        if kind:
            entry["kind"] = kind
        alerts.append(entry)
        _save(_alerts_path(), alerts[-MAX_ALERTS:])
        print(f"ALERT: {text}", file=sys.stderr)
    except Exception as e:
        print(f"alert not saved: {type(e).__name__}: {text}", file=sys.stderr)


def backup_failed(rc):
    """run.sh: scripts/backup_db.py exited non-zero. Not a failed digest: that one went out."""
    print(f"backup exit {rc}", file=sys.stderr)
    alert(BACKUP_TEXT, kind="backup")


def _window(head, tail, limit):
    """At most `limit` lines: today's `head` lines first, then the pending `tail` pairs (alert, line), oldest
    first. When something does not fit, the last line is 'и ещё N' and the cut alerts stay queued."""
    if len(head) + len(tail) <= limit:
        return head + [line for _, line in tail], tail
    lines = head[:limit - 1]
    shown = tail[:max(0, limit - 1 - len(lines))]
    hidden = len(head) + len(tail) - len(lines) - len(shown)
    return lines + [line for _, line in shown] + [f"и ещё {hidden}"], shown


def _shown():
    """(fails, notes, alerts): what the next digest prints, and the stored alerts those lines carry.
    The digest clears exactly these alerts once its header is delivered; everything else stays queued."""
    fails, notes = _today_problems()
    fail_tail, note_tail = [], []
    for a in _load(_alerts_path(), []):
        if a.get("kind") == "backup":
            note_tail.append((a, a.get("text")))
        else:
            when = str(a.get("at", ""))[:16].replace("T", " ")
            fail_tail.append((a, f"Раньше ({when}): {a.get('text')}"))
    fail_lines, fail_shown = _window(fails, fail_tail, ALERT_LINES)
    note_lines, note_shown = _window(notes, note_tail, NOTE_LINES)
    return fail_lines, note_lines, [a for a, _ in fail_shown + note_shown]


def pending_alerts():
    """The alerts the next digest will print (see _shown). Not the whole queue."""
    return _shown()[2]


def clear_alerts(shown):
    """Drop only the alerts a delivered digest has shown; everything else stays queued for the next one."""
    keep = _load(_alerts_path(), [])
    for a in shown:  # one stored entry per shown alert: two identical alerts in the same second are two alerts
        if a in keep:
            keep.remove(a)
    try:
        _save(_alerts_path(), keep)
    except OSError:
        pass


def step_failed(mode, rc, name):
    """run.sh: a step exited non-zero. In the morning collect it is part of today's health; in send it is
    an alert (the digest it would report in did not go out). rc 3 = digest already wrote its own alert."""
    if mode == "send" and "backup_db.py" in name:
        backup_failed(rc)
        return
    if mode == "send":
        if str(rc) != "3":
            alert(f"{datetime.now():%d.%m %H:%M}: отправка подборки упала ({name}, код {rc}) - "
                  "неотправленные вакансии уйдут со следующей подборкой")
        return
    data = load() or {"day": date.today().isoformat()}
    data.setdefault("steps_failed", []).append({"step": name, "rc": str(rc), "at": store.now()})
    _save(_path(), data)


def _company_names():
    try:
        return {c["id"]: c["name"] for c in json.load(open(ROOT / "companies.json", encoding="utf-8"))}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def problems(today=None):
    """(failures, notes): Russian lines for the digest header, within the header's line limits (see _shown).
    failures = intake was degraded today; notes = worth knowing, not a failure. Both empty on a normal morning."""
    try:
        fails, notes, _ = _shown()
    except Exception as e:  # digest.py calls this unwrapped: a broken health file must not stop the digest
        print(f"health problems failed: {type(e).__name__}", file=sys.stderr)
        return [], []
    return fails, notes


def _today_problems():
    """Today's failures and notes from the run record, without the queued alerts."""
    fails, notes = [], []
    h = load()
    if not h:
        fails.append("Утренний сбор сегодня не запускался (сервер, cron или блокировка) - новых вакансий за сегодня нет.")
    else:
        for s in h.get("steps_failed") or []:
            fails.append(f"Шаг {s.get('step')} упал (код {s.get('rc')}) - часть утренней обработки не прошла.")
        if not h.get("finished") and not h.get("steps_failed"):
            fails.append("Утренний сбор ещё идёт или оборвался - что не успел, придёт завтра.")
        c = h.get("collect")
        if c is None and h.get("started"):
            fails.append("Почта и Adzuna сегодня не собраны (collect.py не дошёл до конца).")
        elif c:
            if c.get("email_error"):
                fails.append(f"Почта: не прочитал ({c['email_error']}) - письма LinkedIn, StepStone, derStandard "
                             "не собраны; дочитаю пропущенные дни, когда заработает.")
            if c.get("bad_mails"):
                fails.append(f"Почта: {c['bad_mails']} писем не разобрал (битые), остальные прочитал.")
            if c.get("spoofed"):
                fails.append(f"Почта: {c['spoofed']} писем отброшено - отправитель не прошёл проверку подлинности.")
            if c.get("adzuna_error") or (c.get("adzuna_calls") and c.get("adzuna_failed") == c.get("adzuna_calls")):
                fails.append("Adzuna: не ответила - вакансий оттуда сегодня нет.")
            elif c.get("adzuna_failed"):
                fails.append(f"Adzuna: {c['adzuna_failed']} из {c['adzuna_calls']} запросов без ответа.")
            # no_list (mails from jobs.at, karriere.at without a vacancy list) is not shown: a known source limit.
            # It stays in the collect record and in collect.py's log line.
            if max(c.get("email_days") or 0, c.get("adzuna_days") or 0) > 2:
                notes.append(f"Дочитал пропущенные дни: почта за {c.get('email_days')}, Adzuna за {c.get('adzuna_days')} дн.")
        feeds = _load(store.OUT / "company_feeds.json", {})
        names = _company_names()
        if not store.fresh_out("company_feeds.json") or "totals" not in feeds:
            fails.append("Сайты компаний: сегодня не прочитаны (fetch_companies.py не отработал), закрытия не считаю.")
        else:
            failed = feeds.get("failed") or {}
            if failed:
                listed = ", ".join(sorted(str(v).split(":")[0] for v in failed.values()))
                fails.append(f"Сайты компаний: {len(failed)} из {feeds.get('asked', '?')} не ответили: {listed}.")
            capped = [cid for cid in feeds.get("capped") or [] if cid not in (feeds.get("known_capped") or [])]
            if capped:
                fails.append("Сайты компаний: прочитал только часть вакансий (закрытия по ним не считаю): "
                             + ", ".join(names.get(cid, cid) for cid in capped) + ".")
            # A stray bad record is normal on a big board (Accenture, BearingPoint): the note needs 2 bad records and
            # at least 10 % of the feed (good plus bad records), or it repeats every morning for nothing.
            totals = feeds.get("totals") or {}
            noisy = {k: v for k, v in (feeds.get("bad_records") or {}).items()
                     if _num(v) is not None and v >= 2 and v * 10 >= (_num(totals.get(k)) or 0) + v}
            if noisy:
                notes.append("Пропущены битые записи в лентах: "
                             + ", ".join(f"{names.get(k, k)} {v}" for k, v in sorted(noisy.items())) + ".")
        e = h.get("enrich") or {}
        if e.get("failed"):
            fails.append(f"Поиск оригиналов: {e['failed']} ошибок Claude (вход или лимит?) - попытки не засчитаны, повторю завтра.")
        if e.get("skipped"):
            notes.append(f"Не успел поискать оригиналы для {e['skipped']} вакансий - поищу завтра.")
        s = h.get("score") or {}
        if s.get("failed_chunks"):
            fails.append(f"Оценка Claude: {s.get('fallback', 0)} из {s.get('jobs', 0)} без оценки (вход или лимит?) - "
                         "переоценю завтра.")
        try:
            fails.extend(text for _, text in _signals(_today(), _history()))
        except Exception as e:
            print(f"health signals failed: {type(e).__name__}", file=sys.stderr)
    return fails, notes


def _num(v):
    """A count from a record or the history. Bools and strings are not counts (type-safe history)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clean(entry):
    """A morning's entry with the fields the signals read, or None if any field has the wrong type: a bad
    entry is dropped, never read as zero. Optional fields that are absent come back as None."""
    if not isinstance(entry, dict) or not isinstance(entry.get("day"), str):
        return None
    try:
        date.fromisoformat(entry["day"])
    except ValueError:
        return None
    e = entry.get("enrich")
    if e is not None:
        if not isinstance(e, dict):
            return None
        e = {k: _num(e.get(k)) for k in ("todo", "found", "failed", "found_page", "found_paraphrase")}
        if e["todo"] is None or e["found"] is None:
            return None
    kept = entry.get("new_kept")
    if kept is not None and _num(kept) is None:
        return None
    mails = entry.get("mails")
    if mails is not None and (not isinstance(mails, dict) or any(_num(v) is None for v in mails.values())):
        return None
    failed = entry.get("feeds_failed")
    if failed is not None and (not isinstance(failed, list) or not all(isinstance(x, str) for x in failed)):
        return None
    for name in ("signals", "pushed", "push_failed"):
        v = entry.get(name) or []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return None
    return {"day": entry["day"], "enrich": e, "new_kept": kept, "mails": mails,
            "email_error": bool(entry.get("email_error")), "feeds_failed": failed,
            "signals": entry.get("signals") or [], "pushed": entry.get("pushed") or [],
            "push_failed": entry.get("push_failed") or []}


def _snapshot():
    """This morning's numbers from the run record. A metric the run did not record is None, so the signal
    stays silent instead of reading a missing number as zero."""
    h = load()
    if not h:
        return None
    feeds = _load(store.OUT / "company_feeds.json", {})
    failed = feeds.get("failed") if isinstance(feeds, dict) else None
    feeds_failed = (sorted(failed) if isinstance(failed, dict) and store.fresh_out("company_feeds.json")
                    and "totals" in feeds else None)
    e, p, c = h.get("enrich"), h.get("prepare"), h.get("collect")
    return _clean({"day": h.get("day") or date.today().isoformat(),
                   "enrich": e if isinstance(e, dict) else None,
                   "new_kept": p.get("new_kept") if isinstance(p, dict) else None,
                   "mails": c.get("mails_by_source") if isinstance(c, dict) else None,
                   "email_error": bool(c.get("email_error")) if isinstance(c, dict) else False,
                   "feeds_failed": feeds_failed})


def _history():
    """Past mornings, oldest first, one per day. Entries that fail _clean are dropped."""
    by_day = {}
    for h in _load(store.OUT / HISTORY, []):
        clean = _clean(h)
        if clean:
            by_day[clean["day"]] = clean
    return [by_day[d] for d in sorted(by_day)]


def _today():
    """This morning as the signals see it: the live run, merged with what finish() already stored for today.
    A manual rerun on the same day records a smaller run; per stage the richer record wins (more enrich
    jobs, more new kept, more mail), so a rerun never erases what the morning found."""
    live = _snapshot()
    if live is None:
        return None
    old = next((h for h in _history() if h["day"] == live["day"]), None)
    if old is None:
        return live
    e = max((live, old), key=lambda s: s["enrich"]["todo"] if s["enrich"] else -1)
    k = max((live, old), key=lambda s: s["new_kept"] if s["new_kept"] is not None else -1)
    m = max((live, old), key=lambda s: sum(s["mails"].values()) if s["mails"] is not None else -1)
    return dict(live, enrich=e["enrich"], new_kept=k["new_kept"], mails=m["mails"], email_error=m["email_error"],
                feeds_failed=live["feeds_failed"] if live["feeds_failed"] is not None else old["feeds_failed"])


def _run_of(n, today, hist):
    """The last n mornings ending with today, oldest first - or None if one of the earlier ones is missing
    (a morning without a collect run breaks the run: no comparison across the gap)."""
    by_day = {h["day"]: h for h in hist}
    day, run = date.fromisoformat(today["day"]), [today]
    for k in range(1, n):
        prev = by_day.get((day - timedelta(days=k)).isoformat())
        if prev is None:
            return None
        run.insert(0, prev)
    return run


def _enrich_counts(m):
    """(found, asked, page, paraphrase) for one morning, or None. Found is the page and paraphrase finds when
    enrich recorded them, else the plain found. Asked leaves out Claude failures (the failures block reports them)."""
    e = m.get("enrich")
    if not e:
        return None
    asked = e["todo"] - (e["failed"] or 0)
    page, para = e["found_page"], e["found_paraphrase"]
    found = (page or 0) + (para or 0) if page is not None or para is not None else e["found"]
    return found, asked, page, para


def _enrich_low(m):
    c = _enrich_counts(m)
    return bool(c) and c[1] >= ENRICH_MIN_TODO and c[0] / c[1] < ENRICH_MIN_RATE


def _pages_broken(m):
    """Pages are not downloaded while paraphrases still come back: no text from a company page at all."""
    c = _enrich_counts(m)
    return bool(c) and c[1] >= ENRICH_MIN_TODO and c[2] == 0 and c[3] is not None and c[3] >= PAGES_MIN_PARAPHRASE


def _signals(today, hist):
    """[(key, Russian line)] for the quiet failures that hold this morning. Each compares with earlier
    mornings, so none fires without history: the first morning is never an alarm."""
    if not today:
        return []
    past = [h for h in hist if h["day"] < today["day"]]
    if not past:
        return []
    out = []
    run = _run_of(ENRICH_MORNINGS, today, past)
    if run and all(_enrich_low(m) for m in run):
        a, b = _enrich_counts(run[0]), _enrich_counts(run[-1])
        out.append(("enrich", f"Поиск описаний нашёл мало: {a[0]} из {a[1]} и {b[0]} из {b[1]} "
                              "за два утра - проверь enrich"))
    if run and all(_pages_broken(m) for m in run):
        out.append(("enrich_pages", "Скачивание страниц не работает: за два утра ни одного текста со страницы "
                                    f"компании, пересказов {_enrich_counts(run[0])[3]} и {_enrich_counts(run[-1])[3]}"
                                    " - проверь enrich"))
    run = _run_of(KEPT_MORNINGS, today, past)
    if run and all(m.get("new_kept") == 0 for m in run):
        out.append(("kept", "Два утра подряд нет новых вакансий после фильтра"))
    if today.get("mails") is not None and not today.get("email_error"):  # mailbox failure has its own line
        for src, name in MAIL_SOURCES:
            if int(today["mails"].get(src) or 0) == 0:
                out.append((f"mail:{src}", f"Нет писем {name} за 2 дня"))
    run = _run_of(FEED_MORNINGS, today, past)
    if run and all(m.get("feeds_failed") is not None for m in run):
        stuck = set.intersection(*(set(m["feeds_failed"]) for m in run))
        names = _company_names()
        out.extend((f"feed:{cid}", f"Лента {names.get(cid, cid)} не отвечает 3 дня подряд") for cid in sorted(stuck))
    return out


def _streak(key, today, hist):
    """Mornings in a row, up to and including today, on which this signal held."""
    by_day = {h["day"]: h for h in hist if h["day"] < today["day"]}
    day, n, k = date.fromisoformat(today["day"]), 1, 1
    while True:
        h = by_day.get((day - timedelta(days=k)).isoformat())
        if h is None or key not in h["signals"]:
            return n
        n, k = n + 1, k + 1


def _remember_today():
    """finish(): this morning goes into the history with the signals that held, keeping the last HISTORY_DAYS
    mornings. The keys pushed today stay, so a second collect run the same day does not push them again."""
    today = _today()
    if not today:
        return
    hist = _history()
    old = next((h for h in hist if h["day"] == today["day"]), None)
    entry = dict(today, signals=[key for key, _ in _signals(today, hist)],
                 pushed=old["pushed"] if old else [], push_failed=old["push_failed"] if old else [])
    hist = [h for h in hist if h["day"] != today["day"]] + [entry]
    _save(_history_path(), sorted(hist, key=lambda h: h["day"])[-HISTORY_DAYS:])


def _push_signals():
    """finish(): a signal goes to Telegram on the first morning of its streak and then every PUSH_EVERY-th
    morning of that streak (days 1, 4, 7 ...), at most once a day, through the sender run.sh uses
    (scripts/alert.py). A push that failed is retried on the next morning it still holds, without moving the
    cadence. The digest block shows the signal on every morning it holds."""
    today = _today()
    hist = _history()
    entry = next((h for h in hist if today and h["day"] == today["day"]), None)
    if not entry:
        return
    day = date.fromisoformat(today["day"])
    yesterday = next((h for h in hist if h["day"] == (day - timedelta(days=1)).isoformat()), None)
    for key, text in _signals(today, hist):
        if key in entry["pushed"]:
            continue
        scheduled = (_streak(key, today, hist) - 1) % PUSH_EVERY == 0
        retry = key in entry["push_failed"] or bool(yesterday and key in yesterday["push_failed"])
        if not (scheduled or retry):
            continue
        if _send(text):
            entry["pushed"].append(key)
            entry["push_failed"] = [k for k in entry["push_failed"] if k != key]
        elif key not in entry["push_failed"]:
            entry["push_failed"].append(key)
    _save(_history_path(), hist[-HISTORY_DAYS:])


def _send(text):
    try:
        from scripts import alert as telegram  # lazy: importing it switches lookups to IPv4, as digest.py does
        return bool(telegram.send(text))
    except Exception as e:
        print(f"signal not pushed: {type(e).__name__}", file=sys.stderr)
        return False



def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "start":
        start()
    elif cmd == "finish":
        finish()
    elif cmd == "step" and len(argv) >= 5:
        step_failed(argv[2], argv[3], " ".join(argv[4:]))
    elif cmd == "alert" and len(argv) >= 3:
        alert(" ".join(argv[2:]))
    elif cmd == "backup-failed" and len(argv) >= 3:
        backup_failed(argv[2])
    elif cmd == "show":
        fails, notes = problems()
        print("\n".join(fails + notes) or "ok")
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
