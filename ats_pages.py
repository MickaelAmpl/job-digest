"""Page-based job systems for wave 2 (task B2, Stage 7): Phenom, Prospective, Oracle Recruiting Cloud, softgarden,
onlyfy and the AEM job-results list of a company's own site. Each adapter reads the system's own data first (an
embedded JSON blob, a JSON feed, a REST list) and parses the job cards of the results page only when no structured
source exists. Every read goes through robots.txt (fetch_posting's rules) and a 1 s gap per host, and one adapter call
has COMPANY_SECONDS of wall-clock time for all its reads. A read that runs out of time ends the feed as capped, so a
slow morning never closes jobs."""

import functools
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import enrich
import fetch_posting
from fetch_companies import UA, Feed, is_austrian, job, records

MAX_PAGES = 5           # page reads per company; with the robots.txt read that is 6 requests in one morning run
GAP = 1.0               # seconds between two requests to one host (onlyfy asks for Crawl-delay 1)
COMPANY_SECONDS = 60    # wall-clock limit of one adapter call: every page, every robots read, every gap
REQUEST_SECONDS = 40    # socket timeout of one page read; less when the company's time is nearly used up
MAX_BYTES = 40_000_000  # the largest page or feed read
PHENOM_SIZE = 100       # jobs per Phenom read; the embedded list is 10 jobs unless the page is asked for more
_clock = time.monotonic  # tests replace it with a fake clock
_LAST = {}              # host -> time of its last request, for the gap above
_deadline = None        # the running adapter call's time limit, set by _limited()

ROW_SOFTGARDEN = re.compile(r'<div class="matchElement" id="job_id_(\d+)">(.*?)(?=<div class="matchElement" id="job_id_|'
                            r'<div class="pagination)', re.S)
CARD_ONLYFY = re.compile(r'(<a\b[^>]*data-testid="job-card"[^>]*>)(.*?)</a>', re.S)
ITEM_AEM = re.compile(r'<a href="([^"]+)" class="job-results__result-item[^"]*">(.*?)</a>', re.S)


class OutOfTime(Exception):
    """The adapter call's time limit has passed: the read stops and the feed is capped (never a closing list)."""


def _remaining():
    """Seconds left in the running adapter call (a full REQUEST_SECONDS outside one)."""
    return REQUEST_SECONDS if _deadline is None else _deadline - _clock()


def _limited(fn):
    """One adapter call gets COMPANY_SECONDS of wall-clock time. A call that still runs out (single read, nothing
    fetched yet) returns an empty capped Feed: it adds nothing and closes nothing."""
    @functools.wraps(fn)
    def run(c):
        global _deadline
        _deadline = _clock() + COMPANY_SECONDS
        try:
            return fn(c)
        except OutOfTime:
            return Feed([], capped=True)
        finally:
            _deadline = None
    return run


class _Hop(urllib.request.HTTPRedirectHandler):
    """A redirect target is checked as fetch_posting checks a page: http(s) only, not a banned host (DENY), and every
    address it resolves to is public. A refused hop raises, so the company's read fails and keeps its jobs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise urllib.error.URLError(f"redirect refused: {newurl}")
        if enrich.registrable_domain(parsed.hostname) in fetch_posting.DENY:
            raise urllib.error.URLError(f"redirect refused (banned host): {newurl}")
        try:
            public = fetch_posting._public_host(parsed)
        except OSError:
            public = False
        if not public:
            raise urllib.error.URLError(f"redirect refused (not a public address): {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url, accept, timeout):
    """One GET with the project's User-Agent, the given socket timeout and the redirect check above."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.build_opener(_Hop()).open(req, timeout=timeout) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"page larger than {MAX_BYTES // 1_000_000} MB")
    return data.decode("utf-8", "replace")


