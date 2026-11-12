"""Verbatim posting text, fetched by us from the employer's own site or its applicant-tracking system.
Claude's page reader only returns a summary, so enrich.py could never keep a real description. Here the code
opens the page itself: first the schema.org JobPosting block the employer publishes (clean text), else the main
text of the page. Job boards are never opened: karriere.at, jobs.at, LinkedIn, StepStone, Indeed, Glassdoor,
willhaben and the rest ban robots, so DENY is checked first and beats every other rule. robots.txt of the host
is honoured for user agent 'job-digest-bot'. Every URL (first, each redirect hop, final, robots) must be http(s)
on a public address: a private or local target is never fetched (SSRF). One page gets one total deadline."""

import datetime
import html
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urlsplit

import enrich
import store

MAX_BYTES = 2_000_000  # a posting page is far smaller; more than this is a page we do not need
CHUNK = 65_536         # read in chunks, so the deadline is checked between them
DEADLINE = 30          # seconds for one page, redirects included
ROBOT_MAX = 500_000
MIN_CHARS = 300        # shorter is a teaser, a cookie wall or an error page
FORM_MIN = 200         # a form with less text than this is an application button, not the posting
DESC_LIMIT = 8000
ROBOT_AGENT = "job-digest-bot"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 (job-digest; personal job search)")

# Never fetched, whatever the company or the link says. Checked by registrable domain before anything else.
DENY = frozenset(("karriere.at", "jobs.at", "linkedin.com", "stepstone.at", "stepstone.de", "indeed.com",
                  "indeed.at", "glassdoor.com", "willhaben.at", "xing.com", "monster.at", "derstandard.at",
                  "adzuna.at", "devjobs.at"))
# Applicant-tracking systems. One host serves many employers, so the company name must be on the page too.
ATS = frozenset(("greenhouse.io", "lever.co", "smartrecruiters.com", "personio.de", "personio.com", "recruitee.com",
                 "workable.com", "ashbyhq.com", "teamtailor.com", "successfactors.com", "successfactors.eu",
                 "myworkdayjobs.com", "onlyfy.jobs", "dvinci-hr.com", "talentlyft.com", "breezy.hr", "join.com",
                 "softgarden.io", "factorial.com", "bamboohr.com", "prescreen.io", "jobs.personio.de"))
# Hosts fetched although their domain is not an employer's own: the Uni Wien job portal (robots.txt allows job
# pages, Crawl-delay 10 s). Only this host, not the rest of univie.ac.at.
EXTRA_HOSTS = frozenset(("jobs.careercenter.univie.ac.at",))
BAD_SUFFIX = (".localhost", ".local", ".internal", ".lan")  # names that resolve inside a network, never public
PORTS = (None, 80, 443)
# Full phrases only: a live posting may say 'expired contracts' or 'position is closed to external candidates'.
CLOSED = re.compile(
    r"job (posting )?(has )?expired|stellenanzeige (ist )?abgelaufen|no longer available"
    r"|nicht mehr verfügbar|no longer accepting applications|position has been filled"
    r"|stelle (ist )?(bereits )?(besetzt|vergeben)|page not found|seite nicht gefunden"
    r"|\b404\b[^.\n]{0,40}(not found|nicht gefunden)", re.I)
# The only reasons enrich may close a job on. A phrase match never closes it.
CLOSE_REASONS = frozenset(("http 410", "expired (validThrough)"))
JSONLD = re.compile(r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script\s*>', re.I | re.S)
META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)
COOKIE = re.compile(r"(cookie|consent)", re.I)  # a class or id token that starts like a consent banner
LEGAL = frozenset(("gmbh", "ag", "kg", "co", "se", "ltd", "inc", "llc", "sa", "austria", "osterreich",
                   "oesterreich", "group", "gruppe"))
VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                  "track", "wbr"))
SKIP_TAGS = frozenset(("script", "style", "noscript", "template", "svg", "iframe"))
SKIP_IF_CLOSED = frozenset(("nav", "header", "footer"))  # menus; dropped only when their closing tag exists
HIDDEN = SKIP_TAGS | SKIP_IF_CLOSED
BREAK = frozenset(("p", "div", "section", "article", "main", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
                   "table", "blockquote", "dd", "dt", "hr", "br"))
