"""The 'Адаптировать резюме' worker (stage 5, Task 7). The bot starts it as a detached process, so the polling
loop never waits 1-3 minutes for Claude and Chrome; the worker runs run.tailor_job and sends the result itself:
the PDF and one summary message, the base CV with the reason on a failed tailoring, a request for the ad text
when the job has none. Whatever happens, Misha gets a message - never silence.

One run per job at a time: lock file data/cv/<job_id>/.lock, taken by the bot before it spawns the worker
(so a double press is answered at once) and released here. The lock records who holds it (JSON: token, pid,
state, started) and the running worker touches it every HEARTBEAT seconds, so a press after a killed, OOM'd
or never-started worker takes the lock over at once instead of answering 'Уже готовлю' for 12 minutes
(QA 09.10 ops-04/ops-08/cvtailor-08). The bot also polls its children: a worker that died without releasing
its lock gets a message to Misha.
This module imports only stdlib + store at the top: the bot imports it, and a broken tailoring module must
not stop the bot from starting.

CLI (what the bot runs): python3 -m cv.tailor.worker <job_id> <chat_id> [--text path] [--lock token] [--kind cv|letter]
--kind letter writes the cover letter (stage 6) instead of the CV; the lock, heartbeat and release are the same.
"""

import argparse
import errno
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import store  # noqa: E402

RUN_DEADLINE = 600            # = run.RUN_DEADLINE (a test keeps them equal; not imported, the bot stays light)
LOCK_STALE = RUN_DEADLINE + 120  # hard cap from the lock's start: a live run may still build the fallback PDF
PENDING_STALE = 60            # taken by the bot, but no worker ever started (the spawn or the press answer failed)
HEARTBEAT = 20                # the running worker touches its lock this often
HEARTBEAT_STALE = 120         # no touch for this long: the worker hangs or is gone
MIN_TEXT = 200                # = run.MIN_DESC: a shorter pasted ad is asked for again before any worker starts
SERVER_CHROME = "/usr/bin/google-chrome"
MAX_CHANGES, MAX_GAPS = 8, 6
RETRY_PAUSE = 3               # seconds between Telegram retries; tests set 0
ASK_TEXT = "Описания вакансии нет. Пришли текст объявления ответом на это сообщение (или «отмена»)"
SHORT_TEXT = "Текст объявления слишком короткий. Пришли полный текст ответом на это сообщение"
SUMMARY_NAMES = {"product": "продукт", "research": "исследования", "ai": "AI"}
ROLE_SHORT = {"Product Manager": "PM"}  # other roles print as their title without the brackets

TAILOR = None   # tests: fake run.tailor_job(job_id, pasted_text)
LETTER = None   # tests: fake letter.write_letter(job_id)
API = None      # tests: fake bot.api(env, method, **params)
ENV = None      # tests: env dict instead of .env
_children = []  # spawned workers {proc, job_id, token, chat_id, kind}, polled so finished ones do not stay zombies
_notices = []   # (job_id, chat_id, kind) of workers that died without releasing their lock, for the bot to report
DIED_TEXT = ("Подготовка резюме оборвалась (процесс остановлен), резюме не отправлено. "
             "Нажми «Адаптировать резюме» ещё раз.")
LETTER_DIED_TEXT = "Письмо оборвалось (процесс остановлен), письмо не отправлено. Нажми «Написать письмо» ещё раз."
NEED_TEXT_LETTER = "Нет текста объявления. Нажми «Адаптировать резюме» и пришли текст - письмо возьмёт его оттуда."
LETTER_LIBRARY = ("Не получилось написать письмо: библиотека письма (cv/letter_library.json) повреждена "
                  "или не прошла проверку.")
LETTER_NOT_FOUND = "Не получилось написать письмо: вакансия не найдена в базе."


# ---------- lock and spawn (used by the bot) ----------

def cv_dir():
    """Same place as run.cv_dir(), kept here so the bot does not import the tailoring code."""
    return Path(os.environ.get("JOBDIGEST_CV_DIR") or store.DB.parent / "cv")


def lock_path(job_id):
    return cv_dir() / job_id / ".lock"