def _read(url, accept="text/html"):
    """One read inside the company's time limit: time left is checked first, then robots.txt (fetch_posting's rules),
    then the 1 s gap to the same host, then the page with timeout = min(REQUEST_SECONDS, time left). A disallowed page
    raises PermissionError; a read past the limit raises OutOfTime. An uncached robots.txt read may take up to
    fetch_posting.DEADLINE, so it is not started with less time than that."""
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if _remaining() <= 0:
        raise OutOfTime(url)
    if base not in fetch_posting._ROBOTS and _remaining() < fetch_posting.DEADLINE:
        raise OutOfTime(url)
    if not fetch_posting._robots_ok(url):
        raise PermissionError(f"robots.txt disallows {url}")
    last = _LAST.get(parts.netloc)
    if last is not None and GAP - (_clock() - last) > 0:
        wait = GAP - (_clock() - last)
        if wait >= _remaining():
            raise OutOfTime(url)
        time.sleep(wait)
    if _remaining() <= 0:
        raise OutOfTime(url)
    _LAST[parts.netloc] = _clock()
    return _fetch(url, accept, min(REQUEST_SECONDS, _remaining()))


def _page(url, accept="text/html"):
    """_read for a paging loop: None once the company's time is up, so the loop stops and the feed is capped."""
    try:
        return _read(url, accept)
    except OutOfTime:
        return None


def _with_query(url, params):
    """The URL with these query parameters set (replacing any old value of the same name)."""
    parts = urllib.parse.urlsplit(url)
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    kept += [(k, str(v)) for k, v in params.items()]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(kept)))


def _austria_first(places):
    """Places with the Austrian ones first. text() keeps only 200 characters of a location, and a job open in
    Munich and Vienna must still show 'Vienna' inside that cut."""
    places = [p for p in places if p and p.strip()]
    return sorted(places, key=lambda p: not is_austrian(p))


def _json_after(page, marker):
    """The JSON value that follows a JavaScript assignment like 'phApp.ddo = {...};' in a page."""
    i = page.find(marker)
    if i < 0:
        raise ValueError(f"'{marker.strip()}' not on the page (layout changed?)")
    return json.JSONDecoder().raw_decode(page[i + len(marker):])[0]


def _strip(fragment):
    """Visible text of an HTML fragment, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


# ---------- adapters ----------
# A paging loop trusts the total of its FIRST page only: a later page may report 0 (Workday does), and an empty
# page before the total ends the read as capped, never as complete.

@_limited
def phenom(c):
    """Phenom career sites (Allianz, BCG, Mastercard). feed_url = the search-results page with the country facet,
    e.g. https://careers.allianz.com/global/en/search-results?qcountry=Austria. The first results come embedded as
    JSON (phApp.ddo, eagerLoadRefineSearch); the next ones with from=<jobs read so far>. A job's page is
    <site>/<locale>/job/<jobId>. The location is the job's full place list, Austrian places first."""
    feed_url = c["feed_url"]
    prefix = urllib.parse.urlsplit(feed_url).path.split("/search-results")[0]
    base = f"{urllib.parse.urlsplit(feed_url).scheme}://{urllib.parse.urlsplit(feed_url).netloc}{prefix}"
    # size=100 returns a whole board in one read (BCG: 41 jobs). Without it the order shifts between reads and
    # pages repeat or skip jobs, so 'from' paging is only the fallback for boards above 100 jobs.
    found, total, timed_out = {}, None, False
    url = _with_query(feed_url, {"size": PHENOM_SIZE})
    for _ in range(MAX_PAGES):
        page = _page(url)
        if page is None:
            timed_out = True
            break
        ers = _json_after(page, "phApp.ddo = ").get("eagerLoadRefineSearch") or {}
        data = ers.get("data") or {}
        if not isinstance(ers.get("totalHits"), int) or not isinstance(data.get("jobs"), list):
            raise ValueError("Phenom page without a job list (layout changed?)")
        if total is None:
            total = ers["totalHits"]
        for j in data["jobs"]:
            found.setdefault(str(j.get("jobId") or j.get("reqId")), j)
        if not data["jobs"] or len(found) >= total:
            break
        url = _with_query(feed_url, {"size": PHENOM_SIZE, "from": len(found)})

    def build(j):
        jid = j.get("jobId") or j.get("reqId")
        places = j.get("multi_location") or [j.get("cityStateCountry") or ""]
        return job(c, j.get("title", ""), "; ".join(_austria_first(places)), f"{base}/job/{jid}",
                   j.get("descriptionTeaser", ""), j.get("postedDate", ""), j.get("type", ""))
    return records(c, list(found.values()), build, capped=timed_out or total is None or len(found) < total)