MIN_GAP = 1.0    # seconds between two requests to the same host
CRAWL_MAX = 15.0  # a robots.txt Crawl-delay raises the gap for its host, but never above this

_ROBOTS = {}      # scheme://host -> _Rules, once per run
_HOST_LOCKS = {}  # host -> RLock: one request at a time per host; reentrant, robots is read under it too
_HOST_LAST = {}   # host -> clock time of the last request started to it
_HOST_GAP = {}    # host -> seconds between requests, when its robots.txt sets a Crawl-delay
_GUARD = threading.Lock()
_clock = time.monotonic  # tests replace these with a fake clock
_sleep = time.sleep


class _PageTimeout(TimeoutError):
    """The page deadline (DEADLINE, redirects and robots included) has passed."""


class _Refused(Exception):
    """A URL that may not be fetched. .reason is the short reason for the run log."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _today():
    return datetime.date.today()


def _host_lock(host):
    with _GUARD:
        return _HOST_LOCKS.setdefault(host, threading.RLock())


def _polite(host):
    """Waits until the host's gap (MIN_GAP, or its robots Crawl-delay) has passed since the last request to it. The
    lock is held through the wait, so threads queue per host and never hit one site in parallel. Returns the
    seconds spent here, queue included: the page deadline does not count them."""
    started = _clock()
    with _host_lock(host):
        gap = _HOST_GAP.get(host, MIN_GAP)
        last = _HOST_LAST.get(host)
        if last is not None and _clock() - last < gap:
            _sleep(gap - (_clock() - last))
        _HOST_LAST[host] = _clock()
    return _clock() - started


def _parts(url):
    """(parsed, host) for a plain http(s) URL on a real host with a normal port. Userinfo, IP literals, local
    names and odd ports are refused (SSRF). None when refused."""
    try:
        parsed = urlsplit((url or "").strip())
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("http", "https") or "." not in host or not enrich.HOST_OK.fullmatch(host)
            or enrich._is_ip(host) or "@" in parsed.netloc or host.endswith(BAD_SUFFIX) or port not in PORTS):
        return None
    return parsed, host


def allowed(url, job):
    """True when the page may be read at all: not a job board, and either the job's own company domain,
    an ATS domain, or an official domain whose first label equals the company (enrich rule c)."""
    parsed = _parts(url)
    if not parsed:
        return False
    domain = enrich.registrable_domain(parsed[1])
    if domain in DENY:
        return False
    if domain in ATS or parsed[1] in EXTRA_HOSTS:
        return True
    if domain in enrich._own_domains(job):
        return True
    # Rule c: a job board in JOB_BOARDS passes trusted_url for any company, so it is excluded here.
    return domain not in enrich.JOB_BOARDS and bool(enrich.trusted_url(url, job, True))


def _resolve(host, port):
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def _global_ip(addr):
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                                 or ip.is_multicast or ip.is_unspecified)


def _public_host(parsed):
    """Every address the name resolves to is a public one. A name with no address is not public."""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addrs = _resolve(parsed.hostname, port)
    return bool(addrs) and all(_global_ip(a) for a in addrs)


def _refusal(url, job):
    """Why this URL may not be fetched, or None. Used for the first URL, every redirect hop, the final URL and
    robots.txt."""
    parsed = _parts(url)
    if not parsed:
        return "not allowed host"
    if enrich.registrable_domain(parsed[1]) in DENY:
        return "banned host"
    if not allowed(url, job):
        return "not allowed host"
    if not _public_host(parsed[0]):
        return "not allowed host"
    if not _robots_ok(url):
        return "robots"
    return None


def _pattern_match(pattern, target):
    """robots.txt path pattern: '*' matches any run of characters, a trailing '$' anchors the end."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if c == "*" else re.escape(c) for c in body)
    return re.match(regex + (r"\Z" if anchored else ""), target) is not None


