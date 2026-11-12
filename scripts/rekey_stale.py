"""One-off (stage 7 S7-5, 27.10.2026): give stale dedupe keys the key the current rule gives them.

connect() re-keys only when KEY_VERSION goes up, so a row keyed under an older rule keeps that key: a new posting
with the same company and title then misses the row (missed merges only). For every row this computes
store.dedupe_key(company, title, url). A row whose stored key differs moves to the new key when no other row holds
it; a row whose new key another row holds is a conflict: listed, not changed. A row that moves frees its old key, so
a chain of moves resolves in one run and a second run changes nothing. Ids never change. Skipped, never touched:
source='manual' (Misha's own rows) and closed_by='manual_removed' (see store.remove_manual).
Dry run by default; --apply writes, all moves in one transaction. The dry run opens the DB read-only.
Usage: python3 scripts/rekey_stale.py [--db PATH] [--apply]   (--db: a copy of the state DB, for a test)."""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store  # noqa: E402

EXAMPLES = 15  # rekey lines printed; every conflict is printed


def connect(db, write=False):
    """Read-only unless write=True (mode=rw never creates a missing file). Autocommit: main() opens the transaction."""
    con = sqlite3.connect(Path(db).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True,
                          timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def plan(con):
    """(stale, moves, conflicts, skipped, rows): stale = rows whose key is not the current one (skipped ones included),
    moves = (row, new_key) in the order they are written, conflicts = (row, new_key, holder_id) left as they are,
    skipped = stale rows protected as manual or removed. Holders are taken from the DB as it is when the pass starts."""
    rows = con.execute("select id, key, company, title, url, source, closed_by from jobs"
                       " order by first_seen, id").fetchall()
    by_id = {r["id"]: r for r in rows}
    held = {}  # key -> id: every row keeps its key until it moves
    for r in rows:
        if r["key"] is not None:
            held.setdefault(r["key"], r["id"])
    stale = skipped = 0
    pending = []
    for r in rows:
        new = store.dedupe_key({"company": r["company"], "title": r["title"], "url": r["url"]})
        if new == r["key"]:
            continue
        stale += 1
        if (r["source"] or "") == "manual" or (r["closed_by"] or "") == "manual_removed":
            skipped += 1
        else:
            pending.append((r, new))
    moves = []
    moved = True
    while moved and pending:  # one pass per round: a move frees a key that a later row in the next round can take
        moved, waiting = False, []
        for r, new in pending:
            if new in held:
                waiting.append((r, new))
                continue
            if held.get(r["key"]) == r["id"]:
                del held[r["key"]]
            held[new] = r["id"]
            moves.append((r, new))
            moved = True
        pending = waiting
    conflicts = [(r, new, held[new]) for r, new in pending]
    return stale, moves, conflicts, skipped, by_id


def apply(con, moves):
    """New key only. The WHERE repeats the old key, so a row changed meanwhile is left alone."""
    done = 0
    for r, new in moves:
        cur = con.execute("update jobs set key=? where id=? and key is ?", (new, r["id"], r["key"]))
        done += cur.rowcount
    return done


def label(r):
    return f"{r['id']} {r['company'] or '-'} | {r['title'] or '-'}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", help="state DB (default: data/state.db)")
    ap.add_argument("--apply", action="store_true", help="write the new keys (default: dry run, prints only)")
    args = ap.parse_args(argv)
    db = Path(args.db) if args.db else store.DB
    con = connect(db, write=args.apply)
    if args.apply:
        con.execute("BEGIN IMMEDIATE")  # the bot cannot write between our read and our writes
        try:
            stale, moves, conflicts, skipped, by_id = plan(con)
            done = apply(con, moves)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    else:
        stale, moves, conflicts, skipped, by_id = plan(con)
        done = 0
    con.close()
    mode = f"written: {done}" if args.apply else "dry run, nothing written, add --apply"
    print(f"stale {stale}, rekeyed {len(moves)}, conflicts {len(conflicts)}, skipped {skipped} ({mode})")
    for r, new in moves[:EXAMPLES]:
        print(f"  {label(r)}\n      key: {r['key']} -> {new}")
    if len(moves) > EXAMPLES:
        print(f"  ... and {len(moves) - EXAMPLES} more rekeys")
    if conflicts:
        print("conflicts (not changed):")
    for r, new, holder in conflicts:
        h = by_id[holder]
        print(f"  {label(r)}\n      wants key: {new}\n      held by: {label(h)} ({h['source'] or '-'})")
    return moves, conflicts


if __name__ == "__main__":
    main()
