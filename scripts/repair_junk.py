"""One-off repair (stage 7, step 2, 16.10.2026): the old letter parser saved LinkedIn's service text ('See all
jobs', 'Edit alert', saved-search names) as title and company of some jobs rows. The job number in the url
(/jobs/view/<id>/) is real. Each such row is repaired from the alert mails read by the fixed parser
(collect.parse_linkedin): title, company, location and the data JSON get the real values, and the row is set up
for a new score and description check (scored_at, score_tries, has_desc, enrich_tries, promote). The id and the
dedupe key stay; verdict, reasons, status and sent_at are never touched. A row whose job number is not in the
mails is closed (closed=1, closed_by='junk'), never deleted. The dedupe key is recomputed with store.dedupe_key
(kept when another row already has it; reported). msg is cleared so the bot rebuilds the card text. Idempotent: a
repaired row is no longer junk. Run this BEFORE scripts/requeue_title_only.py.

Mails: --mails DIR reads linkedin*.txt files (tests, the dry run on a copy); without it the mail is read live from
IMAP, read-only, LinkedIn only, since --since (default: the collector's lookback limit). --dry-run prints and writes
nothing. Honours JOBDIGEST_DB through store.connect().

Usage: python3 scripts/repair_junk.py [--dry-run] [--mails DIR] [--since YYYY-MM-DD]
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collect  # noqa: E402
import prefilter  # noqa: E402  (the rule is recomputed with the same function as the morning run)
import store  # noqa: E402

JOB_NUMBER = re.compile(r"/jobs/view/(\d+)")


def job_number(url):
    m = JOB_NUMBER.search(url or "")
    return m.group(1) if m else ""


def junk(row):
    """True when the title or the company is LinkedIn service text (the same rule the parser uses)."""
    return collect.linkedin_service(row["title"] or "") or collect.linkedin_service(row["company"] or "")


def mail_jobs(args):
    """LinkedIn job number -> the job the fixed parser reads from the alert mails (first mail wins)."""
    if args.mails:
        bodies = [p.read_text(encoding="utf-8") for p in sorted(Path(args.mails).glob("linkedin*.txt"))]
    else:
        since = (datetime.strptime(args.since, "%Y-%m-%d") if args.since
                 else datetime.now() - timedelta(days=collect.MAX_LOOKBACK))
        bodies = [body for src, _, body in collect.fetch_alert_emails(collect.load_env(), since)
                  if src == "linkedin"]
    found = {}
    for body in bodies:
        for j in collect.parse_linkedin(body):
            found.setdefault(j["ext_id"][3:], collect.clean(j))
    return found


def description(raw):
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return ""
    return str(data.get("description") or "") if isinstance(data, dict) else ""


def repaired_data(raw, job):
    try:
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    data.update(title=job["title"], company=job["company"], location=job["location"], extra=job["extra"])
    return json.dumps(data, ensure_ascii=False)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mails", help="folder of sanitized LinkedIn mail .txt files instead of live IMAP")
    ap.add_argument("--since", help="live IMAP: first day to read, YYYY-MM-DD")
    args = ap.parse_args(argv)

    if args.dry_run:  # read-only: no store.connect(), which would run migrations on the live file
        con = sqlite3.connect(f"file:{store.DB}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
    else:
        con = store.connect()
    mails = mail_jobs(args)
    print(f"job numbers in the alert mails: {len(mails)}")

    # Only rows this script may touch: junk and either open or closed by an earlier run of this script.
    rows = [r for r in con.execute(
        "select id, url, title, company, location, data, verdict, status, sent_at, closed, closed_by"
        " from jobs where closed_by is null or closed_by='junk' order by first_seen") if junk(r)]
    repair, close, warned, dup, taken = [], [], [], [], {}
    dup_close = {}  # row id -> the row that owns the dedupe key (closed_by='duplicate')
    for r in rows:
        number = job_number(r["url"])
        job = mails.get(number)
        if job:
            if r["closed_by"] is None and r["closed"] == 1:
                continue  # closed by hand or by a rule we do not own
            # The dedupe key is recomputed with store's rule (as remember() does for new jobs). If another row
            # already has that key, the key stays as it is (the key is unique) and the clash is reported.
            key = store.dedupe_key({"company": job["company"], "title": job["title"], "url": r["url"]})
            other = con.execute("select id from jobs where key=? and id!=?", (key, r["id"])).fetchone()
            owner = other[0] if other else taken.get(key)
            if owner:
                dup.append(("key", r["id"], owner))
                key = None
                if r["verdict"] is None:  # nobody judged it: the owner of the key wins, this row is closed
                    dup_close[r["id"]] = owner
            else:
                taken[key] = r["id"]
            rule = prefilter.verdict({"title": job["title"], "description": description(r["data"])})
            repair.append((r, job, key, rule))
            if r["verdict"] or r["status"]:  # Misha's own mark on the row
                warned.append(r["id"])
            same = [o for o in con.execute("select id, title, company, closed from jobs where url like ? and id != ?",
                                           (f"%/jobs/view/{number}/%", r["id"]))
                    if not o["closed"] and not junk(o)]
            if same:
                dup.append(("job", r["id"], same[0]["id"]))
        elif r["closed_by"] is None and not r["closed"]:
            close.append(r)

    for r, job, _, rule in repair:
        print(f"REPAIR {r['id']}  {(r['title'] or '')[:40]} / {(r['company'] or '')[:30]}")
        print(f"       -> {job['title'][:60]} / {job['company'][:40]} / {job['location'][:30]}  rule={rule}")
        if r["id"] in warned:
            print(f"       WARNING verdict={r['verdict']} status={r['status']}: kept as is")
    for r in close:
        print(f"CLOSE  {r['id']}  {(r['title'] or '')[:60]}  (job number not in the mails)")
    for kind, rid, other in dup:
        if kind == "key" and rid in dup_close:
            print(f"DUPLICATE {rid}: its repaired key is row {other}'s key: closed as duplicate (no verdict)")
        elif kind == "key":
            print(f"WARNING {rid}: its repaired key is row {other}'s key, verdict kept: stays open, decide by hand")
        else:
            print(f"WARNING {rid}: after repair the same job number as row {other}: not changed, decide by hand")
    verb = "would repair" if args.dry_run else "repaired"
    print("REMINDER: run scripts/repair_junk.py BEFORE scripts/requeue_title_only.py")
    print(f"junk rows: {len(rows)}, {verb}: {len(repair)}, closed as duplicate: {len(dup_close)}, "
          f"{'would close' if args.dry_run else 'closed'}: {len(close)}, with Misha's verdict: {len(warned)}")

    if not args.dry_run:
        # only a row that still passes the pre-filter ('keep') is rescored; other rules just get the new rule
        con.executemany("update jobs set title=?, company=?, location=?, data=?, key=coalesce(?, key), msg=null,"
                        " rule=?, scored_at=case when ?='keep' then null else scored_at end,"
                        " score_tries=0, has_desc=0, enrich_tries=0, promote=0, closed=0, closed_by=null where id=?",
                        [(job["title"], job["company"], job["location"], repaired_data(r["data"], job), key, rule,
                          rule, r["id"]) for r, job, key, rule in repair])
        con.executemany("update jobs set closed=1, closed_by='duplicate', dup_of=? where id=?",
                        [(owner, rid) for rid, owner in dup_close.items()])
        con.executemany("update jobs set closed=1, closed_by='junk' where id=?", [(r["id"],) for r in close])
        con.commit()
    con.close()


if __name__ == "__main__":
    main(sys.argv[1:])
