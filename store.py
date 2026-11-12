"""SQLite state: every vacancy ever seen, its rule verdict and score, what was delivered,
and Misha's button presses. Delivery and scoring state live here (not in the day's JSON
files), so a failed or interrupted morning is picked up by the next run."""

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
# Overridable so tests run on a copy of the server DB and never touch data/ or out/.
DB = Path(os.environ.get("JOBDIGEST_DB") or ROOT / "data" / "state.db")
OUT = Path(os.environ.get("JOBDIGEST_OUT") or ROOT / "out")
WINDOW_DAYS = 15  # an unmarked vacancy stays available this long after it was first seen (Misha, 08.10.2026:
                  # 15, so a job first seen exactly 14 days ago at any time of day is still in)
KEY_VERSION = 3  # bump when dedupe_key changes; connect() re-keys existing rows
                 # 3 (09.10.2026): ß, soft hyphens, (w|m|d), more legal forms, non-Latin titles, empty company

SCHEMA = """
create table if not exists jobs (
  id text primary key,          -- short hash, used in button callback data; never changes once assigned
  key text unique,              -- normalized company + title, the dedupe key
  company text, title text, location text, url text, source text,
  first_seen text, sent_at text, score integer, why text,
  verdict text, reasons text default '[]', verdict_at text,
  data text
);
create table if not exists notes (   -- free text Misha writes to the bot (score corrections, wishes)
  at text, text text
);
create table if not exists meta (k text primary key, v text);
create table if not exists company_feed (  -- last morning run whose feed for this company returned >= 1 job
  company_id text primary key, last_ok text
);
create table if not exists status_log (  -- every application-status change, never edited: the history for /report
  job_id text, status text, at text      -- status 'reset' = Misha pressed 'Сбросить'
);
create table if not exists cv_versions (  -- every tailored CV (stage 5), never edited: which CV went to which job
  id integer primary key, job_id text, created_at text, pdf_path text,
  content text, changes text, gaps text, reverted text,  -- JSON: the CV as rendered, change log, gaps, reverted lines
  coverage_before real, coverage_after real,             -- share of the ad's key terms in the base / tailored CV
  status text                                            -- 'ok' or 'fallback' (base CV sent, see result.json)
);
create table if not exists letters (  -- every cover letter (stage 6), never edited: which letter was written for which job
  id integer primary key autoincrement, job_id text not null, created_at text not null,
  path text not null, status text not null, hook_source text  -- path: letter.txt; hook_source 'model' or 'template'
);
create index if not exists letters_job on letters(job_id);
create table if not exists cv_requests (  -- 'Адаптировать резюме' on a job without an ad text: the bot asked for it;
  job_id text primary key, asked_msg_id integer, at text  -- a reply to asked_msg_id starts the CV worker with that text
);
create table if not exists manual_requests (  -- /add (stage 8): the bot asked for a line; a reply to asked_msg_id is that line
  asked_msg_id integer primary key, kind text, token text, at text  -- kind: new | date | edit
);
create table if not exists manual_drafts (  -- /add confirmation not saved yet; the bot drops it after 24 h
  token text primary key, chat text, msg_id integer, created_at text, json text
);
create table if not exists liveness_checks (  -- stage 9: every weekly check of an interested vacancy (liveness.py)
  id integer primary key autoincrement, job_id text not null, checked_at text not null,
  result text not null,  -- open | closed | unknown | skipped
  reason text, url text
);
create index if not exists liveness_checks_at on liveness_checks(checked_at);
"""

# Columns added after the first deploy; added on connect if missing.
EXTRA_COLUMNS = {
    "msg": "text",          # message text as sent, so 'Изменить' can restore it
    "card_msg_id": "integer",  # Telegram id of the job's card, recorded on a press there (stage 5: refresh it from the CV message)
    "open_url": "text",     # best link to open: the original posting if enrich found it
    "summary": "text",      # short Russian summary of the posting found by enrich
    "closed": "integer default 0",
    "score_json": "text",   # the full scoring object from Claude
    "scored_at": "text",
    "rule": "text",         # prefilter verdict: keep / senior / excluded_field / ...
    "listed": "integer default 0",  # 1 = delivered only as a line in the 'rejected' list (no buttons)
    "has_desc": "integer default 1",  # 0 = title only (enrich retries it), 1 = posting text, 2 = paraphrase only (not retried)
    "enrich_tries": "integer default 0",
    "promote": "integer default 0",   # 1 = was only listed, a later description lifted it to >= 50: send as a card
    "last_seen": "text",    # latest morning run whose prefiltered.json listed the job
    "vitrina_at": "text",   # last time the bot re-sent it from the витрина
    "closed_by": "text",    # 'feed' = the company stopped listing it (reopened if it comes back) / 'enrich' = the original says closed
    "company_id": "text",   # companies.json id for company-feed jobs: links the row to its feed's last answer
    "status": "text",       # application status, only for verdict 'interested' (see STATUSES); null = not applied yet
    "status_at": "text",    # last status change
    "applied_at": "text",   # when Misha pressed 'Отправил резюме'
    "score_tries": "integer default 0",  # mornings Claude failed to score it (fallback 55); rescored until 3
    "dup_of": "text",       # closed_by='duplicate': id of the row it duplicates (shown once in the closed list)
    "applied_cv": "integer",  # cv_versions.id of the CV Misha sent with 'Отправил резюме' (bot.py, data-13)
}
NOTE_COLUMNS = {"reply_to": "text"}
STATUS_LOG_COLUMNS = {"cv_version": "integer"}  # which CV version a status change belongs to (bot.py)

LEGAL = (r"\b(gmbh|ag|kg|og|e\.?u\.?|ltd|inc|se|co|& co|group|gruppe|holding|austria|osterreich|international"
         r"|consulting|corporation|corp|llc|plc|limited)\b")  # NAVAX / NAVAX Consulting (realdata-04)
GENDER = (r"\((m/w/d|w/m/d|m/w/x|w/m/x|f/m/d|m/f/d|f/m/x|m/f/x|d/f/m|all genders|w/m/div|m/w/div|gn|d/w/m)\)"
          r"|\((?:[mwfdx]|div)(?:\s*[/|,]\s*(?:[mwfdx]|div)){1,3}\)"  # (w|m|d), (m, w, d)
          r"|:in\b|\*in\b|_in\b|\binnen\b|\s+-\s+id:\s*\d+")
