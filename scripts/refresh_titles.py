"""One-off (stage 7 S7-4, 27.10.2026): put the feed's title on a company's rows whose link is still in its feed.

store.remember() renames a stored row only when its link changes, so rows stored before a title rule changed keep the
old title (UniCredit: 20 rows still show "AT - EXT - WP - Praktikum - UCBA - ..."). This sets jobs.title from the feed
job with the same url and the same company_id. Only the title column changes: key, id and every other column stay.
Manual rows (source='manual') are skipped: Misha wrote their title himself.
Dry run by default; --apply writes, in one transaction. The dry run opens the DB read-only and writes nothing.
Usage: python3 scripts/refresh_titles.py --company unicredit-bank-austria [--feed PATH] [--db PATH] [--apply]
(--db: a copy of the state DB, for a test; --feed default: out/company_jobs.json)."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store  # noqa: E402


def connect(db, write=False):
    """Read-only unless write=True (mode=rw never creates a missing file). Autocommit: main() opens the transaction."""
    con = sqlite3.connect(Path(db).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True,
                          timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def feed_titles(path, company):
    """url -> title for the company's jobs in the feed file; the first title wins when a url is listed twice."""
    titles = {}
    for job in json.loads(Path(path).read_text(encoding="utf-8")):
        url, title = job.get("url") or "", job.get("title") or ""
        if job.get("company_id") == company and url and title:
            titles.setdefault(url, title)
    return titles


def plan(con, company, titles):
    """(matched, changes): the company's rows (not manual) whose url is in the feed;
    changes = those whose title differs."""
    rows = con.execute("select id, company, title, url from jobs where company_id=?"
                       " and coalesce(source,'')!='manual' order by first_seen, id", (company,)).fetchall()
    matched, changes = 0, []
    for r in rows:
        new = titles.get(r["url"])
        if new is None:
            continue
        matched += 1
        if new != r["title"]:
            changes.append({"id": r["id"], "company": r["company"] or "", "old": r["title"] or "", "new": new})
    return matched, changes


def apply(con, changes):
    """Title only. The WHERE repeats the old title and the manual guard, so a row changed meanwhile is left alone."""
    changed = 0
    for c in changes:
        cur = con.execute("update jobs set title=? where id=? and title is ?"
                          " and coalesce(source,'')!='manual'", (c["new"], c["id"], c["old"]))
        changed += cur.rowcount
    return changed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--company", required=True, help="companies.json id, e.g. unicredit-bank-austria")
    ap.add_argument("--feed", help="feed file (default: out/company_jobs.json)")
    ap.add_argument("--db", help="state DB (default: data/state.db)")
    ap.add_argument("--apply", action="store_true", help="write the titles (default: dry run, prints only)")
    args = ap.parse_args(argv)
    feed = Path(args.feed) if args.feed else store.OUT / "company_jobs.json"
    if not feed.is_file():
        ap.error(f"no feed file: {feed}")
    db = Path(args.db) if args.db else store.DB
    con = connect(db, write=args.apply)
    titles = feed_titles(feed, args.company)
    if args.apply:
        con.execute("BEGIN IMMEDIATE")
        try:
            matched, changes = plan(con, args.company, titles)
            changed = apply(con, changes)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    else:
        matched, changes = plan(con, args.company, titles)
        changed = 0
    con.close()
    mode = f"written: {changed}" if args.apply else "dry run, nothing written, add --apply"
    print(f"{args.company} ({len(titles)} feed jobs): matched {matched}, changed {len(changes)}, "
          f"unchanged {matched - len(changes)} ({mode})")
    for c in changes:
        print(f"  {c['id']}  {c['old']} -> {c['new']}")
    return changes


if __name__ == "__main__":
    main()
