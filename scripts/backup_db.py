"""Daily copy of data/state.db (verdicts, application statuses, CV versions, notes) into
data/backups/state-YYYY-MM-DD.db, kept for 14 days (data-7, ops-16). Uses sqlite's online backup,
so the copy is consistent while the bot and cron write to the live file. Run by run.sh send after
the digest; safe to run by hand (a second run the same day replaces that day's copy).
Restore: stop the bot, cp data/backups/state-<day>.db data/state.db, start the bot."""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store  # noqa: E402

KEEP_DAYS = 14


def backup(db=None, folder=None, today=None, keep=KEEP_DAYS):
    """Write today's copy, check it opens and passes integrity_check, drop copies older than `keep` days.
    Returns the path written."""
    db = Path(db or store.DB)
    folder = Path(folder or db.parent / "backups")
    today = today or date.today()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"state-{today.isoformat()}.db"
    tmp = target.with_suffix(".tmp")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(tmp)
    try:
        ok = check.execute("pragma integrity_check").fetchone()[0]
    finally:
        check.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity_check: {ok}")
    tmp.replace(target)
    cutoff = today - timedelta(days=keep)
    for old in folder.glob("state-*.db"):
        try:
            day = date.fromisoformat(old.stem.removeprefix("state-"))
        except ValueError:
            continue
        if day <= cutoff:  # keeps exactly `keep` daily copies, today included
            old.unlink()
    return target


if __name__ == "__main__":
    path = backup()
    print(f"backup written: {path} ({path.stat().st_size // 1024} KB)")
