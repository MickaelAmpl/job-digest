"""Morning step after collect/fetch/prefilter: remember every job, record the rule verdict
for new ones (and for older rows stored without one), mark when each job was last seen,
record which company feeds answered, reopen feed-closed jobs that are back in the feed,
close company-feed jobs the company no longer lists, then build the scoring queue from
the database - every rule-approved, still unscored job in the store.WINDOW_DAYS window
(so an interrupted morning is retried) - fetch missing descriptions where the job system
offers them, and write out/to_score.json. A company feed read only in part (capped) never closes
jobs, and yesterday's company files are not taken as today's answer."""

import html
import json
import re
import urllib.request

import health
import store

DESC_LIMIT = 8000   # same as fetch_companies / enrich: the whole posting, requirements and pay included
SCORE_TRIES = 3     # mornings a fallback-scored job (Claude down) is queued again
ENRICH_TRIES = 3
MAX_ENRICH_ERRORS = 5  # = enrich.MAX_ERRORS


def txt(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def smartrecruiters_description(url):
    m = re.match(r"https://jobs\.smartrecruiters\.com/([^/]+)/(\d+)", url or "")
    if not m:
        return ""
    req = urllib.request.Request(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}",
                                 headers={"User-Agent": "Mozilla/5.0"})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
    except Exception:
        return ""
    sec = (r.get("jobAd") or {}).get("sections") or {}
    keys = ("jobDescription", "qualifications", "additionalInformation")  # pay and hours are often in the last
    return store.scrub(txt(" ".join((sec.get(k) or {}).get("text", "") for k in keys)))[:DESC_LIMIT]


def read_json(name):
    with open(store.OUT / name, encoding="utf-8") as f:
        return json.load(f)


def answered_companies(feed):
    """(answered, capped): companies whose feed returned at least one job today, and those of them read
    only in part. fetch_companies.py writes every feed's total (before the Austria filter) to
    company_feeds.json, so a company that still lists jobs, just none in Austria any more, counts as
    answered. Without that file, or if it is older than the jobs file (a run killed between the two
    writes), the companies present in the feed - all treated as capped, so nothing is closed on them.
    Files not written this morning answer nothing (fetch_companies.py did not run or died)."""
    totals_path, jobs_path = store.OUT / "company_feeds.json", store.OUT / "company_jobs.json"
    if not store.fresh_out("company_jobs.json"):
        return set(), set()
    try:
        if totals_path.stat().st_mtime >= jobs_path.stat().st_mtime:
            status = read_json("company_feeds.json")
            totals = status.get("totals", status) if isinstance(status, dict) else {}
            capped = set(status.get("capped") or []) if "totals" in status else set()
            return {cid for cid, n in totals.items() if isinstance(n, int) and n}, capped
    except (OSError, ValueError, AttributeError):
        pass
    present = {j.get("company_id") or j.get("company") for j in feed}
    return present, present


def feed_index(feed):
    return ({store.dedupe_key(j) for j in feed}, {j.get("url") for j in feed if j.get("url")},
            {store.same_link(j.get("url")) for j in feed} - {""}, {store.link_id(j.get("url")) for j in feed} - {""})


def in_feed(row, index):
    """Same test for closing and reopening, so a job never flips between the two in one run.
    The job number counts too: a renamed posting is still the same posting (data-8)."""
    keys, urls, links, ids = index
    return (row["key"] in keys or row["url"] in urls or store.same_link(row["url"]) in links
            or store.link_id(row["url"]) in ids)


def reopen_returned(con, index, run_at):
    """A job the feed rule closed that is back in today's feed is open again (in the window from today).
    Jobs closed because the original posting said so (closed_by='enrich') are never reopened.
    pipeline-05: if it was never a card - only a line in the closed list (listed=1) - and Claude scored it
    50+, it is sent as a card now (promote=2); a scored job never sent at all simply goes the normal way."""
    reopened = 0
    for r in con.execute("select id, key, url, listed, sent_at, score, score_json from jobs where closed=1"
                         " and closed_by='feed' and verdict is null").fetchall():
        if in_feed(r, index):
            real = (r["score"] or 0) >= 50 and '"unscored": true' not in (r["score_json"] or "")
            back = 2 if r["sent_at"] and r["listed"] == 1 and real else None
            con.execute("update jobs set closed=0, closed_by=null, last_seen=?, promote=coalesce(?, promote)"
                        " where id=?", (run_at, back, r["id"]))
            reopened += 1
    return reopened


