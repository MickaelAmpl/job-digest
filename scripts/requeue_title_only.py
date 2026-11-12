"""One-off (stage 7 step 1.4, 16.10.2026): put the open jobs that were scored by title only back into the enrich
queue, so the next morning's enrich searches them again with the new fetch (posting text, paraphrase, closed, date).
A row qualifies when it is a 'keep' job still in the window, not closed, not judged by Misha, and has no
description (has_desc 0). Its enrich tries (and enrich_errors in the data JSON, if present) go back to 0; prepare.py
then queues it again (the has_desc=0 clause; enrich_tries=0 also lets a sent row through).
Idempotent: the ids a run requeued are kept in meta ('requeue_title_only') and skipped by every later run.
Usage: python3 scripts/requeue_title_only.py [--dry-run] [--db PATH]   (--db: a copy, for a test)."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store  # noqa: E402

META_KEY = "requeue_title_only"


def done_ids(con):
    row = con.execute("select v from meta where k=?", (META_KEY,)).fetchone()
    try:
        return set(json.loads(row["v"])) if row else set()
    except (ValueError, TypeError):
        return set()


def pending(con):
    """Rows to requeue this run: the qualifying rows minus those an earlier run already requeued."""
    cond, params = store.in_window()
    rows = con.execute("select id, title, company, data, enrich_tries from jobs where rule='keep'"
                       " and coalesce(closed,0)=0 and coalesce(has_desc,1)=0 and verdict is null"
                       f" and {cond} order by company, title", params).fetchall()
    done = done_ids(con)
    return [r for r in rows if r["id"] not in done]


def requeue(con, rows):
    for r in rows:
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            data = {}
        data["enrich_tries"] = 0
        if "enrich_errors" in data:
            data["enrich_errors"] = 0
        con.execute("update jobs set enrich_tries=0, data=? where id=?",
                    (json.dumps(store.scrub(data), ensure_ascii=False), r["id"]))
    ids = sorted(done_ids(con) | {r["id"] for r in rows})
    con.execute("insert or replace into meta (k, v) values (?, ?)", (META_KEY, json.dumps(ids)))
    con.commit()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="count and list only, write nothing")
    ap.add_argument("--db", help="path to a state DB copy (default: data/state.db)")
    args = ap.parse_args(argv)
    if args.db:
        store.DB = Path(args.db)
    con = store.connect()
    rows = pending(con)
    print(f"to requeue: {len(rows)}" + (" (dry run, nothing written)" if args.dry_run else ""))
    for r in rows:
        print(f"  {r['company'][:30]} | {r['title'][:70]} | enrich_tries now {r['enrich_tries'] or 0}")
    if not args.dry_run and rows:
        requeue(con, rows)
        print(f"requeued: {len(rows)}")
    con.close()
    return rows


if __name__ == "__main__":
    main()