SURROGATE = re.compile("[\ud800-\udfff]")


def scrub(obj):
    """Lone UTF-16 surrogates (a JS-truncated emoji in a feed, security-01) cannot be written as UTF-8 to a file
    or to sqlite: replace them with U+FFFD in every string of a job, recursively. Valid emoji are untouched."""
    if isinstance(obj, str):
        return SURROGATE.sub("\ufffd", obj)
    if isinstance(obj, dict):
        return {scrub(k): scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(x) for x in obj]
    return obj


def _clean(s):
    """Lowercase, no soft hyphens / private-use glyphs (Adzuna's U+E4BB), ß = ss, accents dropped."""
    s = "".join(c for c in (s or "") if unicodedata.category(c) not in ("Cf", "Co", "Cs"))
    s = unicodedata.normalize("NFKD", s.lower().replace("ß", "ss"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _norm(s, pattern):
    s = re.sub(pattern, " ", _clean(s))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def dedupe_key(job):
    """Same vacancy from LinkedIn, Adzuna and the company feed -> same key:
    legal forms, gender markers, umlauts and punctuation are ignored. A title with no Latin letters keeps its
    own letters (otherwise all of them would share one key), and an ad with no company is keyed by its site."""
    company = re.split(r"\s+[\(\[]", job.get("company") or "")[0]
    c = _norm(company, LEGAL) or re.sub(r"[\W_]+", " ", _clean(company)).strip()
    if not c:  # 'no company' is not a company: two employer-less ads are two jobs unless their link is the same
        c = "?" + (same_link(job.get("url")) or (job.get("url") or "").strip().lower())
    title = _norm(job.get("title"), GENDER)
    if not title:
        title = re.sub(r"[\W_]+", " ", re.sub(GENDER, " ", _clean(job.get("title")))).strip()
    return c + "|" + title


def job_id(key):
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def now():
    return datetime.now().isoformat(timespec="seconds")


def in_window(closed_ok=False):
    """SQL condition + params for 'still worth showing': not judged, not closed, and either first seen
    in the last WINDOW_DAYS days or a company-feed job that is still live: the latest morning run saw
    it in the feed, or the company's feed did not answer / returned 0 jobs that morning (a failed feed
    says nothing about its jobs, so they keep their place; a job leaves only when the company answered
    without it - then prepare closes it). A feed silent for more than WINDOW_DAYS (or never answered)
    no longer holds its jobs: they fall back to the plain first_seen rule. One definition shared by prepare (scoring queue), digest and
    bot (витрина). closed_ok: digest still lists scored-but-closed jobs once in its closed list -
    duplicates of another row too, so no job ever disappears without a line (data-1)."""
    since = (datetime.now() - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    last_run = "coalesce((select v from meta where k='last_run'), '')"
    return ("(verdict is null" + ("" if closed_ok else " and coalesce(closed,0)=0") +
            " and (first_seen >= ? or (source='company'"
            f" and (last_seen >= {last_run}"
            " or (coalesce(closed,0)=0 and coalesce((select last_ok from company_feed f"
            f" where f.company_id=jobs.company_id), '') < {last_run}"
            " and (select last_ok from company_feed f where f.company_id=jobs.company_id) >= ?)))))", [since, since])


FRESH_HOURS = 20  # a morning file older than this is yesterday's, not today's answer (pipeline-12)


def fresh_out(name, hours=FRESH_HOURS):
    """True if OUT/name was written in the last `hours` - e.g. company_jobs.json by today's fetch_companies.py."""
    try:
        return (datetime.now().timestamp() - (OUT / name).stat().st_mtime) < hours * 3600
    except OSError:
        return False


def score_of(row):
    """The stored Claude score object, or None if it has no integer score. One filter for what can be shown
    and what is counted (digest cards, the витрина line, bot 'Осталось', /stats), so the numbers match."""
    try:
        score = json.loads(row["score_json"] or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(score, dict) or not isinstance(score.get("score"), int) or isinstance(score["score"], bool):
        return None
    return score


def vitrina(con):
    """In-window jobs already delivered as a card that Misha has not marked yet, the next to re-send first."""
    cond, params = in_window()
    # A card re-scored below 50 later (a fallback 55 that Claude scored properly) stays sent but is not re-sent.
    rows = con.execute(f"select * from jobs where {cond} and sent_at is not null and coalesce(listed,0)=0"
                       " and coalesce(score,0) >= 50"
                       " order by vitrina_at is not null, vitrina_at, score desc", params).fetchall()
    return [r for r in rows if score_of(r)]  # a broken score can't be shown, so it is not counted either


TRACKING = re.compile(r"^(utm_.*|feedid|se|v|refid|trackingid|trk|gclid|fbclid)$", re.I)


# What makes a link point at ONE posting: a long number (karriere.at, SuccessFactors, Personio, Greenhouse
# gh_jid, LinkedIn, derStandard, Teamtailor, join.com ...), a UUID (Lever, Ashby), or a known ATS shape
# (Workable /j/<code>, Recruitee /o/<slug>, TalentLyft /jobs/<slug>-<code>). Listing, careers and API pages
# (/karriere/jobs, /ei/services/getJobOffers/stepstone) have none of these and never identify a job (data-1).
JOB_NUMBER = re.compile(r"(?<!\d)\d{5,}(?!\d)")
ID_PARAM = re.compile(r"id|job|ref|req|vacanc|position", re.I)  # query keys that can carry the job number
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
ATS_SHAPES = re.compile(r"/j/[0-9a-z]{6,}(/|$)|/o/[0-9a-z-]{3,}(/|$)|talentlyft\.com/jobs/[0-9a-z-]+-[0-9a-z]{4}$", re.I)


def _job_parts(url):
    """(host, path, kept query pairs) of an http(s) link, or None."""
    from urllib.parse import parse_qsl, urlsplit
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None
    if not host:
        return None
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k))
    return host, parts.path.rstrip("/"), query


def link_id(url):
    """'host#number' when the link carries a job number, else ''. The number stays when an employer renames
    the posting (UNIDO 'Intern (Climate ...)' -> 'Intern (A2D Facility)' on the same /1370852755/, data-8)."""
    p = _job_parts(url)
    if not p:
        return ""
    host, path, query = p
    uuid = UUID.findall(path)
    if uuid:
        return f"{host}#{uuid[-1].lower()}"
    qid = [v for k, v in query if ID_PARAM.search(k) and UUID.fullmatch(v)]  # BingX: ?jobId=<uuid>
    if qid:
        return f"{host}#{qid[-1].lower()}"
    gh = [v for k, v in query if ID_PARAM.search(k) and JOB_NUMBER.fullmatch(v)]
    nums = gh or JOB_NUMBER.findall(path.split("/")[-1] if JOB_NUMBER.search(path.split("/")[-1]) else path)
    return f"{host}#{nums[-1]}" if nums else ""


def same_link(url):
    """Normalized link for 'same link = same job' (Misha, 08.10.2026), or '' when a link says nothing
    about which job it is: anything without a job number, UUID or known ATS job shape (listing, careers
    or API pages, see JOB_NUMBER). Drops tracking parameters, fragment and trailing slash, but keeps
    parameters that carry the job id: Greenhouse on company sites (www.gostudent.org/en-gb/job-position/?gh_jid=...)."""
    from urllib.parse import urlencode
    p = _job_parts(url)
    if not p:
        return ""
    host, path, query = p
    if len([seg for seg in path.split("/") if seg]) < 1:
        return ""
    ident = path + "?" + "&".join(v for k, v in query if ID_PARAM.search(k))
    if not (JOB_NUMBER.search(ident) or UUID.search(ident) or ATS_SHAPES.search(host + path)):
        return ""
    q = urlencode(query)
    return (host + path + ("?" + q if q else "")).lower()


def usable_url(url):
    """A link Misha can open: well-formed http(s), not a StepStone tracking link carrying his search profile."""
    from urllib.parse import quote, urlsplit
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")) or "click.stepstone" in url or len(url) > 1000:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.netloc or " " in parts.netloc or not parts.hostname or "." not in parts.hostname:
        return ""  # the button needs a real host: 'https://:80/x' or 'https://localhost' is not a posting
    return quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")  # e.g. spaces in a path Claude reported


# Application chain agreed with Misha 08.10.2026: Интересно -> Отправил резюме -> Ответили -> Интервью ->
# Оффер / Отказ / Нет ответа. Kept apart from the verdict, which stays the meaning of the card button.
STATUSES = ("applied", "replied", "interview", "offer", "rejected", "no_answer")
STAGE = {"applied": 1, "replied": 2, "interview": 3, "offer": 4}  # how far along the funnel a status is


def set_status(con, job_id, status):
    """Set the application status (None = 'Сбросить') and append it to status_log. Pressing the status a job
    already has is not a change: nothing is written, so the history and 'last change' stay true.
    applied_at keeps the first 'Отправил резюме' through later corrections; a reset clears it. Returns True if changed."""
    row = con.execute("select status from jobs where id=?", (job_id,)).fetchone()
    if not row or row["status"] == status:
        return False
    t = now()
    applied = {"applied": "coalesce(applied_at, :t)", None: "null"}.get(status, "applied_at")
    con.execute(f"update jobs set status=:s, status_at=:t, applied_at={applied} where id=:id",
                {"s": status, "t": t, "id": job_id})
    con.execute("insert into status_log (job_id, status, at) values (?, ?, ?)", (job_id, status or "reset", t))
    con.commit()
    return True


def stages(con):
    """{job id: furthest funnel stage} for interested jobs with a status. Отказ / Нет ответа count as far as the
    job had got before them since the last reset (at least 'Отправил'), so a refusal after an interview still
    counts as a reply and an interview. One definition for /stats, the report and the Sunday summary."""
    rows = con.execute("select id, status from jobs where verdict='interested' and status is not null").fetchall()
    history = {}
    for r in con.execute("select job_id, status from status_log order by at, rowid"):
        history[r[0]] = [] if r[1] == "reset" else history.get(r[0], []) + [r[1]]
    out = {}
    for r in rows:
        if r["status"] in STAGE:
            out[r["id"]] = STAGE[r["status"]]
        else:
            out[r["id"]] = max([1] + [STAGE.get(s, 0) for s in history.get(r["id"], [])])
    return out


def connect():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, extra in (("jobs", EXTRA_COLUMNS), ("notes", NOTE_COLUMNS), ("status_log", STATUS_LOG_COLUMNS)):
        cols = {r[1] for r in con.execute(f"pragma table_info({table})")}
        for name, kind in extra.items():
            if name not in cols:
                try:
                    con.execute(f"alter table {table} add column {name} {kind}")
                except sqlite3.OperationalError as e:  # cron and the bot started together after a deploy (data-3)
                    if "duplicate column" not in str(e):
                        raise
    row = con.execute("select v from meta where k='key_version'").fetchone()
    if not row or int(row[0]) < KEY_VERSION:
        rekey(con)
        con.execute("insert or replace into meta (k, v) values ('key_version', ?)", (str(KEY_VERSION),))
    backfill(con)
    con.commit()
    return con


def backfill(con):
    """Fill the 08.10 columns on older rows: company_id from the stored job, and why a closed row was closed -
    a company-feed row without enrich's 'closed' flag was closed by the feed rule, anything else by enrich.
    Only rows still empty are read, so after the first run this is a quick no-op. Rows that got the fallback
    score before score_tries existed count as one failed morning, so they are rescored (pipeline-02)."""
    con.execute("""update jobs set score_tries=1 where coalesce(score_tries,0)=0 and score_json like '%"unscored": true%'""")
    for r in con.execute("select id, source, data, company_id, closed, closed_by from jobs"
                         " where (source='company' and company_id is null) or (closed=1 and closed_by is null)").fetchall():
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        if r["source"] == "company" and r["company_id"] is None:
            con.execute("update jobs set company_id=? where id=?", (str(d.get("company_id") or ""), r["id"]))
        if r["closed"] == 1 and r["closed_by"] is None:
            by = "feed" if r["source"] == "company" and not d.get("closed") else "enrich"
            con.execute("update jobs set closed_by=? where id=?", (by, r["id"]))


def rekey(con):
    """Recompute dedupe keys with the current rule; ids stay, so buttons in old messages keep working.
    Two old rows that now share a key keep the first; the other keeps its old key (harmless)."""
    for r in con.execute("select id, company, title from jobs where coalesce(closed_by,'') != 'manual_removed'"
                         " order by first_seen").fetchall():
        try:
            con.execute("update jobs set key=? where id=?", (dedupe_key(dict(r)), r["id"]))
        except sqlite3.IntegrityError:
            pass


def find_same_link(con, url):
    """Id of an existing row whose link is the same job as `url` (see same_link), or carries the same job
    number on the same site (link_id: a renamed posting), else None."""
    link = same_link(url)
    if not link:
        return None
    from urllib.parse import urlsplit
    path = urlsplit(url.strip()).path.rstrip("/")
    for r in con.execute("select id, url from jobs where instr(lower(url), ?) > 0"
                         " and coalesce(closed_by,'') != 'manual_removed' order by first_seen",
                         (path.lower(),)).fetchall():
        if same_link(r["url"]) == link:
            return r["id"]
    lid = link_id(url)
    if lid:
        for r in con.execute("select id, url from jobs where instr(lower(url), ?) > 0"
                             " and coalesce(closed_by,'') != 'manual_removed' order by first_seen",
                             (lid.split("#", 1)[1],)).fetchall():
            if link_id(r["url"]) == lid:
                return r["id"]
    return None


def similar_titles(a, b):
    """Two titles name the same job: one contains the other, or they share at least half of their words.
    A same-link match between two different titles is an enrich mistake or an injected link, not a duplicate
    (security-07): 'Junior PM Energy' never hides 'Data Engineer' because both got the same page."""
    x, y = _norm(a, GENDER), _norm(b, GENDER)
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    wx, wy = set(x.split()), set(y.split())
    return len(wx & wy) / len(wx | wy) >= 0.5


def title_part(title):
    """The title half of dedupe_key, computed the same way. A separate copy, so dedupe_key and every stored key
    stay exactly as they are; the company rule of mark_if_duplicate compares these."""
    t = _norm(title, GENDER)
    return t or re.sub(r"[\W_]+", " ", re.sub(GENDER, " ", _clean(title))).strip()


def company_part(company):
    """The company half of dedupe_key: legal forms, brackets and umlauts dropped, so 'CHECK24 Vergleichsportal
    Österreich' becomes 'check24 vergleichsportal'."""
    name = re.split(r"\s+[\(\[]", company or "")[0]
    return _norm(name, LEGAL) or re.sub(r"[\W_]+", " ", _clean(name)).strip()


# Words too common to name one employer on their own ('Bank Austria' is not every bank), and group brands that
# independent companies share ('Raiffeisen Bank Steiermark' is not 'Raiffeisen Bank International', 26.10.2026).
GENERIC_COMPANY_WORDS = {"bank", "versicherung", "versicherungen", "group", "gruppe", "holding", "international",
                         "austria", "osterreich", "osterreichische", "oesterreichische", "wiener", "wien"}
GROUP_BRAND_WORDS = {"raiffeisen", "volksbank", "sparkasse", "hypo"}


def is_generic_company(name):
    """True when every word of the company name is a generic or group-brand word: such a name never proves
    that two postings come from one employer."""
    words = company_part(name).split()
    return bool(words) and set(words) <= GENERIC_COMPANY_WORDS | GROUP_BRAND_WORDS


def same_employer(a, b):
    """One employer when one company name contains the other as whole words, the shorter has >= 4 letters and is
    not only generic or group-brand words: 'check24' in 'check24 vergleichsportal', 'woom' in 'woom bikes', but not
    'raiffeisen bank' in 'raiffeisen bank steiermark' and not 'hypo' in 'hypo tirol'. A shared first word alone is
    not enough either: 'raiffeisen bank' and 'raiffeisenlandesbank' are two different banks."""
    x, y = sorted((company_part(a), company_part(b)), key=len)
    return len(x) >= 4 and f" {x} " in f" {y} " and not is_generic_company(x)


# S7-1 (27.10.2026): the company rule (b) also needs evidence that two rows are one employer, because a shared name
# is not enough ('VIG' vs 'Donau Versicherung', 'Austrian' vs 'Austrian Power Grid', Siemens vs Siemens Energy).
JOB_BOARD_DOMAINS = {"linkedin.com", "lnkd.in", "stepstone.at", "stepstone.de", "adzuna.at", "adzuna.com",
                     "karriere.at", "jobs.at", "derstandard.at", "indeed.com", "glassdoor.com", "xing.com",
                     "willhaben.at"}  # never evidence: every employer posts there
ATS_DOMAINS = {"bamboohr.com", "personio.com", "personio.de", "join.com", "greenhouse.io", "lever.co", "workable.com",
               "recruitee.com", "teamtailor.com", "smartrecruiters.com", "ashbyhq.com", "myworkdayjobs.com",
               "workday.com", "successfactors.com", "successfactors.eu", "taleo.net", "talentlyft.com",
               "softgarden.io", "pinpointhq.com", "breezy.hr", "factorialhr.com", "icims.com"}  # tenant = subdomain
GENERIC_TENANTS = {"", "www", "jobs", "job", "boards", "apply", "careers", "join"}  # subdomains that name no employer
DESCRIPTOR_WORDS = {"vergleichsportal", "bikes", "gruppe", "group", "holding", "osterreich", "oesterreich", "austria",
                    "osterreichische", "oesterreichische", "international", "digital", "services", "solutions",
                    "technologies", "tech", "software", "labs", "versicherung", "versicherungs",
                    "insurance"}  # words that only describe the same employer (27.10: insurance names)
POSTING_LOCALE = re.compile(r"[a-z]{2}([-_][a-z]{2,4})?", re.I)  # /de/, /en/, /en_GB/, /de-at/ in front of a path


def posting_link(url):
    """'host/slug' of a job link whose last path segment names one posting, else ''. The slug must be specific:
    4 digits in a row, or 12+ characters of three or more hyphen-joined words. So 'jobs', 'detail', a 3-digit id
    or a generic page such as 'job-position' (dry run on the server, 26.10.2026: it linked 8 GoStudent jobs in
    different cities) never ties two postings together. A leading locale is ignored: the same posting is often
    published under /de/ and /en/ (CHECK24, 26.10.2026)."""
    p = _job_parts(url)
    if not p:
        return ""
    host, path, _ = p
    segs = [s for s in path.split("/") if s]
    if len(segs) > 1 and POSTING_LOCALE.fullmatch(segs[0]):
        segs = segs[1:]
    slug = segs[-1].lower() if segs else ""
    specific = re.search(r"\d{4}", slug) or (len(slug) >= 12 and slug.count("-") >= 2)
    return f"{host}/{slug}" if slug and specific else ""


def _registrable(host):
    """example.com of a host; example.co.at for the Austrian second-level domains."""
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in ("co", "or", "ac", "gv") and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _company_host_shared(urls_a, urls_b):
    """True when both rows link to one company-owned host: the same host (www. ignored) or the same registrable
    domain. Job boards never count. On an ATS platform only the tenant subdomain counts (woom.bamboohr.com), never the
    platform itself: every BambooHR or Personio customer shares bamboohr.com or personio.com."""
    hosts_a = {p[0] for p in map(_job_parts, urls_a) if p}
    hosts_b = {p[0] for p in map(_job_parts, urls_b) if p}
    for a in hosts_a:
        for b in hosts_b:
            ra, rb = _registrable(a), _registrable(b)
            if ra in JOB_BOARD_DOMAINS or rb in JOB_BOARD_DOMAINS:
                continue
            if a == b and (ra not in ATS_DOMAINS or a[: -len(ra)].rstrip(".") not in GENERIC_TENANTS):
                return True
            if ra == rb and ra not in ATS_DOMAINS:
                return True
    return False


def _one_employer_evidence(me, r):
    """Evidence for the company rule (b) beyond the shared name: a shared company host, or the words the longer name
    adds are only descriptors ('bank' counts only when the shorter name is no group brand). Equal names need nothing more."""
    if _company_host_shared((me["url"], me["open_url"]), (r["url"], r["open_url"])):
        return True
    x, y = sorted((company_part(me["company"]), company_part(r["company"])), key=len)
    extra = set(y.split()) - set(x.split())
    group = bool(set(x.split()) & GROUP_BRAND_WORDS)
    return extra <= DESCRIPTOR_WORDS | (set() if group else {"bank"})


def _other_posting(urls_a, urls_b):
    """True when both sides name a job number on the same site and the numbers differ: two postings, whatever
    their titles say. SumUp 'Erfahrener Außendienstmitarbeiter' m/w/x and m/w/d are gh_jid 8559397002 and
    8874567002; GoStudent 'Center Manager' in each city is its own gh_jid on one job-position page (26.10.2026)."""
    named_a, named_b = {}, {}
    for urls, named in ((urls_a, named_a), (urls_b, named_b)):
        for u in urls:
            lid = link_id(u)
            if lid:
                host, number = lid.split("#", 1)
                named.setdefault(host, set()).add(number)
    return any(named_a[h].isdisjoint(named_b[h]) for h in named_a.keys() & named_b.keys())


def mark_if_duplicate(con, job_id):
    """After enrich found the original: this row and another row are one job when either
    (a) they share a link: the same normalized open_url (Adzuna 'Wiener Netze' whose original is Wiener
        Stadtwerke's own posting, 08.10.2026), or the same host and posting slug (posting_link, 26.10.2026), and
        their titles are similar; or
    (b) they have the same title and the same employer (same_employer): LinkedIn 'woom Bikes' and the company's
        own 'woom' feed, where the links differ (26.10.2026).
    One of the two is closed as closed_by='duplicate' so it is never sent - not as a card, a list or the витрина.
    The original is the one already delivered; if neither or both, the earlier first_seen; on a tie (one
    morning shares a first_seen) the earlier inserted row. Checked from either side, so the order in which
    a batch is scored does not matter. A row with a verdict or already sent is never marked, and a row
    that is itself a duplicate never counts as the original. Returns the id marked, or None."""
    from urllib.parse import urlsplit
    cols = "rowid, id, company, title, url, open_url, first_seen, sent_at, verdict, status, closed_by"
    shown, shown_params = in_window()  # the window the bot and digest show: a row still in it is the one to keep
    me = con.execute(f"select {cols}, ({shown}) as shown from jobs where id=?", [*shown_params, job_id]).fetchone()
    if not me or me["closed_by"] == "duplicate":
        return None
    if me["closed_by"] == "manual_removed":  # 'Это не отправлял': never an original, never a copy
        return None
    since30 = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")

    def recent(row):
        """S7-2, for the company-name rule only: still in the window, first seen within 30 days, or judged (a verdict
        or a status is Misha's own decision and never ages out). An old unjudged row cannot suppress a repost."""
        return (bool(row["shown"]) or row["verdict"] is not None or row["status"] is not None
                or (row["first_seen"] or "") >= since30)

    old = same_link(me["open_url"])
    slugs = {link for link in (posting_link(me["url"]), posting_link(me["open_url"])) if link}
    employer, title = company_part(me["company"]), title_part(me["title"])
    # SQL narrows the candidates to rows that share a link slug or the title key; the checks below decide.
    needles, params = [], []
    if old:
        path = urlsplit(me["open_url"].strip()).path.rstrip("/").lower()
        needles.append("instr(lower(url), ?) > 0 or instr(lower(open_url), ?) > 0")
        params += [path, path]
    for link in slugs:
        slug = link.rsplit("/", 1)[1]
        needles.append("instr(lower(url), ?) > 0 or instr(lower(open_url), ?) > 0")
        params += [slug, slug]
    if employer and title:
        needles.append("instr(key, ?) > 0")  # the dedupe key ends in '|<title part>'
        params.append("|" + title)
    if not needles:
        return None

    def rank(r):  # smaller = the original: delivered, then still shown, then the earlier first_seen
        return (r["sent_at"] is None, not r["shown"], r["first_seen"] or "", r["rowid"])

    for r in con.execute(f"select {cols}, ({shown}) as shown from jobs where id!=?"
                         f" and coalesce(closed_by,'') not in ('duplicate', 'manual_removed')"
                         f" and ({' or '.join(needles)})"
                         " order by first_seen, rowid", [*shown_params, me["id"], *params]).fetchall():
        if _other_posting((me["url"], me["open_url"]), (r["url"], r["open_url"])):
            continue  # each names its own job number on one site: two postings, even with the same title
        link_hit = bool(old and old in (same_link(r["url"]), same_link(r["open_url"]))) or bool(
            slugs & {posting_link(r["url"]), posting_link(r["open_url"])})  # the same posting, whatever its age
        by_name = bool(employer and title and title_part(r["title"]) == title
                       and same_employer(me["company"], r["company"]) and _one_employer_evidence(me, r)
                       and recent(me) and recent(r))  # S7-2: the name rule only, and only for recent rows
        same = (link_hit and similar_titles(me["title"], r["title"])) or by_name
        if not same:
            continue
        copy, original = max(me, r, key=rank), min(me, r, key=rank)
        if copy["verdict"] is None and copy["sent_at"] is None:
            con.execute("update jobs set closed=1, closed_by='duplicate', dup_of=?, promote=0 where id=?",
                        (original["id"], copy["id"]))
            return copy["id"]
    return None


def remember(con, job):
    """Insert if new; always sets job['id'] to the stored row's id. Returns True if never seen before.
    Same key, or the same job link under another company name (Wiener Stadtwerke / Wiener Netze, 08.10),
    is the same job: the first row is reused."""
    job.update(scrub(dict(job)))  # one broken character must not stop the morning (security-01)
    key = dedupe_key(job)
    existing = con.execute("select id from jobs where key=?", (key,)).fetchone()
    if existing:
        job["id"] = existing[0]
        return False
    same = find_same_link(con, job.get("url"))
    if same:
        job["id"] = same
        # Same job number, new title or slug: follow the posting, so the feed rule sees it as still listed.
        # not for a manual row (stage 8): Misha's own title and link stay as he wrote them
        con.execute("update jobs set url=?, title=? where id=? and url!=? and coalesce(source,'')!='manual'",
                    (job.get("url") or "", job.get("title") or "", same, job.get("url") or ""))
        return False
    jid, salt = job_id(key), 0
    while con.execute("select 1 from jobs where id=?", (jid,)).fetchone():  # rekeyed rows keep old ids
        salt += 1
        jid = job_id(f"{key}#{salt}")
    con.execute("insert into jobs (id, key, company, title, location, url, source, first_seen, data, company_id)"
                " values (?,?,?,?,?,?,?,?,?,?)",
                (jid, key, job.get("company") or "", job.get("title") or "", job.get("location") or "",
                 job.get("url") or "", job.get("source") or "", now(), json.dumps(job, ensure_ascii=False),
                 str(job.get("company_id") or "") if job.get("source") == "company" else None))
    job["id"] = jid
    return True


# ------------------------------------------------------------------ manual applications (stage 8)
# API for stage 8 - bot.py, report.py, digest.py, scripts/metrics.py and scripts/import_applications.py use these:
#   NOT_MANUAL                         SQL 'coalesce(source,'')!='manual'': put it in every card / verdict count
#   is_manual(row) -> bool             source='manual', or data.manual_applied (a bot card Misha attached to)
#   manual_datetime(day=None) -> str   'YYYY-MM-DD' -> now() for today, 'YYYY-MM-DDT12:00:00' for a past day,
#                                      None -> now(); a future day raises ValueError
#   find_application_candidates(con, company, title, url="") -> [row]
#                                      for the /add prompt: exact key, then same link, then same employer + similar title
#   exact_application_candidates(con, company, title, url="") -> [row]
#                                      exact key and same link only; a duplicate hit counts as its original
#   application_target(con, job, attach_to=None) -> row | None
#                                      the row add_manual would attach to (None = a new row would be created)
#   add_manual(con, job, applied_at, status="applied", status_at=None, channel="", note="", attach_to=None)
#                                      -> (job_id, outcome), outcome: 'created' | 'attached' | 'unchanged' | 'revived'
#                                      job: dict with company, title, url. applied_at / status_at: full ISO or 'YYYY-MM-DD'.
#                                      A card whose status is already set is never written: 'unchanged'.
#                                      Does NOT commit: the caller commits once (the bot also deletes its draft then).
#   add_manual(..., force_new=False)   the bot's 'Новая': no row with this key -> always a new manual row (no link or
#                                      similar-title attach, no mark_if_duplicate). A row with this key -> normal result,
#                                      nothing inserted. Exclusive with attach_to (ValueError).
#   is_exact_candidate(con, company, title, row) -> bool
#                                      True when the candidate row is this job by exact dedupe key (after dup_of).
#                                      The bot offers 'Новая' for a non-exact best candidate; for exact, it does not.
#   remove_manual(con, job_id) -> bool 'Это не отправлял': a manual row is closed (closed_by 'manual_removed') and its
#                                      key moves away (see Removed rows). An attached card (data.manual_applied) loses the
#                                      application and gets its old verdict back. Any other row, incl. a status Misha set
#                                      himself: returns False, nothing written.
#   manual_row_for_key(con, key) -> row | None
#                                      the manual row of a job: active under the key, or the one Misha removed (for the seed)
#   Removed rows (S8-5): remove_manual moves the key to key#removed:<id> (data.removed_key keeps the original). Alerts
#                                      and find_application_candidates no longer see it, so the same job comes back as a
#                                      card. add_manual with the same job revives the row under its original key when that
#                                      key is free; if an alert took the key meanwhile, add_manual attaches to that row.
#   Attach (S8-2): data.pre_attach holds the verdict before the attach; remove_manual restores it.
#   Caps (S8-7): company 200, title 300, url 2000, channel/note 500 characters, stored text only.
#   Status log: 'applied' at applied_at, the later status at status_at, 'removed' or 'reset' when removed.

NOT_MANUAL = "coalesce(source,'')!='manual'"  # manual applications are never cards, never verdict counts
MANUAL_CAPS = {"company": 200, "title": 300, "url": 2000, "channel": 500, "note": 500}  # S8-7: what add_manual stores


def is_manual(row):
    """True for a row Misha added by hand (source 'manual') and for a bot card he attached to (data.manual_applied).
    Works on sqlite3.Row and on dict."""
    try:
        if row["source"] == "manual":
            return True
    except (KeyError, IndexError):
        pass
    return bool(_data_of(row).get("manual_applied"))


def _data_of(row):
    """The data JSON of a row as a dict (a broken or missing value is an empty dict)."""
    try:
        d = json.loads(row["data"] or "{}")
    except (ValueError, TypeError, KeyError, IndexError):
        return {}
    return d if isinstance(d, dict) else {}


def manual_datetime(day=None):
    """The time stored for a manual application day. Today is now() (Misha's 'Сохранить' is a real moment); any past
    day is noon of that day, so a date never reads as a time of day. A full ISO value passes through unchanged."""
    if day is None:
        return now()
    if len(day) != 10:  # already a datetime
        return day
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"not a day (YYYY-MM-DD): {day!r}")
    today = datetime.now().strftime("%Y-%m-%d")
    if day > today:
        raise ValueError(f"future day {day}: an application cannot be sent later than today")
    return now() if day == today else f"{day}T12:00:00"


def _iso(value):
    """A day ('YYYY-MM-DD') or a full ISO value -> the stored form."""
    return manual_datetime(value) if value else now()


def _original(con, jid):
    """The row itself, or the original it duplicates (dup_of, up to 5 steps). None for a missing row or a removed
    manual row, which never counts as anything."""
    for _ in range(5):
        r = con.execute("select * from jobs where id=?", (jid,)).fetchone()
        if r is None:
            return None
        if r["closed_by"] == "duplicate" and r["dup_of"]:
            jid = r["dup_of"]
            continue
        return None if r["closed_by"] == "manual_removed" else r
    return None


def exact_application_candidates(con, company, title, url=""):
    """Rows that are this job by identity: the same dedupe key first, then the same link (find_same_link), each
    resolved to its original. Returns sqlite3.Row list without repeats."""
    key = dedupe_key({"company": company, "title": title, "url": url})
    hits = [r[0] for r in con.execute("select id from jobs where key=? order by first_seen, rowid", (key,)).fetchall()]
    link = find_same_link(con, url) if url else None
    if link:
        hits.append(link)
    out, seen = [], set()
    for jid in hits:
        r = _original(con, jid)
        if r is not None and r["id"] not in seen:
            seen.add(r["id"])
            out.append(r)
    return out


def find_application_candidates(con, company, title, url=""):
    """Everything that may be the same job as an application being added (the /add prompt and the seed dry run):
    exact_application_candidates first, then rows of the same employer with a similar title. Duplicates are not
    listed (their original is), removed manual rows never appear."""
    out = exact_application_candidates(con, company, title, url)
    seen = {r["id"] for r in out}
    for r in con.execute("select id, company, title from jobs"
                         " where coalesce(closed_by,'') not in ('duplicate', 'manual_removed') order by first_seen, rowid"
                         ).fetchall():
        if r["id"] not in seen and same_employer(company, r["company"]) and similar_titles(title, r["title"]):
            seen.add(r["id"])
            out.append(con.execute("select * from jobs where id=?", (r["id"],)).fetchone())
    return out


def application_target(con, job, attach_to=None):
    """The row add_manual attaches to, or None when it would create a new row. attach_to wins when given (its
    duplicate chain is followed). Otherwise a row with the same key (it is unique, so it is the job), else the one
    exact candidate by link. Two or more exact candidates without a key row: a new row, the caller decides."""
    if attach_to is not None:
        row = _original(con, attach_to)
        if row is None:
            raise ValueError(f"attach_to {attach_to!r} is not a live row")
        return row
    key_row = con.execute("select * from jobs where key=?", (dedupe_key(job),)).fetchone()
    if key_row is not None and key_row["source"] != "manual":
        return _original(con, key_row["id"]) or key_row
    hits = exact_application_candidates(con, job.get("company") or "", job.get("title") or "", job.get("url") or "")
    return hits[0] if len(hits) == 1 else None


def _check_status(status):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")


def _log_status(con, jid, status, applied_at, status_at):
    """'applied' at the applied day; the later status at its own time (never a second 'applied')."""
    con.execute("insert into status_log (job_id, status, at) values (?, 'applied', ?)", (jid, applied_at))
    if status != "applied":
        con.execute("insert into status_log (job_id, status, at) values (?, ?, ?)", (jid, status, status_at))


def _attach(con, row, status, applied_at, status_at, channel, note):
    """Make a bot card Misha's application, but only where it has no status yet: the status is then set (with its log
    rows), the verdict becomes interested and data.manual_applied is set, so remove_manual can undo exactly this.
    A verdict that changes is kept in data.pre_attach (S8-2), so removal puts it back. A status already on the row is
    Misha's own record: nothing is written, no flag is set, outcome 'unchanged'. Returns 'attached' or 'unchanged'."""
    if row["status"] is not None:
        return "unchanged"
    sets, params = ["status=?", "status_at=?", "applied_at=?"], [status, status_at, applied_at]
    if row["applied_at"] and row["applied_at"] < applied_at:  # an earlier applied day already on the row is kept
        params[2] = row["applied_at"]
    data = _data_of(row)
    if row["verdict"] != "interested":
        data["pre_attach"] = {"verdict": row["verdict"], "reasons": row["reasons"], "verdict_at": row["verdict_at"]}
        sets += ["verdict=?", "reasons=?", "verdict_at=?"]
        params += ["interested", "[]", now()]
    data["manual_applied"] = True
    for k, v in (("channel", channel), ("note", note)):
        if v:
            data[k] = v
    _log_status(con, row["id"], status, applied_at, status_at)
    sets.append("data=?")
    params.append(json.dumps(data, ensure_ascii=False))
    con.execute(f"update jobs set {', '.join(sets)} where id=?", [*params, row["id"]])
    return "attached"


def _removed_manual_row(con, key):
    """The manual row Misha removed whose key was this one: its key moved to key#removed:<id> (S8-5), data.removed_key
    keeps the original. None when there is none."""
    for r in con.execute("select * from jobs where source='manual' and closed_by='manual_removed'").fetchall():
        if _data_of(r).get("removed_key") == key:
            return r
    return None


def manual_row_for_key(con, key):
    """The manual row of a job for the seed script and the bot: the row under this key, or the one Misha removed
    (its key moved, see _removed_manual_row). None when there is none."""
    r = con.execute("select * from jobs where key=? and source='manual'", (key,)).fetchone()
    return r if r is not None else _removed_manual_row(con, key)


def add_manual(con, job, applied_at, status="applied", status_at=None, channel="", note="", attach_to=None,
               force_new=False):
    """Record an application Misha sent, outside the bot's cards. Returns (job_id, outcome):
    'created'   - a new manual row: verdict interested, source 'manual', counts as sent (so an unsent scored copy
                  of the same job is closed as duplicate by mark_if_duplicate, and the manual row is the original);
    'attached'  - a row without a status became his application (see _attach); 'unchanged' - nothing to add, or
                  the row already has a status (Misha's own, never written);
    'revived'   - the same key was removed by Misha before: the row is open again.
    A manual row with the same key is never changed by a second call (idempotent). Does not commit.
    force_new (the bot's 'Новая'): with no row of the same key, always a new manual row - no link or similar-title
    attach, and no mark_if_duplicate, which could close the candidate Misha called a different job. With a row of
    the same key nothing is inserted (the key is unique): that row gets the normal result. Exclusive with attach_to."""
    if force_new and attach_to is not None:
        raise ValueError("force_new and attach_to are exclusive")
    _check_status(status)
    applied_at = _iso(applied_at)
    status_at = _iso(status_at) if status_at else (applied_at if status == "applied" else now())
    job = dict(job)  # S8-7: stored text is capped first, so the key and the row use the same values
    for k in ("company", "title", "url"):
        job[k] = str(job.get(k) or "")[: MANUAL_CAPS[k]]
    channel, note = str(channel or "")[: MANUAL_CAPS["channel"]], str(note or "")[: MANUAL_CAPS["note"]]
    key = dedupe_key(job)
    company, title, url = job["company"], job["title"], job["url"]
    old = con.execute("select * from jobs where key=? and source='manual'", (key,)).fetchone()
    if old is not None:
        if old["closed_by"] != "manual_removed":
            return old["id"], "unchanged"
        return _revive(con, old, key, status, status_at, applied_at, channel, note)
    removed = None if force_new else _removed_manual_row(con, key)  # S8-5: its key moved away, it comes back
    if removed is not None and con.execute("select 1 from jobs where key=?", (key,)).fetchone() is None:
        return _revive(con, removed, key, status, status_at, applied_at, channel, note)
    key_row_exists = con.execute("select 1 from jobs where key=?", (key,)).fetchone() is not None
    if not (force_new and not key_row_exists):  # forced create skips the attach search, but never a key row
        target = application_target(con, job, attach_to)
        if target is not None:
            outcome = _attach(con, target, status, applied_at, status_at, channel, note)
            if outcome == "attached":
                mark_if_duplicate(con, target["id"])
            return target["id"], outcome
    jid, salt = job_id(key), 0
    while con.execute("select 1 from jobs where id=?", (jid,)).fetchone():  # rekeyed rows keep old ids
        salt += 1
        jid = job_id(f"{key}#{salt}")
    t = now()
    data = {"company": company, "title": title, "url": url, "location": job.get("location") or "",
            "manual": True, "channel": channel, "note": note}
    con.execute("insert into jobs (id, key, company, title, location, url, source, rule, first_seen, sent_at,"
                " listed, closed, verdict, reasons, verdict_at, status, status_at, applied_at, data)"
                " values (?,?,?,?,?,?,'manual','manual',?,?,0,0,'interested','[]',?,?,?,?,?)",
                (jid, key, company, title, job.get("location") or "", url, t, t, t, status, status_at, applied_at,
                 json.dumps(data, ensure_ascii=False)))
    _log_status(con, jid, status, applied_at, status_at)
    if not force_new:
        mark_if_duplicate(con, jid)  # an unsent scored copy of the same job is closed
    return jid, "created"


def _revive(con, row, key, status, status_at, applied_at, channel, note):
    """Open a manual row Misha removed again, under its original key (S8-5). Returns (job_id, 'revived')."""
    data = _data_of(row)
    data.pop("removed_key", None)
    data.update(manual=True, channel=channel or data.get("channel", ""), note=note or data.get("note", ""))
    con.execute("update jobs set key=?, closed=0, closed_by=null, verdict='interested', reasons='[]', verdict_at=?,"
                " status=?, status_at=?, applied_at=?, sent_at=coalesce(sent_at, ?), data=? where id=?",
                (key, now(), status, status_at, applied_at, now(), json.dumps(data, ensure_ascii=False), row["id"]))
    _log_status(con, row["id"], status, applied_at, status_at)
    mark_if_duplicate(con, row["id"])
    return row["id"], "revived"


def is_exact_candidate(con, company, title, row):
    """True when the candidate `row` (from find_application_candidates) is this job by exact dedupe key: its original
    (dup_of followed) has the key of (company, title). The bot offers 'Новая' only for a best candidate that is not
    exact. False for an empty company (the key then depends on the link, which this check does not have)."""
    if not company:
        return False
    original = _original(con, row["id"])
    return original is not None and original["key"] == dedupe_key({"company": company, "title": title})


def remove_manual(con, job_id):
    """'Это не отправлял'. A manual row is closed for good (closed_by 'manual_removed'): it leaves every list and count,
    and its key moves to key#removed:<id> (data.removed_key keeps the original), so an alert with the same key or link
    creates a card again (S8-5). An attached bot card (data.manual_applied, set only when add_manual set its status)
    loses only the application; its verdict goes back to what it was before the attach (data.pre_attach, S8-2). Any
    other row is refused: returns False and writes nothing, so a status Misha set himself is never cleared. Returns
    True if something was removed; no commit."""
    row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    if row is None:
        return False
    t = now()
    if row["source"] == "manual":
        if row["closed_by"] == "manual_removed":
            return False
        data = _data_of(row)
        data["removed_key"] = row["key"]
        con.execute("update jobs set key=?, closed=1, closed_by='manual_removed', verdict=null, status=null,"
                    " status_at=null, applied_at=null, data=? where id=?",
                    (f"{row['key']}#removed:{job_id}", json.dumps(data, ensure_ascii=False), job_id))
        con.execute("insert into status_log (job_id, status, at) values (?, 'removed', ?)", (job_id, t))
        return True
    if is_manual(row):
        data = _data_of(row)
        data.pop("manual_applied", None)
        pre = data.pop("pre_attach", None)  # S8-2: the verdict the card had before the attach, back again
        verdict, reasons, verdict_at = (pre["verdict"], pre["reasons"], pre["verdict_at"]) if pre else (
            row["verdict"], row["reasons"], row["verdict_at"])
        con.execute("update jobs set status=null, status_at=null, applied_at=null, verdict=?, reasons=?, verdict_at=?,"
                    " data=? where id=?",
                    (verdict, reasons, verdict_at, json.dumps(data, ensure_ascii=False), job_id))
        con.execute("insert into status_log (job_id, status, at) values (?, 'reset', ?)", (job_id, t))
        return True
    return False