def _read_lock(path):
    """{token, pid, state, started} of a lock file, None when there is none. A bare token (locks written
    before 09.10.2026) reads as a 'legacy' lock, judged by its age only."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("token"), str):
            return data
    except ValueError:
        pass
    return {"token": raw.strip(), "state": "legacy"}


def _write_lock(path, data):
    """Replace the lock content atomically (the path never disappears, so acquire's O_EXCL still holds)."""
    tmp = path.with_name(f".lock-{data['token'][:8]}-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def alive(pid):
    """True while the process exists and is not a zombie (a killed worker not reaped yet is gone for us)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, TypeError):
        return False
    try:
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass  # no /proc (macOS): kill(0) is the answer
    return True


def stale_reason(path):
    """Why the lock at path no longer protects a live run, or None while it does. 'gone' = no lock any more."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return "gone"
    lock = _read_lock(path) or {}
    if age >= LOCK_STALE:
        return f"not touched for {int(age)} s"
    started, pid, state = lock.get("started"), lock.get("pid"), lock.get("state")
    if isinstance(started, (int, float)) and time.time() - started >= LOCK_STALE:
        return f"run started {int(time.time() - started)} s ago"
    if isinstance(pid, int) and state in ("pending", "spawned", "running") and not alive(pid):
        return f"{'bot' if state == 'pending' else 'worker'} process {pid} is gone"
    if state == "pending" and age >= PENDING_STALE:
        return "the worker never started"
    if state in ("spawned", "running") and age >= HEARTBEAT_STALE:
        return f"no heartbeat for {int(age)} s"
    return None


def acquire(job_id):
    """A token when this caller now holds the job's lock; None while a live run holds it. A dead holder
    (process gone, never started, no heartbeat, or past the hard cap) is taken over at once."""
    reap()  # a worker that just died must not count as alive
    path = lock_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            why = stale_reason(path)
            if why is None:
                return None
            if why != "gone":
                print(f"tailor lock for {job_id} taken over: {why}", file=sys.stderr, flush=True)
                path.unlink(missing_ok=True)
            continue
        token = uuid.uuid4().hex
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"token": token, "pid": os.getpid(), "state": "pending", "started": time.time()}))
        return token
    return None


def holds(job_id, token):
    lock = _read_lock(lock_path(job_id))
    return bool(lock and token and lock.get("token") == token)


def mark(job_id, token, pid, state):
    """Record who runs the job now (the spawned pid, then the worker itself), if the lock is still ours."""
    path = lock_path(job_id)
    lock = _read_lock(path)
    if not lock or not token or lock.get("token") != token:
        return False
    _write_lock(path, dict(lock, pid=pid, state=state))
    return True


def release(job_id, token=None):
    """Remove the lock - only our own: a stale lock taken over by a newer press is not ours to remove."""
    path = lock_path(job_id)
    try:
        lock = _read_lock(path)
        if lock is not None and (token is None or lock.get("token") == token):
            path.unlink()
    except FileNotFoundError:
        pass


def _heartbeat(job_id, token, stop):
    """Touch the lock while the run lives; stop once it is no longer ours."""
    path = lock_path(job_id)
    while not stop.wait(HEARTBEAT):
        if not holds(job_id, token):
            return
        try:
            os.utime(path)
        except OSError:
            return


def reap():
    """Poll spawned workers. One that exited but still holds its lock died without cleanup (killed, OOM, a crash
    before main): its lock is released and, unless it exited 0, (job_id, chat_id, kind) is queued for the bot to report."""
    for c in list(_children):
        proc = c["proc"]
        code = proc.poll() if hasattr(proc, "poll") else None
        if code is None:
            continue
        _children.remove(c)
        if c.get("token") and holds(c["job_id"], c["token"]):
            release(c["job_id"], c["token"])
            print(f"tailor worker for {c['job_id']} exited {code} without releasing its lock", file=sys.stderr, flush=True)
            if code != 0:
                _notices.append((c["job_id"], c["chat_id"], c.get("kind", "cv")))


def notices():
    """(job_id, chat_id, kind) of workers that died since the last call; the bot sends died_text(kind) for each."""
    reap()
    out = list(_notices)
    del _notices[:]
    return out


def died_text(kind):
    """What Misha reads when a worker of this kind died without an answer."""
    return LETTER_DIED_TEXT if kind == "letter" else DIED_TEXT


