"""Companies' own career pages (wave 2, task C): one generic 'site' adapter, configured per company in companies.json.

Each company names its source in its config (research/wave2_feeds_C.json, merged into companies.json):
- json_feed - a JSON list the page itself loads (a1, Microsoft); when set, it is the source;
- otherwise the job-list page (feed_url) is read: first schema.org JobPosting in JSON-LD, then links whose absolute
  URL matches link_pattern, with title and place taken from the card around the link.
A page that gives no job at all raises: an empty answer from a page that should list jobs would close them all.
Every request goes through fetch_posting's guards: DENY and allowed hosts, public address only, robots.txt of
job-digest-bot, every redirect hop checked, page deadline, size cap. A company gets 6 requests and 30 s by default
(max_requests may raise that, to 12 requests and 60 s at most).
"""

import html
import json
import urllib.error
import re
import time
import urllib.request
from urllib.parse import urljoin, urlsplit, urlunsplit

import enrich
import fetch_posting as fp
from fetch_companies import Feed, flat, job, text

TIMEOUT = 20          # seconds for one request; fetch_posting's 30 s page deadline applies on top
MAX_REQUESTS = 6      # requests per company per run by default; a company may ask for more, up to the ceiling
MAX_REQUESTS_CEILING = 12
SECONDS_PER_REQUEST = 5   # the seconds limit scales with the requests: 6 requests = 30 s
SECONDS_CEILING = 60
MAX_JSON = 4_000_000  # a job list in JSON is far smaller; more than this is not read
HEADING = re.compile(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]\s*>", re.I | re.S)
# An anchor: its attributes may hold quoted text with '>' (an onclick with an arrow function), and href may be quoted or bare.
_ATTR = r"(?:[^>\"']|\"[^\"]*\"|'[^']*')"
LINK = re.compile(r"(?P<open><a\b" + _ATTR + r"*?\shref\s*=\s*(?:\"(?P<dq>[^\"#][^\"]*)\"|'(?P<sq>[^'#][^']*)'"
                  r"|(?P<bare>[^\s\"'>#][^\s>]*))" + _ATTR + r"*>)(?P<inner>.*?)</a\s*>", re.I | re.S)
TITLE_ATTR = re.compile(r"\stitle\s*=\s*[\"']([^\"']+)[\"']", re.I)
CARD_MAX = 3000       # characters read after a link when looking for its title or place (location_after)
FIRST_CARD_MAX = 50_000  # characters read before the first link of a list: a card's text runs back to the previous card
# Link texts that name no job ('Details ansehen' on every card): the title then comes from the link's title attribute.
GENERIC = frozenset(("details ansehen", "details", "details anzeigen", "mehr erfahren", "mehr", "mehr infos",
                     "learn more", "read more", "more", "more info", "apply", "jetzt bewerben", "bewerben",
                     "zur stelle", "zur ausschreibung", "weiterlesen", "view job", "view more", "details & apply", "jobdetails anzeigen",
                     "open", "offen", "see more"))
# An unsolicited application is not a vacancy.
UNSOLICITED = re.compile(r"initiativ|unsolicited|blindbewerbung", re.I)


RETRY_DEFAULT = 10   # seconds to wait after a 429 that names no Retry-After
RETRY_MAX = 20       # longest wait after a 429, whatever Retry-After says


class SiteError(ValueError):
    """Why a company's page could not be read. Raised, so the run keeps the company's jobs instead of closing them."""


class _Budget:
    """Requests and seconds left for one company. Paging stops quietly when the budget ends (the feed is then capped).
    max_requests is the company's own value (default 6, ceiling 12); the seconds limit is 5 per request, ceiling 60."""

    def __init__(self, max_requests=None):
        self.started = time.monotonic()
        self.used = 0
        self.max_requests = max(1, min(MAX_REQUESTS_CEILING, int(max_requests or MAX_REQUESTS)))
        self.seconds = min(SECONDS_CEILING, SECONDS_PER_REQUEST * self.max_requests)

    def ok(self):
        return self.used < self.max_requests and self.remaining() > 0

    def remaining(self):
        return self.seconds - (time.monotonic() - self.started)

    def timeout(self):
        """Socket timeout for the next read: the default, cut to the time left in this company's budget."""
        return max(0.1, min(TIMEOUT, self.remaining()))

    def spend(self):
        if not self.ok():
            raise SiteError("request or time limit per company reached")
        self.used += 1