class _Rules:
    """robots.txt for job-digest-bot: its own group if one names it, else the '*' group. The longest matching
    rule wins; on a tie Allow wins. disallow_all covers 401/403/5xx on robots.txt (RFC 9309)."""

    def __init__(self, text="", disallow_all=False):
        self.disallow_all = disallow_all
        self.groups = []  # (user-agents, rules, crawl-delay seconds or None) per robots.txt group
        if not disallow_all:
            self._parse(text)

    def _parse(self, text):
        groups, agents, rules, crawl, prev_agent = [], None, [], None, False
        for raw in text.lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (x.strip() for x in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not prev_agent:  # consecutive User-agent lines share one group
                    if agents is not None:
                        groups.append((agents, rules, crawl))
                    agents, rules, crawl = [], [], None
                agents.append(value.lower())
                prev_agent = True
                continue
            prev_agent = False
            if agents is not None and field in ("allow", "disallow"):
                rules.append((field == "allow", value))
            elif agents is not None and field == "crawl-delay":
                crawl = _seconds(value)
        if agents is not None:
            groups.append((agents, rules, crawl))
        self.groups = groups

    def _rules_for(self):
        ua = ROBOT_AGENT.lower()
        specific = [rule for agents, rules, _ in self.groups
                    if any(a and a != "*" and ua.startswith(a) for a in agents) for rule in rules]
        if specific:
            return specific
        return [rule for agents, rules, _ in self.groups if "*" in agents for rule in rules]

    def crawl_delay(self):
        """Crawl-delay of the group that applies to us (same group choice as _rules_for), or None."""
        ua = ROBOT_AGENT.lower()
        specific = [c for agents, _, c in self.groups
                    if c is not None and any(a and a != "*" and ua.startswith(a) for a in agents)]
        if specific:
            return specific[0]
        return next((c for agents, _, c in self.groups if c is not None and "*" in agents), None)

    def can_fetch(self, url):
        if self.disallow_all:
            return False
        p = urlsplit(url)
        target = (p.path or "/") + (("?" + p.query) if p.query else "")
        best = None
        for allow, pattern in self._rules_for():
            if pattern and _pattern_match(pattern, target):
                key = (len(pattern), allow)
                if best is None or key > best:
                    best = key
        return best is None or best[1]


class _RobotsRedirect(urllib.request.HTTPRedirectHandler):
    """robots.txt may redirect only within its own host, and only to a public address."""

    def __init__(self, host, deadline=None):
        super().__init__()
        self.host = host
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.deadline is not None and _clock() > self.deadline:
            raise _PageTimeout("page deadline")
        parsed = _parts(newurl)
        if not parsed or parsed[0].netloc != self.host or not _public_host(parsed[0]):
            raise _Refused("robots redirect")
        _polite(self.host)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_robots(robots_url):
    """_Rules for the host, read within the page deadline. 401/403/5xx = disallow everything; 404 and the other
    errors raise, and the caller treats a failure as allowed."""
    host = urlsplit(robots_url).netloc
    _polite(host)
    deadline = _clock() + DEADLINE  # the robots read has its own 30 s, after the queue
    req = urllib.request.Request(robots_url, headers={"User-Agent": UA})
    opener = urllib.request.build_opener(_RobotsRedirect(host, deadline))
    try:
        with opener.open(req, timeout=_budget(10, deadline)) as resp:
            body = _read_capped(resp, deadline, ROBOT_MAX)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) or e.code >= 500:
            return _Rules(disallow_all=True)
        raise
    return _Rules(body.decode("utf-8-sig", "replace"))


