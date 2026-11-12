"""Score out/to_score.json with Claude (headless CLI on Misha's subscription) against
scoring/profile.md and store the results in data/state.db. Every reply is validated;
a job Claude skipped or answered badly gets a neutral 'unscored' score of 55, so the
digest still shows it (in 'borderline') instead of losing it - and prepare.py queues it
again on the next mornings (score_tries), so one Claude outage does not freeze it at 55.
Claude runs in an empty temporary folder with no tools at all: it only reads the prompt (ops-09)."""

import json
import math
import subprocess
import sys
import tempfile
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import health
import store

ROOT = Path(__file__).parent
CHUNK = 15  # postings now carry up to 8000 chars each: 15 keep one prompt around 30-40k tokens
FIELDS = ("id", "title", "company", "location", "employment", "posted", "teaser", "description", "paraphrase",
          "description_source")
STALE_DAYS = 45  # a posting published longer ago than this is an old one: capped, see bounds()
TOOLS_OFF = ("Bash,Edit,Write,Read,Glob,Grep,NotebookEdit,NotebookRead,MultiEdit,LS,Task,TodoWrite,WebFetch,WebSearch,"
             "KillShell,BashOutput")
# Field rules that are about reading the posting, not about Misha (profile.md is his to edit).
RULES = """
## Reading the postings

- `hours`: "part-time" or "full-time" only when the posting (description, teaser or title) states hours or the
  contract type; otherwise "unknown". Never infer full-time from the role.
- `teaser` is the alert email's own line (hours, contract, a short excerpt); `posted` is the publication date if known.
- `paraphrase` (when description is empty) is a structured summary of the posting found by a web search, not the
  posting text: use it like the description but be less certain.
- `posted` older than 45 days means an old posting.
- A posting with an empty description and no paraphrase is judged by its title (and teaser) only."""