def _html(url, guard, budget):
    """(final_url, text) of an HTML page, read through fetch_posting's guards. Raises SiteError otherwise."""
    budget.spend()
    reason = fp._refusal(url, guard)
    if reason:
        raise SiteError(f"not read: {reason}")
    try:
        final, ctype, charset, body = fp._open_page(url, guard, budget.timeout())
    except fp._Refused as e:
        raise SiteError(f"not read: redirect to {e.reason}") from e
    if body is None or ctype != "text/html":
        raise SiteError(f"not an HTML page: {ctype}")
    reason = fp._refusal(final, guard)
    if reason:
        raise SiteError(f"not read: final page {reason}")
    return final, fp._decode(body, charset or fp._meta_charset(body))


def _retry_wait(err):
    """Seconds to wait after a 429: its Retry-After when that is a number of seconds, else RETRY_DEFAULT; at most RETRY_MAX."""
    try:
        wait = float(err.headers.get("Retry-After"))
    except (TypeError, ValueError):
        wait = RETRY_DEFAULT
    return min(RETRY_MAX, max(0.0, wait))


def _json(url, guard, budget, body=None):
    """Parsed JSON of one GET (or POST with a JSON body, when the page's own list is loaded that way) through the same
    guards as _html: refusal check, hop-by-hop redirect check, deadline and size cap. fetch_posting only opens HTML,
    so this is the JSON twin of its _open_page. No credentials or same-origin headers are sent. A 429 is retried once
    after its Retry-After (see _retry_wait), if the company's time allows."""
    budget.spend()
    reason = fp._refusal(url, guard)
    if reason:
        raise SiteError(f"json not read: {reason}")
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"User-Agent": fp.UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in (1, 2):
        fp._polite(urlsplit(url).netloc)
        deadline = fp._clock() + fp.DEADLINE
        opener = urllib.request.build_opener(fp._RedirectGuard(guard, deadline))
        req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
        try:
            with opener.open(req, timeout=budget.timeout()) as resp:
                final, ctype = resp.geturl(), resp.headers.get_content_type()
                body_bytes = fp._read_capped(resp, deadline, MAX_JSON)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            wait = _retry_wait(e)
            if wait >= budget.remaining():
                raise SiteError("429 and no time left for a retry") from e
            time.sleep(wait)
    reason = fp._refusal(final, guard)
    if reason:
        raise SiteError(f"json not read: final {reason}")
    if "json" not in ctype:
        raise SiteError(f"json endpoint answered {ctype}")
    return json.loads(body_bytes.decode("utf-8", "replace"))


def _dig(data, path):
    """Value at a dotted path ('data.positions'); '' gives the value itself; a missing key gives None."""
    for key in (path or "").split("."):
        if not key:
            continue
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _url(c, url):
    """The job URL as the company's stored rows have it: url_rewrite [regex, replacement], applied once. Used when the
    portal changes its link form (UniCredit's /en_GB/ segment): rows stored before the change keep matching."""
    rule = c.get("url_rewrite")
    return re.sub(rule[0], rule[1], url, count=1) if rule and url else url


def _keep(j):
    """A job we can store: a title, a link, not an unsolicited application, not on a job board (DENY)."""
    if not j or not j["title"] or not j["url"] or UNSOLICITED.search(j["title"] + " " + j["url"]):
        return False
    if len(re.sub(r"\W", "", j["title"])) < 3:  # a fragment of markup, not a title
        return False
    return enrich.registrable_domain(urlsplit(j["url"]).hostname or "") not in fp.DENY