def _robots_ok(url):
    """robots.txt of the host, read once per run under the host lock, so concurrent jobs on one host wait for the
    single read and the Crawl-delay gap is known before their first page request. A failed read (404, timeout,
    network error) means allowed. A read that is cut off is not cached."""
    parsed = urlsplit(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    with _host_lock(parsed.netloc):
        if base not in _ROBOTS:
            try:
                _ROBOTS[base] = _fetch_robots(base + "/robots.txt")
                _set_gap(parsed.netloc, _ROBOTS[base])
            except _PageTimeout:
                raise
            except Exception:
                _ROBOTS[base] = _Rules()
        return _ROBOTS[base].can_fetch(url)


def _seconds(value):
    """A Crawl-delay value in seconds, or None when it is not a plain non-negative number."""
    try:
        v = float(value)
    except ValueError:
        return None
    return v if 0 <= v < float("inf") else None


def _set_gap(host, rules):
    """The host's robots.txt Crawl-delay becomes its gap between requests: at least MIN_GAP, at most CRAWL_MAX."""
    delay = rules.crawl_delay()
    if delay is not None:
        _HOST_GAP[host] = min(CRAWL_MAX, max(MIN_GAP, delay))


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Checks every redirect target before urllib follows it, and stops once the page deadline has passed."""

    def __init__(self, job, deadline):
        super().__init__()
        self.job = job
        self.deadline = deadline

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _clock() > self.deadline:
            raise _PageTimeout("page deadline")
        started = _clock()
        reason = _refusal(newurl, self.job)
        if reason:
            raise _Refused(reason)
        _polite(urlsplit(newurl).netloc)
        self.deadline += _clock() - started  # the wait for the next host does not count
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _budget(timeout, deadline):
    """Socket timeout for one read: never longer than what is left of the page deadline."""
    return max(0.1, min(timeout, deadline - _clock()))


def _read_capped(resp, deadline, limit=MAX_BYTES):
    """Body up to limit bytes, read in chunks; raises _PageTimeout once the page deadline has passed."""
    read = getattr(resp, "read1", resp.read)
    buf = bytearray()
    while len(buf) < limit:
        if _clock() > deadline:
            raise _PageTimeout("page deadline")
        chunk = read(min(CHUNK, limit - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _open_page(url, job, timeout):
    """(final_url, content_type, charset, body). The body is read only for text/html. The 30 s page deadline starts
    once the host gap has passed; redirect hops and the body read count against it."""
    _polite(urlsplit(url).netloc)
    deadline = _clock() + DEADLINE
    guard = _RedirectGuard(job, deadline)
    opener = urllib.request.build_opener(guard)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html",
                                               "Accept-Language": "de,en;q=0.8"})
    with opener.open(req, timeout=_budget(timeout, guard.deadline)) as resp:
        ctype = resp.headers.get_content_type()
        if ctype != "text/html":
            return resp.geturl(), ctype, None, None
        return resp.geturl(), ctype, resp.headers.get_content_charset(), _read_capped(resp, guard.deadline)


def _decode(body, charset):
    try:
        return body.decode(charset or "utf-8", "replace")
    except LookupError:  # a charset name Python does not know
        return body.decode("utf-8", "replace")


def _meta_charset(body):
    """Charset named in a <meta> tag of the first 4 KB, for pages whose HTTP header names none."""
    m = META_CHARSET.search(body[:4096])
    return m.group(1).decode("ascii") if m else None


def _cookie_box(attrs):
    """A banner by its class or id token. body, html and main are never a banner, whatever their classes."""
    for name, value in attrs:
        if name in ("id", "class") and value and any(COOKIE.match(tok) for tok in value.split()):
            return True
    return False


class _Visible(HTMLParser):
    """Visible text of a page. Drops scripts, cookie boxes, menus with a closing tag, and short forms; keeps line
    breaks for blocks and list items, the title and the <h1> text, and the text of <main> / <article> separately."""

    def __init__(self, closable=frozenset()):
        super().__init__(convert_charrefs=True)
        self.closable = closable  # menu tags (nav/header/footer) that have a closing tag in the document
        self.parts = []
        self.main = []
        self.h1 = []
        self.title = ""
        self.stack = []
        self.open = Counter()
        self.cookie = []   # stack depth of each open cookie box
        self.marks = []    # (len(parts), len(main)) at each open <form>
        self._in_title = False

    def _emit(self, text):
        if not self.open["h1"] and (self.cookie or any(self.open[t] for t in HIDDEN)):
            return
        self.parts.append(text)
        if self.open["main"] or self.open["article"]:
            self.main.append(text)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
            return
        if tag in SKIP_IF_CLOSED and tag not in self.closable:
            return  # an unclosed menu tag is ignored: its text stays
        if tag == "li":
            self._emit("\n- ")
        elif tag in BREAK:
            self._emit("\n")
        elif tag in ("td", "th"):
            self._emit(" ")
        if tag in VOID:
            return
        if tag == "form":
            self.marks.append((len(self.parts), len(self.main)))
        if tag not in ("body", "html", "main") and _cookie_box(attrs):
            self.cookie.append(len(self.stack) + 1)
        self.stack.append(tag)
        self.open[tag] += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if self.open[tag]:  # close the tag and anything left open inside it
            while self.stack:
                t = self.stack.pop()
                self.open[t] -= 1
                while self.cookie and self.cookie[-1] > len(self.stack):
                    self.cookie.pop()
                if t == "form":
                    self._close_form()
                if t == tag:
                    break
        if tag in BREAK:
            self._emit("\n")

    def _close_form(self):
        start_parts, start_main = self.marks.pop()
        if len("".join(self.parts[start_parts:]).strip()) < FORM_MIN:
            del self.parts[start_parts:]
            del self.main[start_main:]

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.open["h1"]:
            self.h1.append(data)
        self._emit(data)


def _tidy(s):
    """Collapse spaces inside lines, drop empty lines beyond one, keep the line breaks."""
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in s.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _html_text(fragment):
    """Text of a JSON-LD description, which is HTML (sometimes escaped twice)."""
    if "<" not in fragment and "&lt;" in fragment:
        fragment = html.unescape(fragment)
    vis = _Visible()
    vis.feed(fragment)
    vis.close()
    return _tidy("".join(vis.parts))


def _iso_date(value):
    """'2026-10-01' or '2026-10-01T10:00:00+02:00' -> '2026-10-01'; anything else -> ''."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", value) if isinstance(value, str) else None
    if not m:
        return ""
    try:
        return datetime.date.fromisoformat(m.group(1)).isoformat()
    except ValueError:
        return ""


