"""Stage 9 (28.10.2026): once a week, check that the vacancies Misha marked Интересно are still open. A posting that
is gone (HTTP 404/410, a JSON-LD validThrough in the past, or a 'job has expired' / 'Stelle besetzt' phrase on the page)
is closed (closed_by 'liveness') and leaves /interesting, so the bot stops offering a dead link. Job boards are never
opened (fetch_posting.DENY, and enrich.JOB_BOARDS minus fetch_posting.ATS): those rows stay 'проверь сам'. Every page
goes through the fetch_posting guards: a public address on every hop, DENY on every hop, robots.txt, a size cap and a
gap per host. Nothing is written to jobs except a closed row; every row checked gets a liveness_checks row.

    python3 liveness.py    # the Sunday step of run.sh: one summary line, exit 0 even when pages fail
"""

import concurrent.futures
import datetime
import html
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import enrich  # noqa: E402
import fetch_posting  # noqa: E402
import store  # noqa: E402

PAGE_SECONDS = 20   # one page, redirects included
WORKERS = 4         # pages at once
RUN_SECONDS = 600   # the whole run; a row not reached by then is 'unknown', 'не успел'
BOARD_REASON = "доска объявлений - проверь сам"
# fetch_posting.CLOSED (full phrases only) plus the phrases of the 28.10 cases (Sportradar, UNIQA, CHECK24, TOWA).
LIVENESS_CLOSED = re.compile(
    fetch_posting.CLOSED.pattern
    + r"|this job has expired|sorry, this job has expired|stelle wurde (leider )?(bereits )?(besetzt|vergeben)"
      r"|nicht mehr (verfügbar|aktiv|ausgeschrieben)|es konnte leider nichts gefunden werden|job nicht gefunden"
      r"|this position (is|has been) (closed|filled)|no longer (open|active)", re.I)


def board_reason(url):
    """'' when the URL may be read; the board reason when its registrable domain is a job board (DENY, or JOB_BOARDS
    minus ATS). Checked before any request, so a board is never requested, not even through a redirect."""
    parsed = fetch_posting._parts(url)
    if parsed is None:
        return ""  # not a plain http(s) address: hop_refusal says why, before any request
    domain = enrich.registrable_domain(parsed[1])
    if domain in fetch_posting.DENY or (domain in enrich.JOB_BOARDS and domain not in fetch_posting.ATS):
        return BOARD_REASON
    return ""


def hop_refusal(url):
    """The reason this URL may not be fetched, or ''. The rule of this step: never a job board, a public address only,
    robots.txt allowing it. The company's own domain is not required (the URLs came from enrich or from Misha)."""
    reason = board_reason(url)
    if reason:
        return reason
    parsed = fetch_posting._parts(url)
    if parsed is None or not fetch_posting._public_host(parsed[0]):
        return "адрес не публичный"
    if not fetch_posting._robots_ok(url):
        return "robots.txt запрещает"
    return ""


class _HopGuard(urllib.request.HTTPRedirectHandler):
    """Every redirect hop is checked before urllib follows it, and the page deadline is kept."""

    def __init__(self, deadline):
        super().__init__()
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fetch_posting._clock() > self.deadline:
            raise fetch_posting._PageTimeout("page deadline")
        reason = hop_refusal(newurl)
        if reason:
            raise fetch_posting._Refused(reason)
        fetch_posting._polite(urllib.parse.urlsplit(newurl).netloc)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url, opener=None):
    """(status, final_url, content_type, text) for one URL through the guards. Raises fetch_posting._Refused (the first
    URL or a hop refused), urllib.error.HTTPError (4xx/5xx after the redirects), _PageTimeout or OSError. text is None
    unless the page is text/html. opener: tests pass one with a fake transport; the default is the real one."""
    reason = hop_refusal(url)
    if reason:
        raise fetch_posting._Refused(reason)
    fetch_posting._polite(urllib.parse.urlsplit(url).netloc)
    deadline = fetch_posting._clock() + PAGE_SECONDS
    if opener is None:
        opener = urllib.request.build_opener(_HopGuard(deadline))
    req = urllib.request.Request(url, headers={"User-Agent": fetch_posting.UA, "Accept": "text/html",
                                               "Accept-Language": "de,en;q=0.8"})
    with opener.open(req, timeout=fetch_posting._budget(PAGE_SECONDS, deadline)) as resp:
        ctype = resp.headers.get_content_type()
        text = None
        if ctype == "text/html":
            raw = fetch_posting._read_capped(resp, deadline)
            text = fetch_posting._decode(raw, resp.headers.get_content_charset() or fetch_posting._meta_charset(raw))
        return resp.getcode(), resp.geturl(), ctype, text


def _expired_json_ld(text, job, today):
    """True when the JobPosting of THIS job has a validThrough before today. Only postings whose title matches the job
    count, the way fetch_posting._from_jsonld matches them: a listing page with other postings never closes our job by
    their dates (QA minor, 28.10). Closed only when every matching posting has passed its date; a matching posting
    without validThrough is open. No matching posting at all: validThrough is ignored."""
    states = []
    for block in fetch_posting.JSONLD.findall(text):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        for node in fetch_posting._jobposting_nodes(data):
            title = html.unescape(node.get("title")) if isinstance(node.get("title"), str) else ""
            title = title.strip()
            if not title or not fetch_posting._title_match(title, job):
                continue
            until = fetch_posting._iso_date(node.get("validThrough"))
            states.append(bool(until) and datetime.date.fromisoformat(until) < today)
    return bool(states) and all(states)