def _postings(obj):
    """Every schema.org JobPosting in a JSON-LD value, at any depth (ItemList, @graph, nested item)."""
    if isinstance(obj, list):
        for x in obj:
            yield from _postings(x)
    elif isinstance(obj, dict):
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(isinstance(t, str) and t.split("/")[-1] == "JobPosting" for t in types):
            yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _postings(v)


def _ld_location(node):
    """'Wien, Wien, AT' from JSON-LD jobLocation (address parts); 'Remote' for a telecommute posting."""
    places = node.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    parts = []
    for p in places:
        addr = p.get("address") if isinstance(p, dict) else None
        if isinstance(addr, dict):
            parts.append(", ".join(flat(addr.get(k)) for k in ("addressLocality", "addressRegion", "addressCountry")
                                   if flat(addr.get(k)).strip()))
    if node.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    return "; ".join(p for p in parts if p)


def _from_jsonld(doc, c, page_url):
    """(jobs, bad): every JobPosting on the list page, skipped and counted when it has no title or link."""
    out, bad, seen = [], 0, set()
    for block in fp.JSONLD.findall(doc):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        for node in _postings(data):
            link = flat(node.get("url") or "")
            j = job(c, flat(node.get("title")), _ld_location(node) or c.get("default_location", ""),
                    _url(c, urljoin(page_url, link)) if link else "", node.get("description") or "",
                    flat(node.get("datePosted")), flat(node.get("employmentType")))
            if not _keep(j) or j["url"] in seen:
                bad += 1
                continue
            seen.add(j["url"])
            out.append(j)
    return out, bad


def _plain(fragment):
    """Text of an HTML fragment with a '|' where each element starts or ends, so a regex can stop at a cell edge."""
    fragment = re.sub(r"<(script|style|svg)\b[^>]*>.*?</\1\s*>", " ", fragment, flags=re.I | re.S)
    fragment = html.unescape(re.sub(r"<[^>]+>", " | ", fragment))
    return re.sub(r"(\s*\|\s*)+", " | ", re.sub(r"\s+", " ", fragment)).strip(" |")


def _place(s):
    """'Graz | Graz | Munich' -> 'Graz, Munich': the places of one card, each once."""
    seen = []
    for part in re.split(r"\s*\|\s*", s or ""):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return ", ".join(seen)


def _link_records(doc, c, page_url):
    """{url: (title, place)} for each link on one list page whose absolute URL matches link_pattern. The title is the
    link text; when that is generic ('Learn more', 'View More') it is the last match of title_pattern in the card, else
    the link's title attribute. A card is the text since the previous link to a different URL, or, with location_after,
    the text after this link up to the next link to another URL (a title link and its 'View More' link share a URL).
    location_pattern gives the place: the last match before the link, or the first match after it (location_after).
    Regexes run on _plain text. The first real title and place of a URL are kept."""
    pat = re.compile(c["link_pattern"])
    title_re = re.compile(c["title_pattern"]) if c.get("title_pattern") else None
    loc_re = re.compile(c["location_pattern"]) if c.get("location_pattern") else None
    hits = []
    for m in LINK.finditer(doc):
        url = urljoin(page_url, html.unescape((m.group("dq") or m.group("sq") or m.group("bare")).strip()))
        if pat.search(url):
            hits.append((m, url))
    found = {}
    for i, (m, url) in enumerate(hits):
        prev = next((h.end() for h, u in reversed(hits[:i]) if u != url), None)
        start = prev if prev is not None else max(0, m.start() - FIRST_CARD_MAX)
        before = doc[start: m.end()]
        nxt = next((h.start() for h, u in hits[i + 1:] if u != url), None)
        after_end = min(nxt if nxt is not None else m.end() + CARD_MAX, m.end() + CARD_MAX)
        close = doc.find("</li>", m.end(), after_end)  # the card of a list item ends there; the last card must not read the footer
        after = doc[m.end(): close if close != -1 else after_end]
        side = _plain(after if c.get("location_after") else before)  # title_pattern and location_pattern read one side
        heading = HEADING.search(m.group("inner"))  # a card that puts the title in a heading inside the link (check24)
        title = text(heading.group(1) if heading else m.group("inner"), 300)
        if not title or title.lower() in GENERIC:
            found_title = title_re.findall(side) if title_re else []
            attr = TITLE_ATTR.search(m.group("open"))
            title = text(found_title[-1], 300) if found_title else (text(attr.group(1), 300) if attr else "")
        clean = re.search(c["title_clean"], title) if c.get("title_clean") and title else None
        if clean and clean.group(1).strip():  # e.g. 'J / 005 Senior Digital Designer (f/m/d) Remote' -> the job title
            title = clean.group(1).strip()
        rec = found.setdefault(url, {"title": "", "location": ""})
        if title and title.lower() not in GENERIC and not rec["title"]:
            rec["title"] = title
        if loc_re and not rec["location"]:
            places = loc_re.findall(side)
            if places:
                rec["location"] = _place(text(places[0] if c.get("location_after") else places[-1], 200))
    return found