def worker_env():
    """The bot runs as root under systemd: the claude CLI keeps its login in /root, Chrome is the server's."""
    env = dict(os.environ)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        env["HOME"] = "/root"
    if not env.get("CHROME_BIN") and Path(SERVER_CHROME).exists():
        env["CHROME_BIN"] = SERVER_CHROME
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return env


def spawn(job_id, chat_id, token, text=None, kind="cv"):
    """Start the worker detached (own session); its output is appended to data/cv/<job_id>/worker.log.
    A pasted ad text goes through a file next to the versions (no size limit, not visible in ps)."""
    reap()
    folder = cv_dir() / job_id
    folder.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "cv.tailor.worker", job_id, str(chat_id), "--lock", token]
    if kind != "cv":
        args += ["--kind", kind]
    if text:
        path = folder / f"pasted-{time.strftime('%Y%m%d-%H%M%S')}-{token[:6]}.txt"
        path.write_text(text, encoding="utf-8")
        args += ["--text", str(path)]
    with open(folder / "worker.log", "ab") as log:
        proc = subprocess.Popen(args, cwd=PROJECT, env=worker_env(), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _children.append({"proc": proc, "job_id": job_id, "token": token, "chat_id": chat_id, "kind": kind})
    if isinstance(getattr(proc, "pid", None), int):
        mark(job_id, token, proc.pid, "spawned")
    return proc


# ---------- the summary message ----------

def plain(text):
    """Visible text for Telegram: no tags, plain hyphens only (Misha)."""
    return re.sub(r"<[^>]*>", "", str(text or "")).replace("\u2014", "-").replace("\u2013", "-").strip()


def _short(text, n=70):
    text = re.sub(r"</?[bi]>", "", text or "").strip()
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


MAX_TERMS = 5        # claimable terms named in the counts line
FEW_CLAIMABLE, MANY_TERMS = 2, 10


def _names(terms, n=MAX_TERMS):
    terms = [plain(t) for t in terms if plain(t)]
    return ", ".join(terms[:n]) + (f" и ещё {len(terms) - n}" if len(terms) > n else "")


def counts_lines(r):
    """Counts instead of a percentage (Misha 09.10: '0% -> 0%' explained nothing)."""
    c = r.get("counts") or {}
    n, k = c.get("terms") or 0, c.get("claimable") or 0
    if not n:
        return ["Требования вакансии: не удалось посчитать" + (f" ({r['reason']})" if r.get("reason") else "")]
    if not k:
        lines = [f"Требований в вакансии: {n}. По фактам ты не закрываешь требования этой вакансии - "
                 "резюме оставлено базовым по смыслу."]
    else:
        lines = [f"Требований в вакансии: {n}. Ты закрываешь фактами: {k} ({_names(r.get('claimable') or [])}). "
                 f"В резюме было {c.get('before', 0)} из {k}, стало {c.get('after', 0)} из {k}."]
    if k <= FEW_CLAIMABLE and n >= MANY_TERMS:
        lines.append("Вакансия в основном вне твоего опыта.")
    if missing := r.get("claimable_no_wording"):
        lines.append(f"Нет готовой формулировки для: {_names(missing, 10)} (можно добавить в библиотеку)")
    return lines


def summary_label(sid):
    """'B_research' -> 'B (исследования)'."""
    letter, _, rest = str(sid).partition("_")
    return f"{letter} ({SUMMARY_NAMES.get(rest.lower(), rest)})" if rest else letter


def role_names(base):
    return [ROLE_SHORT.get(e["role"], re.sub(r"\s*\([^)]*\)", "", e["role"]).strip()) for e in base["experience"]]


def _swaps(reason):
    m = re.search(r"\(swap: (.*)\)$", reason or "")
    pairs = [p.split(" -> ", 1) for p in m.group(1).split("; ")] if m else []
    return ", ".join(f"«{a}» -> «{b}»" for a, b in (p for p in pairs if len(p) == 2))


def describe(change, roles):
    """One change from library.changes() in plain Russian."""
    field, reason = change.get("field", ""), change.get("reason", "")
    swaps = _swaps(reason)
    if field == "summary":
        m = re.match(r"summary variant (\S+)", reason)
        text = f"Summary: вариант {summary_label(m.group(1))}" if m else "Summary: изменён"
        return text + (f", замена {swaps}" if swaps else "")
    if field == "skills.order":
        return "Навыки: порядок групп изменён"
    if field.startswith("skills"):
        group, _, items = (change.get("after") or "").partition(": ")
        return f"Навыки: {group} - {_short(items, 110)}"
    m = re.match(r"experience\[(\d+)\]", field)
    role = roles[int(m.group(1))] if m and int(m.group(1)) < len(roles) else "Опыт"
    if field.endswith(".order"):
        return f"{role}: порядок пунктов изменён"
    if reason.startswith("dropped"):
        return f"{role}: убран пункт «{_short(change.get('before'))}»"
    if reason.startswith("synonym swap"):
        return f"{role}: {swaps or 'замена слова'}"
    if reason.startswith("library bullet"):
        return f"{role}: добавлен пункт «{_short(change.get('after'))}»"
    return f"{role}: изменено"


WHY_RU = (("term used more than", "слово чаще 3 раз"), ("never-claim", "запрещённый термин"),
          ("new number", "число не из фактов"), ("German words", "немецкие слова"),
          ("skill not in", "навык не из списка"), ("duplicate skill", "повтор навыка"),
          ("more than", "лишний навык"), ("length", "не влезло на страницу"), ("library:", "выбор модели не по правилам"),
          ("bad markup", "разметка"), ("glyph", "символ без шрифта"))
MAX_TEXT = 3900  # Telegram's limit is 4096


def _why_ru(why):
    return next((ru for key, ru in WHY_RU if key in str(why or "")), "правило проверки")


def rejected_line(reverted):
    """'Отклонено проверками: 3 (не влезло на страницу 2, навык не из списка 1)' - what code put back to base."""
    if not reverted:
        return ""
    groups = {}
    for x in reverted:
        k = _why_ru(x.get("why") if isinstance(x, dict) else "")
        groups[k] = groups.get(k, 0) + 1
    parts = ", ".join(f"{k} {n}" for k, n in sorted(groups.items(), key=lambda kv: -kv[1]))
    return f"Отклонено проверками: {len(reverted)} ({parts})"


def _unmet_line(r, gaps):
    """Ad terms the facts do not cover that the model did not list as a gap (QA cvtailor-03: code, not the
    model, decides that SQL/Python are missing)."""
    seen = " ".join((g.get("requirement", "") + " " + g.get("ru", "")).lower() for g in gaps)
    unmet = [t for t in r.get("unmet") or [] if isinstance(t, str) and t.strip() and t.lower() not in seen]
    return f"- Нет в фактах: {_names(unmet, 10)}" if unmet else ""


def warning_lines(r):
    """ATS / PDF check problems as a warning (QA cvtailor-11): they do not stop the CV, but Misha sees them."""
    issues = [str(x) for x in r.get("pdf_issues") or []]
    if not issues:
        return []
    if any("skipped" in x for x in issues):
        return ["", "Внимание: проверка PDF для ATS не выполнена (на сервере нет poppler-utils)."]
    return ["", f"Внимание: проверка PDF для ATS нашла проблемы ({len(issues)}): " + "; ".join(_short(x, 80) for x in issues[:3])]


def summary_text(r, roles):
    lines = counts_lines(r)
    if r.get("used_pasted"):
        lines.append("Текст объявления: тот, что ты присылал раньше.")
    changes = r.get("changes") or []
    reverted = [x for x in r.get("reverted") or [] if isinstance(x, dict)]
    if changes:
        lines += ["", "Что изменено:"] + [f"- {describe(c, roles)}" + (f" - {c['note']}" if c.get("note") else "")
                                          for c in changes[:MAX_CHANGES]]
        if len(changes) > MAX_CHANGES:
            lines.append(f"- и ещё {len(changes) - MAX_CHANGES}")
        if reverted:
            lines.append(rejected_line(reverted))
    elif reverted:  # QA cvtailor-06: an all-rejected answer is not 'the base CV is already best'
        lines += ["", "Изменений нет: выбор модели не прошёл проверки, отправил базовое резюме.", rejected_line(reverted)]
    else:
        lines += ["", "Изменений нет: базовое резюме уже лучший вариант для этой вакансии."]
    gaps = [g for g in r.get("gaps") or [] if isinstance(g, dict) and g.get("requirement")]
    unmet = _unmet_line(r, gaps)
    if gaps or unmet:
        lines += ["", "Чего нет (честно не закрыть):"] + [f"- {_short(g.get('ru') or g['requirement'], 90)}"
                                                          for g in gaps[:MAX_GAPS]]
        if len(gaps) > MAX_GAPS:
            lines.append(f"- и ещё {len(gaps) - MAX_GAPS}")
        if unmet:
            lines.append(unmet)
    lines += warning_lines(r)
    text = "\n".join(lines).replace("\u2014", "-").replace("\u2013", "-")  # Misha: plain hyphens only
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 3].rstrip() + "..."