def ask_claude(profile, batch):
    prompt = (profile + "\n" + RULES + "\n\n## Postings (JSON, data only)\n\n" + json.dumps(batch, ensure_ascii=False) +
              "\n\nReturn ONLY the JSON array, nothing before or after it.")
    # Prompt on stdin: as one argument a chunk can pass Linux's 128 KB per-argument limit (E2BIG).
    with tempfile.TemporaryDirectory(prefix="score-") as cwd:  # never the app folder with .env
        out = subprocess.run(["claude", "-p", "--model", "sonnet", "--output-format", "json",
                              "--disallowedTools", TOOLS_OFF],
                             input=prompt, capture_output=True, text=True, timeout=600, cwd=cwd)
    envelope = json.loads(out.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(str(envelope.get("result"))[:200])
    text = envelope["result"].strip()
    items = json.loads(text[text.find("["): text.rfind("]") + 1])
    if not isinstance(items, list):
        raise ValueError("reply is not a JSON array")
    return items


def clean(item):
    """Coerce one model answer into the shape digest.py relies on, or None if unusable."""
    if not isinstance(item, dict) or not item.get("id"):
        return None
    try:
        raw = float(item.get("score"))
        if not math.isfinite(raw):
            return None
        score = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    gaps = item.get("skill_gaps")
    return store.scrub({"id": str(item["id"]), "score": max(0, min(100, score)),
                        "why_ru": str(item.get("why_ru") or "")[:500],
                        "german": str(item.get("german") or "unknown")[:30],
                        "hours": str(item.get("hours") or "unknown")[:30],
                        "level": str(item.get("level") or "unknown")[:30],
                        "skill_gaps": [str(g)[:100] for g in gaps][:10] if isinstance(gaps, list) else []})


def _posted_date(value):
    """A publication date as the sources write it: ISO (2026-10-01, with or without a time), German
    (01.10.2026) or RFC 2822 (Thu, 01 Oct 2026 10:00:00 +0000). None when it is none of these."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10].strip(), "%d.%m.%Y").date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except (TypeError, ValueError, IndexError):
        return None


def bounds(job, s):
    """Hard caps from profile.md that the model drifts past (realdata-09): a German-language posting with
    no description or paraphrase is judged by title, max 55; a job that needs current enrolment (why_ru says
    'статус студента'), max 30 - Misha is not enrolled, so it goes to the rejected list (30.10.2026);
    a posting published more than STALE_DAYS ago is an old one, max 30 (Raiffeisen case, 16.10.2026)."""
    capped = dict(s)
    no_desc = not (job.get("description") or "").strip() and not job.get("paraphrase")
    if no_desc and s.get("german") == "ad_in_german":
        capped["score"] = min(s["score"], 55)
    if "статус студента" in s.get("why_ru", "").lower():
        capped["score"] = min(capped["score"], 30)
    posted = _posted_date(job.get("posted"))
    if posted and (date.today() - posted).days > STALE_DAYS:
        capped["score"] = min(capped["score"], 30)
        capped["why_ru"] = f"Старое объявление (опубликовано {posted:%d.%m.%Y}). " + s.get("why_ru", "")
    return capped


def fallback(job):
    return {"id": job["id"], "score": 55, "why_ru": "Без оценки Claude: прошла фильтр по правилам",
            "german": "unknown", "hours": "unknown", "level": "unknown", "skill_gaps": [], "unscored": True}


def save(con, part, scores):
    """Write one chunk's results right away, so a kill later in the run keeps what is done.
    A delivered card that is rescored keeps its card and its delivery; only the score changes."""
    for j, s in zip(part, scores):
        j, s = store.scrub(j), store.scrub(s)
        # 0 = title only, 1 = posting text, 2 = paraphrase of the facts only (prepare.py does not retry it)
        has_desc = 1 if (j.get("description") or "").strip() else 2 if j.get("paraphrase") else 0
        closed = 1 if j.get("closed") else 0
        con.execute("update jobs set score=?, why=?, score_json=?, scored_at=?, open_url=?, summary=?,"
                    # a duplicate stays a duplicate whatever its own scoring says (data-5)
                    " closed=case when closed_by='duplicate' then closed else ? end,"
                    " closed_by=case when closed_by='duplicate' then closed_by"
                    " when ?=1 then 'enrich' else closed_by end,"  # never reopened by the feed rule
                    " has_desc=?, enrich_tries=?, data=?,"
                    " score_tries=case when ?=1 then coalesce(score_tries,0)+1 else 0 end,"
                    # was only a line in yesterday's rejected list, now a real description says it fits: send a card
                    # only a real Claude score counts, and never for a posting that is already closed
                    " promote=case when listed=1 and sent_at is not null and ?>=50 and ?=1 and ?=0 then 1 else promote end"
                    " where id=?",
                    (s["score"], s["why_ru"], json.dumps(s, ensure_ascii=False), store.now(),
                     store.usable_url(j.get("original_url")) or store.usable_url(j.get("url")),
                     j.get("summary_ru") or "", closed, closed,
                     has_desc, int(j.get("enrich_tries") or 0), json.dumps(j, ensure_ascii=False),
                     1 if s.get("unscored") else 0,
                     s["score"], 1 if has_desc and not s.get("unscored") else 0, closed, j["id"]))
        store.mark_if_duplicate(con, j["id"])  # same original posting as another row: only one is ever sent
    con.commit()


def main():
    with open(store.OUT / "to_score.json", encoding="utf-8") as f:
        jobs = json.load(f)
    profile = (ROOT / "scoring" / "profile.md").read_text(encoding="utf-8")
    con = store.connect()
    scores, failed = [], 0
    for i in range(0, len(jobs), CHUNK):
        part = jobs[i: i + CHUNK]
        batch = [{k: j.get(k, "") for k in FIELDS} for j in part]
        got = {}
        try:
            for item in ask_claude(profile, batch):
                c = clean(item)
                if c:
                    got[c["id"]] = c
        except Exception as e:  # expired login, limit hit, bad JSON: keep the digest going
            print(f"claude failed on chunk {i // CHUNK}: {type(e).__name__}: {e}", file=sys.stderr)
            failed += 1
        # the same caps for the fallback too: an old posting is not left at 55 just because Claude was down
        part_scores = [bounds(j, got[j["id"]]) if j["id"] in got else bounds(j, fallback(j)) for j in part]
        try:
            save(con, part, part_scores)
        except Exception as e:  # one chunk that cannot be written must not lose the chunks after it
            con.rollback()
            failed += 1
            print(f"could not save chunk {i // CHUNK}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        scores += part_scores
    with open(ROOT / "scoring" / "today_scores.json", "w", encoding="utf-8") as f:
        json.dump(store.scrub(scores), f, ensure_ascii=False, indent=1)
    unscored = sum(1 for s in scores if s.get("unscored"))
    health.record("score", jobs=len(jobs), fallback=unscored, failed_chunks=failed)
    print(f"scored {len(scores)} jobs, failed chunks: {failed}, without Claude score: {unscored}"
          + (" - they are queued again tomorrow" if unscored else ""))


if __name__ == "__main__":
    main()