def _jobs_from_records(c, found):
    """(jobs, bad) from _link_records: a card with no place of its own is bad when place_required is set."""
    out, bad = [], 0
    for url, rec in found.items():
        place = rec["location"] or c.get("default_location", "")
        if c.get("place_required") and not place:  # a card that names no place of its own is not kept
            bad += 1
            continue
        j = job(c, rec["title"], place, _url(c, url))
        if _keep(j):
            out.append(j)
        else:
            bad += 1
    return out, bad


def _from_links(doc, c, page_url):
    """(jobs, bad) of one list page: see _link_records and _jobs_from_records."""
    return _jobs_from_records(c, _link_records(doc, c, page_url))


def _stated(doc, c):
    """The count the list page states, or None. count_pattern's group 'stated' (or group 1) is the total."""
    if not c.get("count_pattern"):
        return None
    m = re.search(c["count_pattern"], _plain(doc))
    if not m:
        return None
    return int(m.group("stated") if "stated" in m.groupdict() else m.group(1))


def _read_link_pages(doc, c, page_url, guard, budget):
    """(jobs, bad, complete) for a link list: the first page, then with list_pages {url: template with {offset}, step}
    the next pages, until the count the first page states is reached. A page with no new link before that count is not
    the end: capped. Without a stated count only the first page is read, and the feed is capped."""
    found = _link_records(doc, c, page_url)
    stated = _stated(doc, c)
    spec = c.get("list_pages")
    if spec and stated is not None:
        step = int(spec["step"])
        offset = step
        while len(found) < stated and budget.ok():
            final, page_doc = _html(spec["url"].replace("{offset}", str(offset)), guard, budget)
            new = {u: r for u, r in _link_records(page_doc, c, final).items() if u not in found}
            if not new:
                break
            found.update(new)
            offset += step
    jobs, bad = _jobs_from_records(c, found)
    return jobs, bad, stated is not None and len(found) == stated


def _with_param(url, name, value):
    """The URL with one query parameter set to value. The rest of the query is kept as the company wrote it
    (Vienna%2C%20Austria stays so), only the paging parameter moves to the end."""
    parts = urlsplit(url)
    kept = [q for q in parts.query.split("&") if q and q.split("=", 1)[0] != name]
    kept.append(f"{name}={value}")
    return urlunsplit(parts._replace(query="&".join(kept)))


