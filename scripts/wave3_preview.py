"""Wave 3 preview (stage 7, 26.10.2026): which jobs the new title rules add, before Misha switches groups on.

Opens the state DB read-only and takes the jobs first seen in the last N days, counted back from the newest
first_seen in the DB. Each job is rebuilt from its stored data (title, description) and its verdict is recomputed
with the current prefilter rules. Prints Markdown:
  Добавится - stored rule not 'keep', now 'keep', grouped by the rule that keeps it;
  one line counting kept rows whose description now shows a required German level. Not acted on: the bot never
  re-judges a kept row; the full list goes to the JSON under german_seen_after_keep.
--out PATH also writes PATH.md and PATH.json. Usage: python3 scripts/wave3_preview.py --db PATH [--days 15] [--out PATH]"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prefilter  # noqa: E402

# Credit order of the rules, as pass_reason() checks them.
REASON_ORDER = ["target", "ai_edge", "graduate", *prefilter.ROLE_GROUPS]


def connect(db, write=False):
    """Read-only unless write=True (apply_wave3.py). mode=rw never creates a missing file."""
    con = sqlite3.connect(Path(db).resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)
    con.row_factory = sqlite3.Row
    return con


def window_since(con, days):
    """(newest first_seen, since): the window counted back from the newest first_seen in the DB, (None, None) if empty."""
    newest = con.execute("select max(first_seen) from jobs").fetchone()[0]
    if not newest:
        return None, None
    return newest, (datetime.fromisoformat(newest) - timedelta(days=days)).isoformat(timespec="seconds")


def window(con, days):
    """(newest, since, rows) for the jobs first seen inside the window."""
    newest, since = window_since(con, days)
    if since is None:
        return None, None, []
    rows = con.execute("select id, title, company, source, first_seen, rule, data from jobs"
                       " where first_seen >= ? order by first_seen desc", (since,)).fetchall()
    return newest, since, rows


def job_of(row):
    """The job as the morning run saw it: title and description from the stored data JSON, and the stored rule."""
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    job = {"title": data.get("title") or row["title"] or "", "description": data.get("description") or ""}
    return job, row["rule"] or data.get("rule") or ""


def compare(rows):
    """(added, german_seen): added = stored not 'keep', now 'keep'; german_seen = stored 'keep', now german_required."""
    added, german_seen = [], []
    for row in rows:
        job, stored = job_of(row)
        new = prefilter.verdict(job)
        item = {"id": row["id"], "title": job["title"], "company": row["company"] or "",
                "source": row["source"] or "", "first_seen": row["first_seen"] or "",
                "stored_rule": stored, "verdict": new}
        if stored != "keep" and new == "keep":
            item["reason"] = prefilter.reason(job)
            added.append(item)
        elif stored == "keep" and new == "german_required":
            item["phrase"] = prefilter.german_phrase(job["description"])
            german_seen.append(item)
    return added, german_seen


def cell(text):
    """One Markdown table cell: no line breaks, pipes escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")


def markdown(newest, since, rows, added, german_seen):
    kept_now = sum(1 for r in rows if (r["rule"] or "") == "keep")
    lines = ["# Волна 3: что изменит новый фильтр", "",
             f"Самый свежий показ в базе: {newest.replace('T', ' ')}. Окно: с {since.replace('T', ' ')} "
             f"(строк: {len(rows)}).",
             f"Прошло фильтр сейчас: {kept_now}.", "",
             f"## Добавится: {len(added)}", ""]
    by_reason = {}
    for item in added:
        by_reason.setdefault(item["reason"], []).append(item)
    for name in sorted(by_reason, key=lambda n: REASON_ORDER.index(n) if n in REASON_ORDER else len(REASON_ORDER)):
        items = by_reason[name]
        lines += [f"### {name} ({len(items)})", "", "| Название | Компания | Источник | Первый показ |",
                  "|---|---|---|---|"]
        lines += [f"| {cell(i['title'])} | {cell(i['company'])} | {cell(i['source'])} | "
                  f"{cell(i['first_seen'].replace('T', ' ')[:16])} |" for i in items]
        lines.append("")
    lines += [f"Строк, где в описании теперь видно требование немецкого: {len(german_seen)} "
              "(на работу бота не влияет: правило применяется только к новым вакансиям)", ""]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True, help="state DB copy to read (opened read-only)")
    ap.add_argument("--days", type=int, default=15, help="window in days back from the newest first_seen")
    ap.add_argument("--out", help="also write PATH.md and PATH.json (default: stdout only)")
    args = ap.parse_args(argv)

    con = connect(args.db)
    newest, since, rows = window(con, args.days)
    if not rows:
        print("В базе нет показов за окно.")
        return
    added, german_seen = compare(rows)
    md = markdown(newest, since, rows, added, german_seen)
    print(md)
    if args.out:
        out = Path(args.out)
        base = out.with_suffix("") if out.suffix in (".md", ".json") else out
        base.with_suffix(".md").write_text(md, encoding="utf-8")
        by_reason = {}
        for item in added:
            by_reason.setdefault(item["reason"], []).append(item)
        payload = {"newest": newest, "since": since, "rows_in_window": len(rows),
                   "added": by_reason, "german_seen_after_keep": german_seen}
        base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
