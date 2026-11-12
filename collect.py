"""Morning collection: pull vacancies from job-alert emails and Adzuna, dedupe them, and write
out/trial-<date>.csv for prefilter.py. One bad mail or Adzuna record is skipped, never the run.
The lookback covers every day since the last successful read of each source (a missed morning or a
dead mailbox is caught up, up to 15 days), and the run's health goes to out/health.json for the digest."""

import csv
import email
import email.utils
import html
import imaplib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

import health
import store

ROOT = Path(__file__).parent
OUT = store.OUT   # JOBDIGEST_OUT in tests, like every other stage (ops-15)
STATE = store.DB.parent / "collect_state.json"   # last successful read per source; survives out/ clean-ups
MAX_LOOKBACK = 15   # = store.WINDOW_DAYS: older jobs would not be shown anyway
MAX_BODY = 500_000  # chars of one mail body; real alerts are 20-80 KB
CAPS = {"title": 300, "company": 200, "location": 200, "extra": 1000, "url": 2000}


def load_env():
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ---------- email sources ----------

# Sender = the address's exact domain (or the exact address), never a substring of the From header:
# '"jobs.at" <evil@attacker.test>' is not jobs.at (security-05).
SENDERS = {
    "jobalerts-noreply@linkedin.com": "linkedin",
    "jobagent.stepstone.at": "stepstone",
    "karriere.at": "karriere",
    "derstandard.at": "derstandard",
    "jobs.at": "jobsat",
    "devjobs.at": "jobsat",
    "office@careercenter-univie.at": "uniwien",
}
# Alert mails from these carry no vacancy list we can parse (newsletters and 'new jobs for you' teasers):
# counted in the digest's health block, never parsed (pipeline-08).
NO_LIST = {"karriere", "jobsat"}


def _header(value):
    """Decoded header text; an unknown charset (windows-874, x-mac-cyrillic) falls back to latin-1/utf-8."""
    out = []
    try:
        parts = decode_header(value or "")
    except Exception:
        return str(value or "")
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                chunk = chunk.decode(charset or "ascii", "replace")
            except (LookupError, TypeError):
                chunk = chunk.decode("utf-8", "replace")
        out.append(chunk)
    return "".join(out)


def source_of(sender):
    addr = email.utils.parseaddr(_header(sender))[1].lower().strip()
    domain = addr.rsplit("@", 1)[1] if "@" in addr else ""
    for key, name in SENDERS.items():
        if "@" in key and addr == key:
            return name
        if "@" not in key and (domain == key or domain.endswith("." + key)):
            return name
    return None


def spoofed(msg):
    """Gmail's own verdict on the sender: a mail that fails DMARC is not from the domain it names."""
    auth = " ".join(str(v) for v in (msg.get_all("Authentication-Results") or [])).lower()
    return "dmarc=fail" in auth


def text_part(msg):
    """Plain text if the mail has it, else its HTML (DER STANDARD sends HTML only). An unknown charset is
    read as UTF-8 with replacement characters (security-03)."""
    found = {}
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html") and ct not in found:
            payload = part.get_payload(decode=True) or b""
            try:
                found[ct] = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except (LookupError, TypeError):
                found[ct] = payload.decode("utf-8", "replace")
    return (found.get("text/plain") or found.get("text/html", ""))[:MAX_BODY]


def fetch_alert_emails(env, since, stats=None):
    """Alert mails since `since`. Each mail is read on its own: a broken one is counted in stats['bad']
    and skipped, the rest still come through. stats['no_list'] counts mails from senders without a list."""
    stats = stats if stats is not None else {}
    stats.setdefault("bad", 0)
    stats.setdefault("no_list", 0)
    stats.setdefault("spoofed", 0)
    imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)  # a stalled connection must not eat the morning
    imap.login(env["GMAIL_ADDRESS"], env["GMAIL_APP_PASSWORD"].replace(" ", ""))
    imap.select("INBOX", readonly=True)  # never marks anything as read
    _, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
    mails = []
    for num in (data[0] or b"").split():
        try:
            _, d = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM)])")  # sender first: strangers' mail is never read
            src = source_of(email.message_from_bytes(d[0][1]).get("From", "")) if d and d[0] else None
            if not src:  # skip every non-job sender; their mail is never parsed or printed
                continue
            if src in NO_LIST:
                stats["no_list"] += 1
                continue
            _, d = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(d[0][1])
            if spoofed(msg):
                stats["spoofed"] += 1
                continue
            mails.append((src, str(msg.get("Date", "")), text_part(msg)))
        except (imaplib.IMAP4.abort, OSError):
            raise  # the connection itself is gone: the whole source failed
        except Exception as e:  # charset, MIME nesting, a deleted message: lose this mail only
            stats["bad"] += 1
            print(f"skipped one mail: {type(e).__name__}", file=sys.stderr)
    imap.logout()
    return mails