def result_buttons(job_id, version_id):
    """The answer sheet of this version and 'Отправил резюме', which records which version went out (data-13)."""
    rows = [[{"text": "Шпаргалка для анкеты", "callback_data": f"c:ans:{version_id}"}]] if version_id else []
    applied = f"c:applied:{job_id}" + (f":{version_id}" if version_id else "")
    return rows + [[{"text": "Отправил резюме", "callback_data": applied}]]


# ---------- the run ----------

def _retryable(e):
    """True when the request surely did not land: 429/5xx, or no connection at all. A timeout or a dropped
    connection after sending may have delivered it - a retry could send the PDF twice."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or e.code >= 500
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    return isinstance(reason, (ConnectionRefusedError, socket.gaierror)) or \
        getattr(reason, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH)


def _call(call, env, method, once=False, **params):
    """A Telegram call that must land: three tries; raises when Telegram never accepted it. once=True (an upload):
    retried only when the request surely did not land."""
    last = None
    for attempt in range(3):
        try:
            res = call(env, method, **params)
            if res.get("ok"):
                return res
            last = f"refused: {str(res)[:200]}"
            if once:
                break
        except Exception as e:  # HTTP error, timeout, network
            last = f"{type(e).__name__}: {e}"
            if once and not _retryable(e):
                print(f"{method} failed, not retried (may have been delivered): {last}", file=sys.stderr, flush=True)
                break
        print(f"{method} failed (try {attempt + 1}): {last}", file=sys.stderr, flush=True)
        if attempt < 2:
            time.sleep(RETRY_PAUSE)
    raise RuntimeError(f"Telegram {method}: {last}")


def _job(job_id):
    con = store.connect()
    try:
        return con.execute("select title, company from jobs where id=?", (job_id,)).fetchone()
    finally:
        con.close()


def _version_id(job_id, pdf):
    con = store.connect()
    try:
        row = con.execute("select id from cv_versions where job_id=? and pdf_path=? order by id desc limit 1",
                          (job_id, pdf)).fetchone()
        return row["id"] if row else None
    finally:
        con.close()


def _send_pdf(call, env, chat_id, job_id, pdf):
    job = _job(job_id)
    caption = " · ".join(plain(x) for x in ((job["title"], job["company"]) if job else ()) if plain(x))[:1000]
    path = Path(pdf)
    _call(call, env, "sendDocument", chat_id=chat_id, document=(plain(path.name), path.read_bytes(), "application/pdf"),
          once=True, **({"caption": caption} if caption else {}))


def asks_path(job_id):
    return cv_dir() / job_id / "asks.json"


def ask_for_text(call, env, chat_id, job_id, pasted):
    """Ask for the ad text; the bot starts the worker when Misha replies to this or any earlier ask for the job.
    cv_requests keeps one row per job (the newest ask), so every ask id is also kept in data/cv/<job>/asks.json
    (QA data-6: a reply to an older ask was saved as a note)."""
    res = _call(call, env, "sendMessage", chat_id=chat_id, text=SHORT_TEXT if pasted else ASK_TEXT,
                reply_markup={"force_reply": True, "input_field_placeholder": "Текст объявления"})
    msg_id = res["result"]["message_id"]
    try:
        path = asks_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            ids = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ids = []
        ids = [i for i in ids if isinstance(i, int)][-50:] + [msg_id]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ids), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"asks index not written for {job_id}: {e}", file=sys.stderr, flush=True)
    con = store.connect()
    try:
        con.execute("insert or replace into cv_requests (job_id, asked_msg_id, at) values (?,?,?)",
                    (job_id, msg_id, store.now()))
        con.commit()
    finally:
        con.close()


def asked_job(con, msg_id):
    """The job whose 'Описания вакансии нет' message has this id: the newest ask (cv_requests) or any earlier one."""
    if not msg_id:
        return None
    row = con.execute("select job_id from cv_requests where asked_msg_id=?", (msg_id,)).fetchone()
    if row:
        return row["job_id"]
    for path in cv_dir().glob("*/asks.json"):
        try:
            if msg_id in json.loads(path.read_text(encoding="utf-8")):
                return path.parent.name
        except (OSError, ValueError, TypeError):
            continue
    return None


def deliver(call, env, chat_id, job_id, r, pasted=None):
    status = r.get("status")
    print(f"{store.now()} {job_id}: {status} {r.get('reason', '')}", flush=True)
    if status == "need_text":
        ask_for_text(call, env, chat_id, job_id, pasted)
    elif status == "ok":
        from cv.tailor import run
        base = json.loads(run.BASE_JSON.read_text(encoding="utf-8"))
        _send_pdf(call, env, chat_id, job_id, r["pdf"])
        _call(call, env, "sendMessage", chat_id=chat_id, text=summary_text(r, role_names(base)),
              reply_markup={"inline_keyboard": result_buttons(job_id, _version_id(job_id, r["pdf"]))})
    elif status == "fallback":
        _send_pdf(call, env, chat_id, job_id, r["pdf"])
        reason = _short(plain(r.get("reason")) or "причина не записана", 300).rstrip(".")
        # QA cvtailor-13: the answer sheet is saved for a fallback too, so its button is offered
        _call(call, env, "sendMessage", chat_id=chat_id,
              text=f"Не получилось адаптировать: {reason}. Прислал базовое резюме.",
              reply_markup={"inline_keyboard": result_buttons(job_id, _version_id(job_id, r["pdf"]))})
    else:
        raise RuntimeError(f"unexpected status {status!r}")


# ---------- the cover letter (stage 6) ----------

def _hyphens(text):
    return str(text or "").replace("\u2014", "-").replace("\u2013", "-")


def _cut(text):
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 3].rstrip() + "..."


def letter_summary(r):
    """The second message under the letter: words, which requirement each proof closes, how the first paragraph
    was made, the notes and the German-ad warning. Plain hyphens only, like the CV summary."""
    words = r.get("words") or len(str(r.get("text") or "").split())
    lines = [f"Письмо: {words} слов."]
    evidence = [e for e in r.get("evidence") or [] if isinstance(e, dict) and plain(e.get("requirement"))
                and plain(e.get("label_ru"))]
    if evidence:
        lines.append("Какие требования закрывает:")
        lines += [f"- {plain(e['requirement'])} -> {plain(e['label_ru'])}" for e in evidence[:4]]
    if r.get("hook_source") == "model":
        lines.append("Первый абзац написан под компанию и проверен.")
    else:
        note = plain(r.get("hook_note")).rstrip(".")
        lines.append(f"Первый абзац по шаблону: {note}." if note else "Первый абзац по шаблону.")
    notes = [plain(n) for n in r.get("notes") or [] if plain(n)]
    if notes:
        lines += ["Заметки:"] + [f"- {n}" for n in notes]
    if r.get("german_warning"):
        lines.append("Внимание: объявление на немецком, английский не упомянут - шанс ниже.")
    return _cut(_hyphens("\n".join(lines)))


def deliver_letter(call, env, chat_id, job_id, r):
    """The letter as Misha copies it (plain text, no formatting), then the summary. A missing ad text is asked for
    through the CV button, because that is where the text is pasted."""
    status = r.get("status")
    print(f"{store.now()} {job_id}: letter {status} {r.get('reason', '')}", flush=True)
    if status == "need_text":
        _call(call, env, "sendMessage", chat_id=chat_id, text=NEED_TEXT_LETTER)
    elif status == "ok":
        _call(call, env, "sendMessage", chat_id=chat_id, text=_cut(_hyphens(r["text"])))
        _call(call, env, "sendMessage", chat_id=chat_id, text=letter_summary(r))
    elif status == "blocked":  # a letter-never word is still in the text after the template fallback: nothing is sent
        term = plain(r.get("blocked_term"))[:60] or "?"
        _call(call, env, "sendMessage", chat_id=chat_id,
              text=f"Не получилось написать письмо: в тексте осталось запрещённое ({term}). Письмо не отправлено.")
    else:
        raise RuntimeError(f"unexpected letter status {status!r}")


def _write_letter(job_id):
    """letter.write_letter, imported only when a letter is actually written: the bot and the CV never need it."""
    if LETTER is not None:
        return LETTER(job_id)
    from cv.tailor import letter
    return letter.write_letter(job_id)


def setup_error_text(e):
    """A broken input file stops the run loudly and names the file (QA cvtailor-14)."""
    name = getattr(e, "file", None) or "library.json"
    what = ("библиотека формулировок (cv/library.json) повреждена или не прошла проверку" if name == "library.json"
            else f"файл cv/{name} повреждён или не прошёл проверку")
    return f"Не получилось адаптировать: {what}, резюме не отправлено. Нужно починить этот файл."


def main(job_id, chat_id, pasted=None, token=None, kind="cv"):
    """Tailor one job (kind cv) or write its cover letter (kind letter) and send the result to chat_id. Never returns
    silently: any failure becomes a short Russian message (and a traceback in worker.log). The lock records this
    process and is touched every HEARTBEAT seconds while the run lives; it is released in every case."""
    stop = threading.Event()
    if token and mark(job_id, token, os.getpid(), "running"):
        threading.Thread(target=_heartbeat, args=(job_id, token, stop), daemon=True).start()
    try:
        env = ENV
        if API is not None:
            call = API
        else:
            import bot  # its api() (multipart upload), the IPv4-only resolver and the .env reader
            call = bot.api
            env = env if env is not None else bot.read_env()
        if env is None:
            from digest import load_env
            env = load_env()
    except Exception:
        traceback.print_exc()
        stop.set()
        release(job_id, token)
        return 1
    try:
        from cv.tailor import library
        if kind == "letter":
            try:
                r = _write_letter(job_id)
            except library.LibraryError as e:
                print(f"letter setup error: {e}", file=sys.stderr, flush=True)
                _call(call, env, "sendMessage", chat_id=chat_id, text=LETTER_LIBRARY)
                return 1
            except LookupError:
                _call(call, env, "sendMessage", chat_id=chat_id, text=LETTER_NOT_FOUND)
                return 1
            deliver_letter(call, env, chat_id, job_id, r)
            return 0
        from cv.tailor import run
        try:
            r = (TAILOR or run.tailor_job)(job_id, pasted)
        except library.LibraryError as e:
            print(f"setup error: {e}", file=sys.stderr, flush=True)
            _call(call, env, "sendMessage", chat_id=chat_id, text=setup_error_text(e))
            return 1
        except LookupError:
            _call(call, env, "sendMessage", chat_id=chat_id, text="Не получилось адаптировать: вакансия не найдена в базе.")
            return 1
        deliver(call, env, chat_id, job_id, r, pasted)
        return 0
    except Exception as e:
        traceback.print_exc()
        try:
            text = (f"Не получилось написать письмо: внутренняя ошибка ({type(e).__name__}). Попробуй ещё раз."
                    if kind == "letter" else
                    f"Не получилось адаптировать резюме: внутренняя ошибка ({type(e).__name__}). Попробуй ещё раз.")
            call(env, "sendMessage", chat_id=chat_id, text=text)
        except Exception:
            traceback.print_exc()
        return 1
    finally:
        stop.set()
        release(job_id, token)


def cli(argv=None):
    ap = argparse.ArgumentParser(description="Tailor the CV for one job and send it to Telegram.")
    ap.add_argument("job_id")
    ap.add_argument("chat_id")
    ap.add_argument("--text", help="file with the pasted ad text")
    ap.add_argument("--lock", help="lock token taken by the bot")
    ap.add_argument("--kind", choices=["cv", "letter"], default="cv", help="the CV (default) or the cover letter")
    try:  # no part of the bot token in worker.log, whatever an exception prints (QA ops-05)
        import bot
        bot.mask_streams()
    except Exception:
        traceback.print_exc()
    args = ap.parse_args(argv)
    print(f"{store.now()} start {args.job_id}", flush=True)
    try:
        pasted = Path(args.text).read_text(encoding="utf-8") if args.text else None
    except OSError as e:
        print(f"cannot read --text: {e}", file=sys.stderr, flush=True)
        pasted = None  # tailor_job then asks for the text again instead of staying silent
    return main(args.job_id, args.chat_id, pasted, args.lock, args.kind)


if __name__ == "__main__":
    sys.exit(cli())