@_limited
def prospective(c):
    """Prospective job widget (Helvetia). feed_url = the widget's JSON list with the Austria facet, e.g.
    https://ohws.prospective.ch/public/v1/medium/1002532/jobs?lang=de&offset=0&limit=100&f=20:1192756 (16 jobs; the
    unfiltered list has 166 across CH, FR, AT). Attribute '20' holds the country, '20_<id>' the towns; both go into
    the location so the country word is there for is_austrian. Read in offset pages until 'total'."""
    feed_url = c["feed_url"]
    items, total, pages, timed_out = [], None, 0, False
    while True:
        text_page = _page(_with_query(feed_url, {"offset": len(items)}), "application/json")
        if text_page is None:
            timed_out = True
            break
        d = json.loads(text_page)
        page = d.get("jobs") or []
        if not isinstance(d.get("total"), int) or not isinstance(page, list):
            raise ValueError("Prospective answer without 'total' or 'jobs' (layout changed?)")
        if total is None:
            total = d["total"]
        items += page
        pages += 1
        if not page or len(items) >= total or pages >= MAX_PAGES:
            break

    def build(j):
        a = j.get("attributes") or {}
        szas = j.get("szas") or {}
        towns = [v for k, vals in a.items() if k.startswith("20_") for v in vals]
        places = towns + list(a.get("20") or [])
        desc = " ".join(v for k, v in szas.items() if isinstance(v, str) and re.search(
            r"requirement|responsib|aufgab|anforder", k, re.I))
        return job(c, j.get("title", ""), ", ".join(places), szas.get("sza_apply_link", ""), desc)
    return records(c, items, build, capped=timed_out or total is None or len(items) < total)


@_limited
def oracle(c):
    """Oracle Recruiting Cloud career site (Ipsos). feed_url = the candidate site, e.g.
    https://ecqf.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/IpsosCareers, and extra key site_number
    = its 'CX_' number (the page's data-sitenumber). The public REST list (recruitingCEJobRequisitions) is read in
    pages of 50 newest first. Places are the primary and the secondary locations; a country code AT adds 'Österreich'."""
    feed_url = c["feed_url"]
    parts = urllib.parse.urlsplit(feed_url)
    api = f"{parts.scheme}://{parts.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    reqs, total, pages, timed_out = [], None, 0, False
    while True:
        url = (f"{api}?onlyData=true&expand=requisitionList.secondaryLocations"
               f"&finder=findReqs;siteNumber={c['site_number']},facetsList=LOCATIONS%3BWORK_LOCATIONS%3BTITLES"
               f"%3BCATEGORIES%3BORGANIZATIONS%3BPOSTING_DATES%3BFLEX_FIELDS,limit=50,offset={len(reqs)},"
               f"sortBy=POSTING_DATES_DESC")
        text_page = _page(url, "application/json")
        if text_page is None:
            timed_out = True
            break
        d = json.loads(text_page)
        item = (d.get("items") or [{}])[0]
        if not isinstance(item.get("TotalJobsCount"), int) or not isinstance(item.get("requisitionList"), list):
            raise ValueError("Oracle answer without TotalJobsCount or requisitionList (layout changed?)")
        if total is None:
            total = item["TotalJobsCount"]
        page = item["requisitionList"]
        reqs += page
        pages += 1
        if not page or len(reqs) >= total or pages >= MAX_PAGES:
            break

    def build(r):
        places = [r.get("PrimaryLocation") or ""] + [s.get("Name") or "" for s in r.get("secondaryLocations") or []]
        if r.get("PrimaryLocationCountry") == "AT":
            places.append("Österreich")
        return job(c, r.get("Title", ""), "; ".join(_austria_first(places)),
                   f"{feed_url.rstrip('/')}/job/{r.get('Id')}", r.get("ShortDescriptionStr", ""),
                   r.get("PostedDate", ""))
    return records(c, reqs, build, capped=timed_out or total is None or len(reqs) < total)