def _from_json(c, guard, budget):
    """(jobs, bad, complete) from the company's json_feed. With page_param the endpoint is paged: page_start (default 0)
    and page_step are added to the parameter each time. Complete only when the total is reached, or, with no total,
    when a page is shorter than page_size. An empty page before the total is not the end: capped (never complete).
    The budget ending mid-way is capped too. Without page_param the endpoint is read once."""
    spec = c["json_feed"]
    url, param, step = spec["url"], spec.get("page_param"), int(spec.get("page_step") or 0)
    out, bad, seen, value, got, total = [], 0, set(), int(spec.get("page_start") or 0), 0, None
    while True:
        data = _json(_with_param(url, param, value) if param else url, guard, budget, spec.get("body"))
        items = _dig(data, spec.get("items"))
        if not isinstance(items, list):
            raise SiteError(f"json_feed: no list at '{spec.get('items')}'")
        got += len(items)
        page_total = _dig(data, spec.get("total"))
        if total is None and isinstance(page_total, int):
            total = page_total  # the first page states the total; a later page's 0 or missing total is no news
        reached = isinstance(total, int) and got >= total  # every item of the list is read: not capped
        for it in items:
            if not isinstance(it, dict):
                bad += 1
                continue
            link = flat(_dig(it, spec.get("link") or "url"))
            loc = flat(_dig(it, spec.get("location") or "location"))
            if spec.get("country"):  # the endpoint is filtered to this country; a town alone is not always named in AT
                loc = f"{loc}, {spec['country']}" if loc else spec["country"]
            j = job(c, _dig(it, spec.get("title") or "title"), loc or c.get("default_location", ""),
                    _url(c, urljoin(url, link)) if link else "", "", _dig(it, spec["posted"]) if spec.get("posted") else "")
            if not _keep(j) or j["url"] in seen:
                bad += 1
                continue
            seen.add(j["url"])
            out.append(j)
        # no total: a page shorter than page_size is the last one (the only complete case without a total)
        short = total is None and bool(spec.get("page_size")) and len(items) < int(spec["page_size"])
        if not param:
            return out, bad, reached or short
        value += step
        if reached or short:
            return out, bad, True
        if not items or not budget.ok():  # an empty page before the total is not the end: capped, never complete
            return out, bad, False


def site(c):
    """Adapter 'site'. Company keys (companies.json or research/wave2_feeds_C.json):
    feed_url - the job-list page (https). Read for JSON-LD and links; not read when json_feed is set.
    json_feed - dict for a JSON list the page calls: url; items - dotted path to the list; title, location, link -
    dotted keys inside one item (defaults 'title', 'location', 'url'); country - appended to each location (the
    endpoint is already filtered to that country); posted - optional date key; total - dotted path to the full
    count; page_param, page_start and page_step - paging (e.g. 'start', 0, 10). When set, it is the source.
    link_pattern - regex on absolute link URLs; title_pattern and location_pattern - regexes for the card around a
    link (the text before it, or after it with location_after: true), last match wins. Needed when the link text is
    generic ('Learn more') or shows no place.
    default_location - used only when the source gives no location (the company hires only in that place).
    place_required - True: a card that names no place of its own (and has no default_location) is counted as bad.
    count_pattern - regex with the count the list page states (group 'stated', or group 1); the feed is complete only
    when the cards read (vacancies plus skipped ones, over all pages) equal that count. No count: capped.
    list_pages - {url, step}: further pages of a link list; url is a template with {offset}, offsets step, 2*step, ...
    page_size - for a json_feed without total (see there). A job without location is dropped later by is_austrian().
    max_requests - requests this company may take per run (default 6, ceiling 12; the seconds limit is 5 per request,
    ceiling 60). title_clean - regex whose group 1 replaces the title (e.g. a numbering prefix).
    json_feed.method 'POST' with json_feed.body - for a list the page loads by POST (the body is sent as JSON)."""
    budget = _Budget(c.get("max_requests"))
    guard = {"company": c["name"], "company_id": c["id"], "title": ""}
    if c.get("json_feed"):
        jobs, bad, complete = _from_json(c, guard, budget)
        source = "json"
    else:
        page_url, doc = _html(c["feed_url"], guard, budget)
        jobs, bad = _from_jsonld(doc, c, page_url)
        complete, source = False, "jsonld"
        if not jobs and c.get("link_pattern"):
            jobs, bad, complete = _read_link_pages(doc, c, page_url, guard, budget)
            source = "links"
    if not jobs:
        raise SiteError(f"no vacancies found ({source}; the page or list may be empty or changed)")
    feed = Feed(jobs, capped=not complete, bad=bad)
    feed.source = source  # which source gave the jobs; the research run reports it
    feed.requests = budget.used
    return feed


ADAPTERS = {"site": site}