def _jobposting_nodes(obj):
    if isinstance(obj, list):
        for item in obj:
            yield from _jobposting_nodes(item)
    elif isinstance(obj, dict):
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(isinstance(t, str) and t.split("/")[-1] == "JobPosting" for t in types):
            yield obj
        if "@graph" in obj:
            yield from _jobposting_nodes(obj["@graph"])


def _from_jsonld(doc, job):
    """The JobPosting for this job: among postings whose title matches the job, the one with the longest
    description. None when the page has no such posting."""
    best = None
    for block in JSONLD.findall(doc):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        for node in _jobposting_nodes(data):
            title = html.unescape(node.get("title")) if isinstance(node.get("title"), str) else ""
            title = title.strip()
            if not title or not _title_match(title, job):
                continue
            desc = node.get("description")
            if isinstance(desc, list):
                desc = "\n".join(x for x in desc if isinstance(x, str))
            body = _html_text(desc) if isinstance(desc, str) and desc.strip() else ""
            cand = {"text": _tidy(f"{title}\n{body}") if body else "", "title": title,
                    "posted": _iso_date(node.get("datePosted")), "valid_through": _iso_date(node.get("validThrough"))}
            if best is None or len(cand["text"]) > len(best["text"]):
                best = cand
    return best


def _start_at_title(text, job):
    """Career pages put their menu above the posting. If a line with the job title's main words sits in the first
    half of the text, start there (that line is kept). Otherwise the text is returned unchanged."""
    words = set(re.findall(r"[a-z]{4,}", enrich._fold(job.get("title") or "")))
    if not words:
        return text
    offset = 0
    for line in text.split("\n"):
        if offset > len(text) / 2:
            break
        folded = enrich._fold(line)
        if sum(w in folded for w in words) * 2 >= len(words):
            return text[offset:].strip()
        offset += len(line) + 1
    return text


def _title_match(text, job):
    """At least half of the job title's words with 4+ letters appear as whole words in the text (umlauts folded)."""
    words = set(re.findall(r"[a-z]{4,}", enrich._fold(job.get("title") or "")))
    if not words:
        return False
    tokens = set(re.findall(r"[a-z0-9]+", enrich._fold(text or "")))
    return sum(w in tokens for w in words) * 2 >= len(words)


def _company_on_page(text, job):
    """The first distinctive word of the company name (no legal form, no brackets) is on the page as a whole word.
    The full job-record name is often longer than the page's own name ('NAVAX Consulting' vs 'NAVAX')."""
    name = re.sub(r"\([^)]*\)", " ", job.get("company") or "")
    key = [t for t in re.findall(r"[a-z0-9]+", enrich._fold(name)) if t not in LEGAL]
    if not key:
        return False
    return key[0] in re.findall(r"[a-z0-9]+", enrich._fold(text or ""))