@_limited
def softgarden(c):
    """softgarden career sites, two generations. (a) feed_url ends in .json: the site's schema.org DataFeed
    (Volksbank Wien, jobs.volksbankwien.at/jobs.feed.json); numberOfItems says how many postings it holds.
    (b) any other feed_url: the older 'career board' list (Craftworks, <tenant>.softgarden.io/en/vacancies), parsed
    from its job rows, which have no structured source. The rows are the visible list; a pager with entries
    means the board goes on, so the feed is capped. A page without the list markup raises."""
    if c["feed_url"].endswith(".json"):
        return _softgarden_feed(c)
    return _softgarden_board(c)


def _softgarden_feed(c):
    d = json.loads(_read(c["feed_url"], "application/json"))
    items = d.get("dataFeedElement")
    if not isinstance(items, list) or not isinstance(d.get("numberOfItems"), int):
        raise ValueError("softgarden feed without dataFeedElement/numberOfItems (layout changed?)")

    def place(p):
        a = (p or {}).get("address") or {}
        return ", ".join(v for v in (a.get(k) for k in ("addressLocality", "addressRegion", "addressCountry"))
                         if v and v != "-")

    def build(e):
        p = e["item"]
        locs = p.get("jobLocation") or []
        locs = locs if isinstance(locs, list) else [locs]
        return job(c, p.get("title", ""), "; ".join(_austria_first([place(x) for x in locs])), p.get("url", ""),
                   p.get("description", ""), p.get("datePosted", ""), p.get("employmentType", ""))
    return records(c, items, build, capped=len(items) < d["numberOfItems"])


def _softgarden_board(c):
    page = _read(c["feed_url"])
    if 'id="outputContainer"' not in page and 'class="outputContainer"' not in page:
        raise ValueError("softgarden board without its job list (layout changed?)")
    rows = []
    for jid, body in ROW_SOFTGARDEN.findall(page):
        link = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
        if not link:
            continue
        places = [_strip(x) for x in re.findall(r'<span class="location-view-item">(.*?)</span>', body, re.S)]
        rows.append({"id": jid, "href": html.unescape(link.group(1)), "title": _strip(link.group(2)),
                     "location": "; ".join(_austria_first(places))})
    pager = re.search(r'id="job-search-pagination"[^>]*>(.*?)</ul>', page, re.S)
    capped = bool(pager and "<li" in pager.group(1))

    def build(r):
        return job(c, r["title"], r["location"], urllib.parse.urljoin(c["feed_url"], r["href"]))
    return records(c, rows, build, capped=capped)


def _empty_onlyfy(page):
    """onlyfy's empty board: its jobs-empty-state with the 'No job openings right now' announcement, and neither a
    job card nor a count on the page (a page with a card or a count is never read as empty)."""
    return ('data-testid="jobs-empty-state"' in page and "No job openings right now" in page
            and "pagination-items-count" not in page and not CARD_ONLYFY.search(page))


