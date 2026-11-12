"""Metrics for a date range: the before/after numbers of stage 7 (specs/stage7-plan.md, step 0).

Reads the jobs table and the daily logs (data/logs/YYYY-MM-DD.log, or --logs DIR) and prints one table: days as
columns, metrics as rows, a total column. --md prints the same as a Markdown table with Russian headers.
Read-only: the DB opens with mode=ro, the logs are only read, nothing is written.

Day rules: new jobs, kept jobs, their description and score buckets, junk and closed are counted by first_seen;
sent by sent_at; verdicts by verdict_at. There is no closed_at column, so "closed" is the closed count among the
day's new jobs. A day without a log file shows "н/д" in the log rows and is named in the notes.

CLI: python3 scripts/metrics.py --from 2026-10-10 --to 2026-10-16 [--logs DIR] [--md] [--title TEXT] [--old-days N]
Env: JOBDIGEST_DB overrides the DB path (default data/state.db), as in store.py."""

import argparse
import ast
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import collect  # noqa: E402  the junk rule is collect's own, so the metric and the pipeline agree
import store  # noqa: E402  NOT_MANUAL: applications Misha added by hand are not the bot's funnel
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
OLD_DAYS = 30  # a posting older than this on the day it was sent counts as old (data.posted, YYYY-MM-DD)
MISSING = "н/д"

# enrich.py prints "without description: N, found originals: F, not found: ..." (old format) or
# "found originals: F (page P, direct D, paraphrase R), not found: ..." (since 16.10). The comma parts are read by
# name; "found originals" and its breakdown are read by regex, because their commas sit inside the brackets.
ENRICH_KEYS = {"found page": "found_page", "found paraphrase": "found_paraphrase",
               "not found": "not_found", "closed": "closed"}
ENRICH_PARTS = {"page": "found_page", "direct": "found_direct", "paraphrase": "found_paraphrase"}


def db_path():
    return Path(os.environ.get("JOBDIGEST_DB") or ROOT / "data" / "state.db")


def open_ro(path):
    """mode=ro: the metrics can never change the production copy."""
    if not path.exists():
        raise SystemExit(f"no database at {path}")
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def day_range(start, end):
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def col(row, name):
    """Column value, or None when the DB is older than the column (read-only, no migration here)."""
    return row[name] if name in row.keys() else None


def day_of(ts):
    return ts[:10] if ts else None


def is_junk(row):
    """A LinkedIn footer row, not a vacancy: the same test collect.py uses on the mail text (title or company is
    LinkedIn's own text, e.g. 'See all jobs', 'Edit alert', 'New jobs match your preferences'), or closed_by='junk'."""
    title = col(row, "title") or ""
    company = col(row, "company") or ""
    return (collect.linkedin_service(title) or collect.linkedin_service(company)
            or col(row, "closed_by") == "junk")


def posted_age(row, ref_day):
    """Days between data.posted and the day it was sent; None when the posting has no usable date."""
    try:
        posted = json.loads(col(row, "data") or "{}").get("posted") or ""
        return (date.fromisoformat(ref_day) - date.fromisoformat(posted[:10])).days
    except (ValueError, TypeError, AttributeError):
        return None


def db_rows(con, first, last):
    """Every bot job that first appeared, was sent or got a verdict inside the range. Manual applications
    (source 'manual') are left out: they are Misha's own list, not the bot's funnel."""
    sql = ("select * from jobs where (substr(first_seen,1,10) between ? and ? "
           "or substr(sent_at,1,10) between ? and ? or substr(verdict_at,1,10) between ? and ?)"
           f" and {store.NOT_MANUAL}")
    return con.execute(sql, (first, last) * 3).fetchall()


def db_metrics(rows, days, old_days=OLD_DAYS):
    """m[metric][day] -> count. Kept jobs are rule='keep' (junk included, as the audit counted them)."""
    m = defaultdict(Counter)
    inside = set(days)
    for r in rows:
        fs, sent, ver = day_of(col(r, "first_seen")), day_of(col(r, "sent_at")), day_of(col(r, "verdict_at"))
        if fs in inside:
            m["new"][fs] += 1
            if is_junk(r):
                m["junk"][fs] += 1
            if col(r, "closed"):
                m["closed_new"][fs] += 1
            if col(r, "rule") == "keep":
                m["kept"][fs] += 1
                m[f"src:{col(r, 'source') or '?'}"][fs] += 1
                has_desc = col(r, "has_desc")
                if has_desc in (0, 1, 2):
                    m[f"hd{has_desc}"][fs] += 1
                score = col(r, "score")
                if score is not None:
                    m["score_hi" if score >= 60 else "score_mid" if score >= 50 else "score_lo"][fs] += 1
        if sent in inside:
            m["sent"][sent] += 1
            if not col(r, "listed"):
                m["cards"][sent] += 1
            if col(r, "closed"):
                m["sent_closed"][sent] += 1
            age = posted_age(r, sent)
            if age is not None and age > old_days:
                m["sent_old"][sent] += 1
        if ver in inside:
            m["verdicts"][ver] += 1
    return m