def dash_blocks(body):
    """Split at lines made of 10+ dashes, line by line (a regex with ^\s*...$ over many blank lines is
    quadratic: 60,000 blank lines took minutes, security-04)."""
    blocks, cur = [], []
    for line in body.split("\n"):
        s = line.strip()
        if len(s) >= 10 and set(s) == {"-"}:
            blocks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    blocks.append("\n".join(cur))
    return blocks


LI_SERVICE_START = ("see all jobs", "edit alert", "new jobs from your other alerts", "your job alert",
                    "a new job matches", "new jobs match", "new job matches", "manage alerts",
                    "you'll receive", "you’ll receive", "top job picks for you", "jobs you may be interested in",
                    "recommended for you", "jobs similar to", "more jobs for you")
LI_SERVICE_END = (" jobs in vienna", " jobs in austria")
LI_SEARCH_PLACE = re.compile(r" jobs in [^,]{2,40}$", re.I)  # a saved search's name: '... jobs in <place>'


def linkedin_service(line):
    """LinkedIn's own text around the vacancies: headers, 'See all jobs', 'Edit alert', the saved-search names
    in <strong> tags or wrapped onto a line of their own, links. Several saved searches share one mail, so this
    text sits between vacancies (Misha, 16.10.2026): it is never a title, company or location."""
    low = line.strip().lower()
    return (low.startswith(LI_SERVICE_START) or low.endswith(LI_SERVICE_END) or LI_SEARCH_PLACE.search(low) is not None
            or "<strong" in low or "</strong>" in low or (low.startswith("(") and " or " in low)
            or low == "promoted" or "https://" in low or "http://" in low)


def parse_linkedin(body):
    """Plain-text alert: blocks split by dashed lines. A job is a block with a 'View job: URL' link; its title,
    company and location are the first three lines that are not LinkedIn's service text, and the lines after
    them (badges such as '5 school alumni') are extra."""
    jobs, seen = [], set()
    body = body.replace("\r\n", "\n")
    for block in dash_blocks(body):
        m = re.search(r"View job: (https://www\.linkedin\.com/comm/jobs/view/(\d+)\S*)", block)
        if not m or m.group(2) in seen:  # one job matching two saved searches appears in two blocks
            continue
        lines = [l.strip() for l in block[: m.start()].splitlines() if l.strip()]
        lines = [l for l in lines if not linkedin_service(l)]
        if len(lines) < 3:
            continue
        seen.add(m.group(2))
        title, company, location = lines[0], lines[1], lines[2]
        # Raw alert links carry a one-click login token: keep only the clean job URL.
        jobs.append(dict(title=title, company=company, location=location,
                         url=f"https://www.linkedin.com/jobs/view/{m.group(2)}/", ext_id=f"li:{m.group(2)}",
                         extra=" | ".join(lines[3:])))
    return jobs


def parse_stepstone(body):
    """Plain-text alert: title, tracking URL, company, location, size, contract, work mode, teaser."""
    jobs = []
    lines = [l.strip() for l in body.splitlines()]
    for i, line in enumerate(lines):
        if not line.startswith("https://click.stepstone.at/") or i == 0:
            continue
        title = lines[i - 1]
        if not title or title.startswith(("http", "mehr", "Alle passenden", "E-Mail", "Diese E-Mail")):
            continue
        rest = [l for l in lines[i + 1 : i + 10] if l]
        if len(rest) < 2 or rest[0].startswith("http"):
            continue
        company, location = rest[0], rest[1]
        meta = [l for l in rest[2:5] if not l.startswith("http") and l not in ("mehr", "Neu")]
        jobs.append(dict(title=title, company=company, location=location, url=line,
                         ext_id="", extra=" | ".join(meta)))
    return jobs