def close_dropped(con, index, companies, capped=()):
    """A company-feed job the company no longer lists is gone: close it. Only for companies that
    answered today in full - an empty, failed or capped feed (only its newest postings) says nothing
    about its old jobs (pipeline-01: RBI/KPMG jobs were 'removed' just for scrolling off page 1)."""
    closed = 0
    for r in con.execute("select id, key, company, url, company_id from jobs where source='company'"
                         " and verdict is null and coalesce(closed,0)=0").fetchall():
        cid = r["company_id"] or r["company"]
        if cid in companies and cid not in capped and not in_feed(r, index):
            con.execute("update jobs set closed=1, closed_by='feed' where id=?", (r["id"],))
            closed += 1
    return closed


def main():
    con = store.connect()
    jobs = read_json("prefiltered.json")
    run_at = store.now()
    new = new_kept = backfilled = 0
    for j in jobs:
        if store.remember(con, j):  # every job is remembered, so rejected ones are not re-checked tomorrow
            new += 1
            new_kept += j.get("rule") == "keep"  # health: zero for two mornings is a quiet failure (health.py)
            con.execute("update jobs set rule=?, data=? where id=?",
                        (j.get("rule", ""), json.dumps(j, ensure_ascii=False), j["id"]))
        else:
            # Rows stored without a rule (the 07.10 baseline) get one now; data only while still unscored,
            # so a scored row keeps the description and links enrich found.
            cur = con.execute("update jobs set rule=?, data=case when scored_at is null then ? else data end"
                              " where id=? and rule is null",
                              (j.get("rule", ""), json.dumps(j, ensure_ascii=False), j["id"]))
            backfilled += cur.rowcount
        con.execute("update jobs set last_seen=? where id=?", (run_at, j["id"]))
    # Rows not in today's file (e.g. 07.10 Adzuna/email jobs) still carry their prefilter rule in data.
    for r in con.execute("select id, data from jobs where rule is null").fetchall():
        try:
            rule = json.loads(r["data"] or "{}").get("rule")
        except (ValueError, AttributeError):
            rule = None
        if rule:
            con.execute("update jobs set rule=? where id=?", (rule, r["id"]))
            backfilled += 1
    con.execute("insert or replace into meta (k, v) values ('last_run', ?)", (run_at,))
    try:
        feed = read_json("company_jobs.json") if store.fresh_out("company_jobs.json") else []
    except (OSError, ValueError):
        feed = []
    companies, capped = answered_companies(feed)
    con.executemany("insert or replace into company_feed (company_id, last_ok) values (?, ?)",
                    [(cid, run_at) for cid in companies])
    index = feed_index(feed)
    reopened = reopen_returned(con, index, run_at)
    closed = close_dropped(con, index, companies, capped)
    con.commit()  # release the write lock before any network calls (bot.py writes button presses)
    health.record("prepare", new=new, new_kept=new_kept)

    cond, params = store.in_window()
    # Unscored jobs; jobs Claude could not score (fallback 55: login expired, limit hit), again on the next
    # SCORE_TRIES mornings - a delivered card keeps its card, only its score is updated (pipeline-02);
    # and jobs scored by title only and not judged by Misha: postings from yesterday are often not in search
    # engines yet, so enrich tries again (max ENRICH_TRIES searches) while not yet a card, and a card whose
    # original was never searched (the enrich budget ran out that morning, pipeline-11) gets its search too.
    rows = con.execute(f"select id, data, enrich_tries, scored_at, score_tries from jobs where rule='keep' and {cond}"
                       " and (scored_at is null"
                       f" or coalesce(score_tries,0) between 1 and {SCORE_TRIES - 1}"
                       f" or (coalesce(has_desc,1)=0 and coalesce(enrich_tries,0) < {ENRICH_TRIES}"
                       " and (sent_at is null or listed=1 or coalesce(enrich_tries,0)=0)))"
                       " order by coalesce(enrich_tries,0), first_seen desc", params).fetchall()
    queue = []
    for r in rows:
        try:
            j = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        j["id"] = r["id"]
        j["enrich_tries"] = r["enrich_tries"] or 0
        if r["scored_at"] and not r["score_tries"] and int(j.get("enrich_errors") or 0) >= MAX_ENRICH_ERRORS:
            continue  # only here for another search, and its searches keep failing: not worth a rescore
        if not j.get("description"):
            j["description"] = smartrecruiters_description(j.get("url"))
        queue.append(store.scrub(j))
    with open(store.OUT / "to_score.json", "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=1)
    retry = sum(1 for j in queue if j["enrich_tries"])
    print(f"seen {len(jobs)}, new {new}, rule backfilled {backfilled}, feeds answered {len(companies)}, "
          f"closed by feed {closed}, reopened {reopened}, capped feeds (never closed) {len(capped)}, "
          f"scoring queue: {len(queue)} (of them retries of title-only jobs: {retry})")


if __name__ == "__main__":
    main()