def literal(text):
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None


def parse_log(text):
    """One day's log -> (numbers, failed feed names). A key is present only when its line is in the log,
    so a missing line shows as н/д, not as 0. Repeated lines of one day add up."""
    nums, failed = Counter(), Counter()
    for line in text.splitlines():
        if "without description:" in line:
            m = re.search(r"without description: (\d+)", line)
            if m:
                nums["todo"] += int(m.group(1))
            m = re.search(r"found originals: (\d+)(?: \(([^)]*)\))?", line)
            if m:
                nums["found"] += int(m.group(1))
                for name, val in re.findall(r"(page|direct|paraphrase) (\d+)", m.group(2) or ""):
                    nums[ENRICH_PARTS[name]] += int(val)
            for part in re.sub(r"\([^)]*\)", "", line).split(","):  # brackets dropped: their commas are not parts
                key, sep, val = part.rpartition(":")
                key = key.strip().replace("_", " ").lower()
                if sep and key in ENRICH_KEYS and val.strip().isdigit():
                    nums[ENRICH_KEYS[key]] += int(val)
        m = re.search(r"feeds answered (\d+)", line)
        if m:
            nums["feeds"] += int(m.group(1))
        m = re.search(r"skipped \(no adapter / no feed\): (\d+)", line)
        if m:
            nums["skipped_feeds"] += int(m.group(1))
        m = re.search(r"(?:^|\s)failed: (\[.*\])\s*$", line)
        if m and isinstance(literal(m.group(1)), list):
            names = literal(m.group(1))
            nums["feed_failed"] += len(names)
            for n in names:
                failed[str(n)] += 1
        m = re.search(r"raw per source: (\{.*\})\s*$", line)
        if m and isinstance(literal(m.group(1)), dict):
            raw = literal(m.group(1))
            nums["raw_total"] += sum(int(v) for v in raw.values())
            for k, v in raw.items():
                nums[f"raw:{k}"] += int(v)
    return nums, failed


def log_metrics(logdir, days):
    """day -> (numbers, failed names), or None when there is no log file for that day."""
    res = {}
    for d in days:
        p = Path(logdir) / f"{d}.log"
        res[d] = parse_log(p.read_text(encoding="utf-8", errors="replace")) if p.exists() else None
    return res