def check_url(url, fetch, today, job=None):
    """(result, reason) for one URL: result is open, closed, unknown. job: the row's company and title, for the
    JSON-LD match. Never raises: every failure is a result."""
    try:
        _status, _final, _ctype, text = fetch(url)
    except fetch_posting._Refused as e:
        return "unknown", e.reason
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "closed", "страница не найдена (404)"
        if e.code == 410:
            return "closed", "страница удалена (410)"
        if e.code in (401, 403, 429):
            return "unknown", f"сайт не пустил ({e.code})"
        return "unknown", f"сайт ответил с ошибкой ({e.code})"
    except (fetch_posting._PageTimeout, TimeoutError, socket.timeout):
        return "unknown", "не открылся (таймаут)"
    except Exception:  # a DNS or connection failure, a broken redirect, a page the parser cannot read: never a crash
        return "unknown", "не открылся"
    if text is None:
        return "open", ""  # not a text page: nothing to read, the link itself answered
    if _expired_json_ld(text, job or {}, today):
        return "closed", "срок вакансии истёк (validThrough)"
    m = LIVENESS_CLOSED.search(fetch_posting._html_text(text))
    if m:
        return "closed", f"на странице: «{m.group(0)}»"
    return "open", ""


def row_url(row):
    """open_url when it is a usable link, else url; '' when neither is."""
    return store.usable_url(row["open_url"]) or store.usable_url(row["url"])


def _json(text):
    try:
        d = json.loads(text or "{}")
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _worker(url, fetch, today, deadline, job):
    """Runs in a thread. It never touches the database: the main thread writes every result."""
    if time.monotonic() >= deadline:
        return "unknown", "не успел", url
    result, reason = check_url(url, fetch, today, job)
    return result, reason, url


def _record(con, counts, row, result, reason, url):
    """One liveness_checks row and, for a closed job, the closing: one commit per row. A row whose state changed while
    the page was read (Misha reopened it, applied or judged it) is not closed: it is recorded as skipped."""
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    if result == "closed":
        cur = con.execute("select data from jobs where id=?", (row["id"],)).fetchone()
        data = _json(cur["data"]) if cur is not None else {}
        data["liveness"] = {"at": checked_at, "reason": reason, "url": url}
        # The condition is part of the write: a verdict or status pressed since the row was read is never overwritten.
        changed = con.execute("update jobs set closed=1, closed_by='liveness', promote=0, data=? where id=?"
                              " and verdict='interested' and status is null and coalesce(closed,0)=0",
                              (json.dumps(data, ensure_ascii=False), row["id"])).rowcount
        if changed != 1:
            result, reason = "skipped", "изменилось во время проверки"
    con.execute("insert into liveness_checks (job_id, checked_at, result, reason, url) values (?,?,?,?,?)",
                (row["id"], checked_at, result, reason, url))
    con.commit()
    counts[result] += 1


def run(con, today=None, fetch=None, budget=RUN_SECONDS, workers=WORKERS):
    """Checks every interested vacancy that is not yet applied to and not closed. Returns the counts:
    checked (all rows), open, closed, unknown, skipped. fetch(url) -> (status, final_url, ctype, text) is injectable
    for tests; the default goes through the guards. budget: seconds for the whole run."""
    today = today or datetime.date.today()
    fetch = fetch or fetch_page
    deadline = time.monotonic() + budget
    counts = {"open": 0, "closed": 0, "unknown": 0, "skipped": 0}
    rows = con.execute("select * from jobs where verdict='interested' and status is null and coalesce(closed,0)=0"
                       " order by verdict_at, rowid").fetchall()
    todo = []
    for row in rows:
        url = row_url(row)
        if (_json(row["data"]).get("liveness") or {}).get("reopened_at"):
            _record(con, counts, row, "skipped", "вернул Миша", url)
        elif not url:
            _record(con, counts, row, "unknown", "нет ссылки", "")
        elif board_reason(url):
            _record(con, counts, row, "unknown", BOARD_REASON, url)
        else:
            todo.append((row, url))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(_worker, url, fetch, today, deadline, {"company": row["company"], "title": row["title"]}):
               (row, url) for row, url in todo}
    seen = set()
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=max(0.0, deadline - time.monotonic())):
            row, _url = futures[fut]
            seen.add(fut)
            result, reason, url = fut.result()
            _record(con, counts, row, result, reason, url)
    except concurrent.futures.TimeoutError:
        pass  # the budget is spent: the rest is marked below, nothing else is started
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for fut, (row, url) in futures.items():
        if fut not in seen:
            _record(con, counts, row, "unknown", "не успел", url)
    checked = sum(counts.values())
    return {"checked": checked, **counts}


def main():
    con = store.connect()
    try:
        res = run(con)
    finally:
        con.close()
    print(f"liveness: checked {res['checked']}, open {res['open']}, closed {res['closed']}, "
          f"unknown {res['unknown']}, skipped {res['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
