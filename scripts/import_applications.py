"""Stage 8 seed: the applications Misha sent before the bot knew about them (research/applications_seed.json, from his
chat list of 26.10.2026). Each row becomes a manual application (store.add_manual), or attaches to the bot card it
already has (existing_id, or the one exact candidate).

Dry run by default: prints every row with the outcome it would get and the candidates the store sees, and writes
nothing (the dry run runs the same store.connect() migration every bot start runs, which is idempotent).
--apply writes, one transaction per row. A second --apply changes nothing: a row is left alone when a manual row
with its key exists (even one Misha removed), or when its target already has a status or was reset by Misha.

    python3 scripts/import_applications.py                  # dry run on the DB store.py points at
    python3 scripts/import_applications.py --apply          # write it
    python3 scripts/import_applications.py --db /path/copy.db --apply
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import store  # noqa: E402

SEED = ROOT / "research" / "applications_seed.json"
REQUIRED = ("company", "title", "applied_at")


def _day(value, field, i):
    try:
        datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"row {i}: {field} must be YYYY-MM-DD, got {value!r}")
    return str(value)


def load_rows(path):
    """The seed rows, checked: a list of objects with company, title, applied_at (YYYY-MM-DD). status defaults to
    'applied'; status_at, channel, note, url and existing_id are optional. Raises ValueError with the row number."""
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("the seed file must be a JSON list")
    out = []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise ValueError(f"row {i}: not an object")
        for field in REQUIRED:
            if not str(r.get(field) or "").strip():
                raise ValueError(f"row {i}: {field} is missing")
        status = r.get("status") or "applied"
        if status not in store.STATUSES:
            raise ValueError(f"row {i}: status {status!r} is not one of {store.STATUSES}")
        out.append({
            "company": str(r["company"]).strip(), "title": str(r["title"]).strip(),
            "url": str(r.get("url") or "").strip(), "applied_at": _day(r["applied_at"], "applied_at", i),
            "status": status, "status_at": _day(r["status_at"], "status_at", i) if r.get("status_at") else None,
            "channel": str(r.get("channel") or ""), "note": str(r.get("note") or ""),
            "existing_id": str(r["existing_id"]) if r.get("existing_id") else None,
        })
    return out


def _skip_reason(con, job, row):
    """Why this row is left alone on every run, or None when it must be written."""
    manual = store.manual_row_for_key(con, store.dedupe_key(job))  # S8-5: a removed row keeps its moved key
    if manual is not None:
        note = " (you removed it, it stays removed)" if manual["closed_by"] == "manual_removed" else ""
        return f"manual row {manual['id']} is already there{note}"
    target = store.application_target(con, job, row["existing_id"])
    if target is not None and target["source"] != "manual":
        if target["status"] is not None:
            return f"row {target['id']} already has status {target['status']}"
        if con.execute("select 1 from status_log where job_id=? and status='reset'", (target["id"],)).fetchone():
            return f"row {target['id']}: you reset its status before"
    return None


def _show(c):
    sent = "sent" if c["sent_at"] else "not sent"
    return (f"      {c['id']} | {c['source']} | {c['company']} / {c['title']} | verdict {c['verdict'] or '-'}"
            f" | status {c['status'] or '-'} | {sent} | closed {c['closed_by'] or '-'}"
            f" | first seen {str(c['first_seen'] or '')[:10]}")


def run(con, rows, apply, out):
    """Process the rows in one explicit transaction, a savepoint per row: a failing row undoes only itself. Dry run
    rolls everything back at the end, so each row still sees the rows before it, as on a real run."""
    counts = {"created": 0, "attached": 0, "unchanged": 0, "revived": 0, "error": 0}
    con.execute("begin")
    for i, row in enumerate(rows, 1):
        job = {"company": row["company"], "title": row["title"], "url": row["url"]}
        when = f"applied {row['applied_at']}" + ("" if row["status"] == "applied" else f", status {row['status']}")
        print(f"[{i}] {row['company']} / {row['title']} ({when})", file=out)
        cands = store.find_application_candidates(con, row["company"], row["title"], row["url"])
        if cands:
            print("    candidates:", file=out)
            for c in cands:
                print(_show(c), file=out)
        else:
            print("    candidates: none", file=out)
        con.execute("savepoint seed_row")
        try:
            skip = _skip_reason(con, job, row)
            if skip is None:
                jid, outcome = store.add_manual(con, job, row["applied_at"], status=row["status"],
                                                status_at=row["status_at"], channel=row["channel"], note=row["note"],
                                                attach_to=row["existing_id"])
                detail = f"row {jid}"
            else:
                outcome, detail = "unchanged", skip
            con.execute("release seed_row")
        except (ValueError, sqlite3.Error) as e:
            con.execute("rollback to seed_row")
            con.execute("release seed_row")
            outcome, detail = "error", str(e)
        counts[outcome] += 1
        label = outcome if apply or outcome in ("unchanged", "error") else f"would be {outcome}"
        print(f"    -> {label}: {detail}", file=out)
        if apply:
            con.commit()
            con.execute("begin")
    if apply:
        con.commit()
    else:
        con.rollback()
    return counts


def main(argv=None, out=None):
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="Load the applications Misha sent before the bot knew about them.")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run, writes nothing)")
    ap.add_argument("--file", default=str(SEED), help="seed JSON (default: research/applications_seed.json)")
    ap.add_argument("--db", help="state DB to use (default: store.DB, data/state.db or JOBDIGEST_DB)")
    args = ap.parse_args(argv)
    try:
        rows = load_rows(args.file)
    except (OSError, ValueError) as e:
        print(f"seed file not loaded, nothing done: {e}", file=out)
        return 2
    if args.db:
        store.DB = Path(args.db)
    con = store.connect()
    try:
        print(f"database: {store.DB}", file=out)
        print("mode: APPLY (writing, one transaction per row)" if args.apply
              else "mode: DRY RUN (nothing is written; add --apply to write)", file=out)
        counts = run(con, rows, args.apply, out)
    finally:
        con.close()
    print("done: " + ", ".join(f"{k} {v}" for k, v in counts.items()), file=out)
    if not args.apply:
        print("DRY RUN: nothing was written.", file=out)
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