def parse_derstandard(body):
    """HTML alert: per job a title link to /detail/<id>, then 'Company / Location', then 'Job ansehen'."""
    import html as _html
    urls = []
    for u in re.findall(r'href="(https://jobs\.derstandard\.at/detail/[^"?]+)', body):
        if not urls or urls[-1] != u:
            urls.append(u)
    # [^<>] instead of [^>]: a mail full of '<' without '>' stays linear (security-04)
    t = re.sub(r"<(br|/p|/div|/tr|/td|/h\d)[^<>]*>", "\n", body)
    t = _html.unescape(re.sub(r"<[^<>]*>", " ", t))
    lines = [l for l in (re.sub(r"\s+", " ", x).strip() for x in t.splitlines()) if l]
    jobs = []
    for i in range(1, len(lines) - 1):
        if lines[i + 1] == "Job ansehen" and " / " in lines[i]:
            company, location = lines[i].split(" / ", 1)
            url = urls[len(jobs)] if len(jobs) < len(urls) else ""
            jobs.append(dict(title=lines[i - 1], company=company, location=location, url=url, ext_id="", extra=""))
    return jobs


UW_MD = re.compile(r"^\[([^\]]+)\]\((https?://[^\s)]+)\)$")          # markdown: [Title](link)
UW_TITLE = re.compile(r"^(.*?)\s*\(\s*(https?://[^\s()]+)\s*\)$")    # plain text: Title ( link )
UW_COMPANY = re.compile(r"^(.+?)\s*\|\s*([^|]+?)\s*(?:\|\s*([^|]*?)\s*)?$")  # Company | Location | DD.MM.YYYY (date optional)
UW_HEADER = ("hallo", "es gibt neue ergebnisse", "alle jobs anzeigen", "jobalarm ändern", "jobportal career center")


def parse_uniwien(body):
    """Plain-text alert from the Uni Wien Career Center: a title line that ends with its link, then the line
    'Company | Location | DD.MM.YYYY'. Header and footer lines never have a company line under them, so they
    are not jobs. The link is a SendGrid click redirect: it goes under 'link' and resolve_uniwien turns it into
    the portal's own URL. This parser makes no requests."""
    lines = [l.strip()[:1000] for l in body.replace("\r\n", "\n").split("\n")]  # long lines cut: regex stays cheap
    lines = [l for l in lines if l]
    jobs = []
    for i in range(len(lines) - 1):
        m = UW_MD.match(lines[i]) or UW_TITLE.match(lines[i])
        c = UW_COMPANY.match(lines[i + 1]) if m else None
        if not m or not c:
            continue
        title = " ".join(html.unescape(m.group(1)).split())
        if not title or title.lower().startswith(UW_HEADER):
            continue
        try:
            posted = datetime.strptime(c.group(3) or "", "%d.%m.%Y").date().isoformat()
        except ValueError:  # no date or an impossible one: the job is kept without a posting date
            posted = ""
        jobs.append(dict(title=title, company=" ".join(html.unescape(c.group(1)).split()),
                         location=" ".join(html.unescape(c.group(2)).split()),
                         url="", ext_id="", posted=posted, extra="", link=m.group(2)))
    return jobs


UW_HOST = "jobs.careercenter.univie.ac.at"
UW_CLICK = ".sendgrid.net"  # the mail's links are SendGrid click redirects; no other host is ever requested
UW_JOB = re.compile(r"^/job/[^/\s]+-(\d{4,})/?$")  # /job/<slug>-<number>: the number is the portal's job id
UW_MAX = 30       # click links followed per run
UW_GAP = 10.0     # seconds between requests: the portal's robots.txt Crawl-delay
UW_TIMEOUT = 15   # seconds per request