@_limited
def onlyfy(c):
    """onlyfy career sites (Magenta Telekom, Novomatic, Generali, wikifolio; jobbase widgets forward to onlyfy).
    feed_url = the career page's English board, e.g. https://magenta.onlyfy.jobs/en. Its job cards carry title,
    'place | employment | dd.mm.yyyy'; the total is in 'out of N jobs'. Pages come with ?page=N (10 per page).
    A card names the town only, so a town missing from fetch_companies.AUSTRIA_PLACE is not Austrian for the filter.
    A board with no jobs is onlyfy's own empty state (jobs-empty-state, 'No job openings right now', no card, no
    count): an empty, uncapped feed. Any other page without a count still raises."""
    feed_url = c["feed_url"]
    cards, total, pages, timed_out = [], None, 0, False
    while True:
        url = feed_url if pages == 0 else _with_query(feed_url, {"page": pages + 1})
        page = _page(url)
        if page is None:
            timed_out = True
            break
        m = re.search(r'pagination-items-count"[^>]*>(.*?)</span>', page, re.S)
        found = re.search(r"out of\s*(\d+)\s*jobs", _strip(m.group(1)) if m else "")
        if not found:
            if pages == 0 and _empty_onlyfy(page):
                total = 0  # an empty board, stated as such
                break
            raise ValueError("onlyfy page without 'out of N jobs' (layout changed?)")
        if total is None:
            total = int(found.group(1))
        batch = []
        for tag, body in CARD_ONLYFY.findall(page):
            href = re.search(r'href="([^"]+)"', tag)
            title = re.search(r'data-testid="job-title"[^>]*>(.*?)</h3>', body, re.S)
            info = re.search(r'data-testid="job-more-info"[^>]*>(.*?)</div>', body, re.S)
            if href and title:
                parts = [p.strip() for p in _strip(info.group(1)).split("|")] if info else []
                batch.append({"href": href.group(1), "title": _strip(title.group(1)), "parts": parts})
        cards += batch
        pages += 1
        if not batch or len(cards) >= total or pages >= MAX_PAGES:
            break

    def build(card):
        parts = card["parts"] + ["", "", ""]
        posted = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", parts[2])
        posted = f"{posted.group(3)}-{posted.group(2)}-{posted.group(1)}" if posted else ""
        href = urllib.parse.urljoin(feed_url, card["href"].split("?")[0])
        return job(c, card["title"], parts[0], href, "", posted, parts[1])
    return records(c, cards, build, capped=timed_out or total is None or len(cards) < total)


@_limited
def aem_results(c):
    """A company's own AEM job list (Wüstenrot: the open-positions page loads its list from the results fragment
    .../offene-stellen/_jcr_content/.../job_overview.results.html). Items are links with a title and the place;
    the total is 'numTotalResults'. Pages come with ?page=N."""
    feed_url = c["feed_url"]
    items, total, pages, timed_out = [], None, 0, False
    while True:
        url = feed_url if pages == 0 else _with_query(feed_url, {"page": pages + 1})
        page = _page(url)
        if page is None:
            timed_out = True
            break
        m = re.search(r'numTotalResults">\s*(\d+)\s*<', page)
        if not m:
            raise ValueError("AEM results without numTotalResults (layout changed?)")
        if total is None:
            total = int(m.group(1))
        batch = []
        for href, body in ITEM_AEM.findall(page):
            title = re.search(r'job-results__job-title">(.*?)</span>', body, re.S)
            place = re.search(r'job-results__workplace-location">(.*?)</span>', body, re.S)
            if title:
                batch.append({"href": href, "title": _strip(title.group(1)),
                              "place": _strip(place.group(1)) if place else ""})
        items += batch
        pages += 1
        if not batch or len(items) >= total or pages >= MAX_PAGES:
            break

    def build(r):
        return job(c, r["title"], r["place"], urllib.parse.urljoin(feed_url, html.unescape(r["href"])))
    return records(c, items, build, capped=timed_out or total is None or len(items) < total)


ADAPTERS = {"phenom": phenom, "prospective": prospective, "oracle": oracle, "softgarden": softgarden,
            "onlyfy": onlyfy, "aem_results": aem_results}