def companies_counts(path=ROOT / "companies.json"):
    """(companies with a feed, all companies) from companies.json; None when the file is missing."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return sum(1 for c in data if c.get("feed_ok") and c.get("feed_url")), len(data)


def db_table(m, days, old_days):
    srcs = sorted({k[4:] for k in m if k.startswith("src:")}, key=lambda s: (-sum(m[f"src:{s}"].values()), s))
    spec = [("Новых вакансий (по дню first_seen)", "new"),
            ("Прошли фильтр (rule = keep)", "kept")]
    spec += [(f"↳ источник {s}", f"src:{s}") for s in srcs]
    spec += [("↳ с описанием (has_desc = 1)", "hd1"),
             ("↳ с пересказом (has_desc = 2)", "hd2"),
             ("↳ только заголовок (has_desc = 0)", "hd0"),
             ("↳ оценка 60 и выше", "score_hi"),
             ("↳ оценка 50-59", "score_mid"),
             ("↳ оценка ниже 50", "score_lo"),
             ("Мусор из писем LinkedIn (футеры)", "junk"),
             ("Новых и закрыто (closed = 1)", "closed_new"),
             ("Отправлено, включая строки списка (по дню sent_at)", "sent"),
             ("Карточки, не строки списка (listed = 0, по дню sent_at)", "cards"),
             ("Отправлено закрытых", "sent_closed"),
             (f"Отправлено старых (posted старше {old_days} дн.)", "sent_old"),
             ("Отметок (по дню verdict_at)", "verdicts")]
    return [(label, [m[key][d] for d in days]) for label, key in spec]


def log_table(logs, days):
    have = [logs[d][0] for d in days if logs[d] is not None]

    def values(key):
        return [logs[d][0].get(key) if logs[d] is not None else None for d in days]

    def per_source(src):  # a raw line that exists but does not name a source means 0 from it
        return [(logs[d][0].get(f"raw:{src}", 0) if logs[d] is not None and "raw_total" in logs[d][0] else None)
                for d in days]

    spec = [("Enrich: без описания на входе", "todo", "sum", True),
            ("Enrich: найдено оригиналов", "found", "sum", True),
            ("↳ найдено: страница", "found_page", "sum", False),
            ("↳ найдено: напрямую", "found_direct", "sum", False),
            ("↳ найдено: пересказ", "found_paraphrase", "sum", False),
            ("Enrich: не найдено", "not_found", "sum", True),
            ("Enrich: закрыто по оригиналу", "closed", "sum", True),
            ("Лент ответило (prepare; итого - среднее за день)", "feeds", "avg", True),
            ("Без адаптера или ленты (fetch; итого - среднее за день)", "skipped_feeds", "avg", True),
            ("Сбоев лент (failed)", "feed_failed", "sum", True),
            ("Сырых записей (raw per source)", "raw_total", "sum", True)]
    rows = []
    for label, key, mode, always in spec:
        if always or any(key in h for h in have):
            rows.append((label, values(key), mode))
    srcs = sorted({k[4:] for h in have for k in h if k.startswith("raw:")})
    rows += [(f"↳ сырых {s}", per_source(s), "sum") for s in srcs]
    return rows


def fmt(v):
    return MISSING if v is None else str(v)


def header(days):
    return ["Показатель"] + [f"{d[8:]}.{d[5:7]} {WEEKDAYS[date.fromisoformat(d).weekday()]}" for d in days] + ["Итого"]


def total(vals, mode):
    """Counts add up over the days; a daily snapshot (feeds answered) is averaged instead."""
    got = [v for v in vals if v is not None]
    if not got or mode == "none":
        return None
    return round(sum(got) / len(got)) if mode == "avg" else sum(got)


def body(rows):
    """rows of (label, values[, total mode]) -> rows of strings with the total column added."""
    out = []
    for label, vals, *mode in rows:
        out.append([label] + [fmt(v) for v in vals] + [fmt(total(vals, mode[0] if mode else "sum"))])
    return out


def notes(days, logs, counts):
    out = ["День: новые, отбор, оценки, мусор и закрытые - по first_seen; отправлено - по sent_at; "
           "отметки - по verdict_at. Закрытого времени в базе нет, поэтому закрытые считаются среди новых дня."]
    missing = [d for d in days if logs[d] is None]
    if missing:
        out.append("Нет лога за: " + ", ".join(missing) + " (в итогах логов учтены только дни с логом)")
    if counts is None:
        out.append("companies.json не найден")
    else:
        out.append(f"Компании с лентой (companies.json, feed_ok и feed_url): {counts[0]} из {counts[1]}")
    names = Counter()
    for d in days:
        if logs[d] is not None:
            names.update(logs[d][1])
    if names:
        out.append("Сбои лент за период: " + "; ".join(f"{n} ({c} дн.)" for n, c in names.most_common()))
    return out


def render_md(title, sections, notes_list, days):
    lines = [f"# {title}", ""]
    for name, rows in sections:
        head = header(days)
        lines += [f"## {name}", "", "| " + " | ".join(head) + " |",
                  "|" + "|".join(["---"] + ["---:"] * (len(head) - 1)) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in body(rows)]
        lines.append("")
    lines += [f"- {n}" for n in notes_list]
    return "\n".join(lines) + "\n"


def render_text(title, sections, notes_list, days):
    lines = [title, ""]
    for name, rows in sections:
        table = [header(days)] + body(rows)
        widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
        lines.append(name)
        for r in table:
            lines.append("  ".join(cell.ljust(w) if i == 0 else cell.rjust(w)
                                   for i, (cell, w) in enumerate(zip(r, widths))))
        lines.append("")
    lines += notes_list
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Job-digest metrics per day for a date range (read-only).")
    ap.add_argument("--from", dest="start", required=True, help="first day, YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="last day, YYYY-MM-DD (inclusive)")
    ap.add_argument("--logs", default=str(ROOT / "data" / "logs"), help="folder with YYYY-MM-DD.log files")
    ap.add_argument("--md", action="store_true", help="print a Markdown table with Russian headers")
    ap.add_argument("--title", default=None, help="title line (default: the date range)")
    ap.add_argument("--old-days", type=int, default=OLD_DAYS, help="age in days that counts as old")
    args = ap.parse_args(argv)
    try:
        days = day_range(args.start, args.end)
    except ValueError:
        ap.error("dates must be YYYY-MM-DD")
    if args.start > args.end:
        ap.error("--from is after --to")

    con = open_ro(db_path())
    try:
        rows = db_rows(con, days[0], days[-1])
    finally:
        con.close()
    m = db_metrics(rows, days, args.old_days)
    logs = log_metrics(args.logs, days)
    sections = [("База (jobs)", db_table(m, days, args.old_days)), ("Логи", log_table(logs, days))]
    counts = companies_counts(ROOT / "companies.json")
    title = args.title or f"Метрики job-digest: {days[0]} - {days[-1]}"
    render = render_md if args.md else render_text
    print(render(title, sections, notes(days, logs, counts), days), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