def _extract(doc, job):
    """{text, label, heads, page, posted, valid_through}. JSON-LD wins when its text is long enough or longer than
    the HTML text; else the HTML text (<main>/<article> when long enough, else the whole page). heads is what
    the title check reads: <title>, <h1> and the JSON-LD title; page is all visible text."""
    closable = frozenset(t for t in SKIP_IF_CLOSED if re.search(rf"</{t}\s*>", doc, re.I))
    vis = _Visible(closable)
    vis.feed(doc)
    vis.close()
    jl = _from_jsonld(doc, job)
    main = _tidy("".join(vis.main))
    html_text = _start_at_title(main if len(main) >= MIN_CHARS else _tidy("".join(vis.parts)), job)
    heads = " ".join([vis.title] + vis.h1 + ([jl["title"]] if jl else []))
    page = "".join(vis.parts) + " " + (jl["text"] if jl else "")
    dates = {"posted": jl["posted"] if jl else "", "valid_through": jl["valid_through"] if jl else ""}
    if jl and (len(jl["text"]) >= MIN_CHARS or len(jl["text"]) >= len(html_text)):
        return dict(text=jl["text"], label="ok:jsonld", heads=heads, page=page, **dates)
    return dict(text=html_text, label="ok:html", heads=heads, page=page, **dates)


def _judge(text, page, job, multi_tenant):
    """'' when the text is a posting for this job, else the reason. A closed phrase gives 'closed phrase': the
    text is dropped, but the job is not closed on it (only http 410 and an expired validThrough close a job)."""
    if CLOSED.search(page["heads"] + " " + text[:200]):
        return "closed phrase"
    if len(text) < MIN_CHARS:
        return "too short"
    if not _title_match(page["heads"], job):
        return "no title match"
    if multi_tenant and not _company_on_page(page["heads"] + " " + page["page"], job):
        return "no company match"
    return ""


def fetch_posting_info(url, job, timeout=20):
    """dict: text (posting, up to 8000 chars, or ''), reason ('ok:jsonld', 'ok:html' or why not), posted (ISO date
    from JSON-LD datePosted or ''), valid_through (ISO date from JSON-LD validThrough or ''), closed (True only
    for 'http 410' and 'expired (validThrough)'). Never raises: enrich calls this in worker threads."""
    info = {"text": "", "reason": "", "posted": "", "valid_through": "", "closed": False}

    def done(reason):
        info["reason"] = reason
        info["closed"] = reason in CLOSE_REASONS
        return info

    try:
        reason = _refusal(url, job)
        if reason:
            return done(reason)
        final, ctype, charset, body = _open_page(url, job, timeout)
        reason = _refusal(final, job)  # the redirect guard saw the hops; this is the last word on where we landed
        if reason:
            return done(reason)
        if ctype != "text/html":
            return done("not html")
        page = _extract(_decode(body, charset or _meta_charset(body)), job)
        info["posted"], info["valid_through"] = page["posted"], page["valid_through"]
        if info["valid_through"] and datetime.date.fromisoformat(info["valid_through"]) < _today():
            return done("expired (validThrough)")
        multi = enrich.registrable_domain(urlsplit(final).hostname or "") in ATS
        why = _judge(page["text"], page, job, multi)
        if why:
            return done(why)
        info["text"] = store.scrub(page["text"])[:DESC_LIMIT]
        return done(page["label"])
    except _Refused as e:
        return done(e.reason)
    except _PageTimeout:
        return done("error: TimeoutError")
    except urllib.error.HTTPError as e:  # 404, 410 and the rest: the reason is the exact status
        return done(f"http {e.code}")
    except urllib.error.URLError as e:  # urllib wraps a socket timeout: report the inner error
        inner = e.reason if isinstance(e.reason, Exception) else e
        return done(f"error: {type(inner).__name__}")
    except Exception as e:
        return done(f"error: {type(e).__name__}")


def fetch_description(url, job, timeout=20):
    """(text, reason) - the thin form of fetch_posting_info."""
    info = fetch_posting_info(url, job, timeout)
    return info["text"], info["reason"]