def _uw_hop(url):
    """'portal' for an https URL on the job portal, 'click' for an https SendGrid host, else None. Port 443 only,
    no userinfo. A host ending in the SendGrid suffix is a name, so an IP literal never passes."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    if parts.scheme != "https" or "@" in parts.netloc or port not in (None, 443) or not host:
        return None
    if host == UW_HOST:
        return "portal"
    return "click" if host.endswith(UW_CLICK) else None


class _PortalReached(Exception):
    """The redirect chain reached the job portal: that URL is the result and is never requested."""

    def __init__(self, url):
        super().__init__("portal")
        self.url = url


class _ClickRedirects(urllib.request.HTTPRedirectHandler):
    """Checks each redirect hop before urllib requests it: SendGrid https hosts and the portal only. The first
    portal hop ends the chain without a request to it; any other hop is refused without a request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        kind = _uw_hop(target)
        if kind == "portal":
            raise _PortalReached(target)
        if kind != "click":
            raise OSError("redirect refused")  # nothing printed: the target may carry a token
        return super().redirect_request(req, fp, code, msg, headers, target)


def follow_link(link):
    """Final URL of one click link: the first URL must be an https SendGrid link, each later hop is checked by
    _ClickRedirects, the body is never read. HEAD first, GET when the server refuses HEAD. Raises on any failure;
    nothing here prints the link, it carries the recipient."""
    if _uw_hop(link) != "click":
        raise OSError("click link refused")  # no request at all
    opener = urllib.request.build_opener(_ClickRedirects)
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(link, method=method, headers={"User-Agent": "job-digest-bot"})
        try:
            with opener.open(req, timeout=UW_TIMEOUT) as resp:
                return resp.geturl()
        except _PortalReached as stop:
            return stop.url
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue
            raise
    raise OSError("no answer to GET")


def jobportal_url(final):
    """(url, ext_id) when the final URL is an https job page of the portal, else ('', ''). The URL is rebuilt
    without its query string; the id is the number at the end of the job's slug."""
    try:
        parts = urllib.parse.urlsplit(final)
        port = parts.port
    except ValueError:
        return "", ""
    m = UW_JOB.match(parts.path)
    if parts.scheme != "https" or parts.hostname != UW_HOST or port not in (None, 443) or not m:
        return "", ""
    return f"https://{UW_HOST}{parts.path}", f"uw:{m.group(1)}"


def resolve_uniwien(jobs, follow=None):
    """Gives each Uni Wien job the portal's URL and id. One request per distinct job (title, company, posted), so
    a job in two mails is followed once; UW_GAP seconds apart, at most UW_MAX per run. A failed request or a
    foreign target leaves url empty and the job is kept. The click link is removed from every Uni Wien job here,
    so it is never stored, logged or written to the CSV."""
    follow = follow or follow_link
    done, calls, failed = {}, 0, 0
    for j in jobs:
        if j.get("source") != "uniwien":
            continue
        link = j.pop("link", "")
        key = (j.get("title", ""), j.get("company", ""), j.get("posted", ""))
        if key not in done:
            if not link or calls >= UW_MAX:
                done[key] = ("", "")
            else:
                if calls:
                    time.sleep(UW_GAP)
                calls += 1
                try:
                    done[key] = jobportal_url(follow(link))
                except Exception:  # network, timeout, HTTP error: this job goes on without a link
                    done[key] = ("", "")
                    failed += 1
        j["url"], j["ext_id"] = done[key]
    linked = sum(1 for j in jobs if j.get("source") == "uniwien" and j.get("url"))
    print(f"uniwien links: {calls} followed, {failed} failed, {linked} jobs with a link", file=sys.stderr)
    return calls


PARSERS = {"linkedin": parse_linkedin, "stepstone": parse_stepstone, "derstandard": parse_derstandard,
           "uniwien": parse_uniwien}


# ---------- Adzuna ----------

ADZUNA_QUERIES = [
    ("product manager", "Wien"), ("product owner", "Wien"), ("product analyst", "Wien"),
    ("produktmanager", "Wien"), ("marktforschung", "Wien"),
    ("market research", "Wien"), ("growth manager", "Wien"), ("werkstudent", "Wien"),
    ("praktikum product", "Wien"), ("product manager remote", "Österreich"),
]


ADZUNA_PAGE, ADZUNA_PAGES = 50, 5
ADZUNA_PAUSE = 2.6  # the free plan allows 25 calls a minute


