"""One-off for the wave 3 deploy day (stage 7, 26.10.2026): the new title rules for jobs already in the DB.

prepare.py and store.remember() never re-judge a stored row, so the new rules reach only jobs first seen after the
deploy. This sets rule='keep' on the rows the new rules keep, in the window of wave3_preview.py: stored rule not
'keep', new verdict 'keep' (enabled groups only), verdict still NULL (not judged by Misha), not closed. Nothing else
changes: scored_at stays NULL, so prepare.py queues the job for scoring the next morning.
Dry run unless --apply; the dry run writes nothing. Usage: python3 scripts/apply_wave3.py --db PATH [--days 15] [--apply]"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import prefilter  # noqa: E402
from scripts import wave3_preview  # noqa: E402


def plan(con, days):
    """(newest, changes): the rows the rules keep that the stored rule does not, still open for Misha and the feed."""
    newest, since = wave3_preview.window_since(con, days)
    if since is None:
        return None, []
    rows = con.execute("select id, title, company, rule, data from jobs where first_seen >= ? and verdict is null"
                       " and coalesce(closed,0)=0 order by first_seen desc", (since,)).fetchall()
    changes = []
    for row in rows:
        job, stored = wave3_preview.job_of(row)
        if stored != "keep" and prefilter.verdict(job) == "keep":
            changes.append({"id": row["id"], "title": job["title"], "company": row["company"] or "",
                            "reason": prefilter.reason(job)})
    return newest, changes


def apply(con, changes):
    """Sets rule='keep' only; the WHERE repeats the guards, so a verdict or close written meanwhile is left alone."""
    changed = 0
    for c in changes:
        cur = con.execute("update jobs set rule='keep' where id=? and verdict is null and coalesce(closed,0)=0"
                          " and coalesce(rule,'')!='keep'", (c["id"],))
        changed += cur.rowcount
    con.commit()
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="state DB (opened read-only unless --apply)")
    ap.add_argument("--days", type=int, default=15, help="window in days back from the newest first_seen")
    ap.add_argument("--apply", action="store_true", help="write rule='keep' (default: dry run, prints only)")
    args = ap.parse_args(argv)

    con = wave3_preview.connect(args.db, write=args.apply)
    newest, changes = plan(con, args.days)
    if newest is None:
        print("В базе нет показов.")
        return changes
    mode = "записано" if args.apply else "dry run: ничего не записано, добавьте --apply"
    print(f"Самый свежий показ: {newest.replace('T', ' ')}, окно {args.days} дней. Режим: {mode}.")
    print(f"Строк получат rule='keep': {len(changes)}")
    for c in changes:
        print(f"  [{c['reason']}] {c['title']} | {c['company']}")
    if args.apply:
        print(f"Записано строк: {apply(con, changes)}")
    return changes


if __name__ == "__main__":
    main()