def adzuna_job(j, what):
    """One Adzuna result as a job; null fields are empty (security-12)."""
    obj = lambda k: j.get(k) if isinstance(j.get(k), dict) else {}
    created = str(j.get("created") or "")[:10]
    return dict(source="adzuna", query=what, title=str(j.get("title") or ""),
                company=str(obj("company").get("display_name") or ""),
                location=str(obj("location").get("display_name") or ""),
                url=str(j.get("redirect_url") or ""), ext_id=f"az:{j.get('id')}", posted=created,
                extra=(f"posted {created}" if created else "")
                      + (f" | {j.get('contract_time')}" if j.get("contract_time") else ""))


def fetch_adzuna(env, days, stats=None):
    """Newest first; a full page means there may be more, so page on (max ADZUNA_PAGES per query) -
    a 14-day backfill returns more than one page for the broad queries. A malformed record is skipped,
    a failed query is counted in stats (calls / failed) for the digest's health block."""
    stats = stats if stats is not None else {}
    stats.setdefault("calls", 0)
    stats.setdefault("failed", 0)
    stats.setdefault("bad", 0)
    jobs, calls = [], 0
    for what, where in ADZUNA_QUERIES:
        for page in range(1, ADZUNA_PAGES + 1):
            q = urllib.parse.urlencode({
                "app_id": env["ADZUNA_APP_ID"], "app_key": env["ADZUNA_APP_KEY"], "what": what, "where": where,
                "max_days_old": days, "results_per_page": ADZUNA_PAGE, "sort_by": "date",
                "content-type": "application/json"})
            if calls:
                time.sleep(ADZUNA_PAUSE)
            calls += 1
            stats["calls"] += 1
            try:
                r = json.load(urllib.request.urlopen(f"https://api.adzuna.com/v1/api/jobs/at/search/{page}?{q}",
                                                     timeout=30))
            except Exception as e:  # keep going; one failed query must not kill the run
                stats["failed"] += 1
                print(f"adzuna '{what}' page {page} failed: {type(e).__name__}", file=sys.stderr)
                break
            results = r.get("results") if isinstance(r, dict) and isinstance(r.get("results"), list) else []
            for j in results:
                try:
                    jobs.append(adzuna_job(j, what))
                except Exception:
                    stats["bad"] += 1
            if len(results) < ADZUNA_PAGE:
                break
            if page == ADZUNA_PAGES:
                print(f"adzuna '{what}': {ADZUNA_PAGES} full pages, older results not read", file=sys.stderr)
    return jobs


# ---------- dedupe ----------

LEGAL = r"\b(gmbh|ag|kg|og|e\.?u\.?|ltd|inc|se|co|& co|group|gruppe|austria|österreich)\b"
GENDER = r"\((m/w/d|w/m/d|m/w/x|w/m/x|f/m/d|m/f/d|all genders|w/m/div|m/w/div|d/f/m|gn)\)|:in\b|\*in\b"


def norm(s, pattern):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(pattern, " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def dedupe(jobs):
    seen, out = {}, []
    for j in jobs:
        # a title or company with no Latin letters keeps its own letters; no company = the ad's link (data-2)
        company = norm(j["company"], LEGAL) or j["company"].lower().strip() or "?" + j.get("url", "")
        key = (company, norm(j["title"], GENDER) or j["title"].lower().strip())
        if key in seen:
            seen[key]["also_on"] = ",".join(sorted(set(filter(None, seen[key]["also_on"].split(",") + [j["source"]]))))
            continue
        j["also_on"] = j["source"]
        seen[key] = j
        out.append(j)
    return out


def clean(job):
    """Every field a capped, valid UTF-8 string: a lone surrogate or a 200 KB line in one hostile mail
    must not break the CSV or the next steps (security-01/02)."""
    return {k: re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", store.scrub(str(v if v is not None else "")))[:CAPS.get(k, 300)]
            for k, v in job.items()}  # NUL and other control characters break the csv writer


def load_state():
    try:
        d = json.loads(STATE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def lookback(state, source, minimum, now=None):
    """Days to read for a source: since its last successful read (a missed morning, a dead mailbox or a
    server down for days is caught up, ops-07), at least `minimum`, at most MAX_LOOKBACK."""
    now = now or datetime.now()
    try:
        gap = (now - datetime.fromisoformat(state[source])).days + 1
    except (KeyError, TypeError, ValueError):
        gap = minimum
    return max(minimum, min(MAX_LOOKBACK, gap))


def save_state(state):
    try:
        STATE.parent.mkdir(exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        tmp.replace(STATE)
    except OSError as e:
        print(f"could not save {STATE.name}: {type(e).__name__}", file=sys.stderr)


def main():
    minimum = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    env = load_env()
    state, started = load_state(), datetime.now()
    raw, per_mail, mail_stats, az_stats, email_error = [], [], {}, {}, ""
    email_days = lookback(state, "email", minimum, started)
    try:
        mails = fetch_alert_emails(env, started - timedelta(days=email_days), mail_stats)
        state["email"] = started.isoformat(timespec="seconds")
    except Exception as e:  # mail down or password revoked: still collect Adzuna below
        email_error = type(e).__name__
        print(f"email collection failed: {type(e).__name__}: {e}", file=sys.stderr)
        mails = []
    for src, date, body in mails:
        try:
            parsed = PARSERS.get(src, lambda b: [])(body)
        except Exception as e:
            mail_stats["bad"] = mail_stats.get("bad", 0) + 1
            print(f"could not parse one {src} mail: {type(e).__name__}", file=sys.stderr)
            continue
        per_mail.append((src, date[:22], len(parsed)))
        for j in parsed:
            j.update(source=src, query="")
            raw.append(j)
    try:
        resolve_uniwien(raw)  # Uni Wien click links -> portal URLs; paced, and a failure keeps the jobs
    except Exception as e:
        for j in raw:
            j.pop("link", None)  # a click link is never stored, even when resolving stops halfway
        print(f"uniwien links not resolved: {type(e).__name__}", file=sys.stderr)
    az_days = lookback(state, "adzuna", minimum, started)
    try:
        raw += fetch_adzuna(env, az_days, az_stats)
    except Exception as e:  # .env without Adzuna keys and the like: the emails above still go out
        az_stats["error"] = type(e).__name__
        print(f"adzuna collection failed: {type(e).__name__}: {e}", file=sys.stderr)
    if az_stats.get("calls") and az_stats.get("failed", 0) < az_stats["calls"]:
        state["adzuna"] = started.isoformat(timespec="seconds")
    unique = dedupe([clean(j) for j in raw])

    OUT.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    cols = ["source", "also_on", "title", "company", "location", "extra", "posted", "query", "url"]
    tmp = OUT / f"trial-{stamp}.csv.tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(unique)
    tmp.replace(OUT / f"trial-{stamp}.csv")
    save_state(state)  # only after the table is written: a crash before it re-reads the same days tomorrow
    health.record("collect", email_error=email_error, email_days=email_days, mails=len(mails),
                  bad_mails=mail_stats.get("bad", 0), no_list=mail_stats.get("no_list", 0),
                  spoofed=mail_stats.get("spoofed", 0), adzuna_calls=az_stats.get("calls", 0),
                  adzuna_failed=az_stats.get("failed", 0), adzuna_error=az_stats.get("error", ""),
                  adzuna_days=az_days,
                  mails_by_source={s: sum(1 for m in mails if m[0] == s) for s in ("linkedin", "stepstone")})

    print(f"lookback: email {email_days} days, adzuna {az_days} days")
    print("emails:")
    for src, date, n in per_mail:
        print(f"  {src:12} {date}  {n} jobs")
    print(f"mails skipped: broken {mail_stats.get('bad', 0)}, without a vacancy list (jobs.at/karriere.at) "
          f"{mail_stats.get('no_list', 0)}, failed DMARC {mail_stats.get('spoofed', 0)}")
    counts = {}
    for j in raw:
        counts[j["source"]] = counts.get(j["source"], 0) + 1
    print("raw per source:", counts)
    print(f"raw total {len(raw)}, unique {len(unique)}, duplicates removed {len(raw) - len(unique)}")
    print("table:", OUT / f"trial-{stamp}.csv")


if __name__ == "__main__":
    main()
