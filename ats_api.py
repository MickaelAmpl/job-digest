"""Task B1 adapters (wave 2): the job systems whose list is JSON, or JSON/markup the careers page renders:
JOIN, Workday, eRecruiter, BambooHR, Personio boards (JSON), GoHire, Kula, Factorial. HiBob is not here: its job API answers 401 to any
plain HTTP client (it needs the page's own session), so it is marked unreadable. fetch_companies.main() merges ADAPTERS.
Each function takes a company dict and returns a Feed; a failed read raises, it never returns an empty Feed.
Systems that already have an adapter in fetch_companies (Personio, SuccessFactors, Teamtailor) are configured
there by feed_url only and are not in this file."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from fetch_companies import AT_CODE, AUSTRIA_PLACE, UA, Feed, day, flat, get, job, records, text

MAX_BYTES = 40_000_000  # the size cap fetch_companies.get() uses
WD_PAGE = 20          # the largest page Workday accepts (limit 100 is refused with HTTP 400)
WD_MAX_PAGES = 10     # 200 Austrian postings read in full; a bigger board is capped
WD_DEADLINE = 60      # seconds per company across all requests of one workday() call (QA minor 2)
ADAPTER_DEADLINE = 60  # the same limit for the adapters that make several requests (personio_json)
# Austrian places that Workday lists on their own and fetch_companies.AUSTRIA_PLACE does not know.
AT_EXTRA = re.compile(r"amstetten", re.I)


def _names_austria(s):
    """True when the text names Austria the way fetch_companies.is_austrian() reads it (Wien, Vienna, AT...)."""
    return bool(AUSTRIA_PLACE.search(s or "") or AT_CODE.search(s or ""))


def _at_name(s):
    """A Workday place value that is in Austria: the names is_austrian() knows, plus towns it does not."""
    return _names_austria(s) or bool(AT_EXTRA.search(s or ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Workday answers in place: a redirect is refused (HTTPError 3xx), never followed (QA minor 8)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _send(req, timeout):
    """One request through the no-redirect opener: the caller's timeout (at most 40 s), the size cap, and a 3xx
    reply raises instead of being followed."""
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("reply larger than 40 MB")
    return data


def _post_json(url, body, timeout=40):
    """POST for Workday's cxs API (fetch_companies.get is GET only, and it follows redirects)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers={
        "User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"})
    return json.loads(_send(req, timeout))


def _get(url, accept="application/json", timeout=40):
    """GET with a caller's timeout, for adapters that make several requests inside one deadline."""
    return _send(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept}), timeout)


def _origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# ---------- JOIN ----------

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def join(c):
    """JOIN (join.com/companies/<slug>). feed_url = the company page. The page's own state (Next.js
    __NEXT_DATA__ -> initialState.jobs) holds its open jobs; pagination.total is the full count. The state
    carries at most 5 jobs per page, so when total is larger than the items read the feed is capped.
    Job URL: join.com/companies/<slug>/<idParam>. The state is pageProps.pageData (current) or
    pageProps.initialState (older pages); a page with neither raises."""
    m = NEXT_DATA.search(get(c["feed_url"], "text/html").decode("utf-8", "replace"))
    if not m:
        raise ValueError("JOIN page has no __NEXT_DATA__ job state")
    pp = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    # JOIN's pages keep the job list under pageData since 30.10 and under initialState before; both are read.
    state = pp.get("pageData") or pp.get("initialState")
    jobs = state.get("jobs") if isinstance(state, dict) else None
    if not isinstance(jobs, dict):
        raise ValueError("JOIN page has neither pageData.jobs nor initialState.jobs")
    items, total = jobs.get("items") or [], (jobs.get("pagination") or {}).get("total")
    slug = urllib.parse.urlsplit(c["feed_url"]).path.strip("/").split("/")[-1]

    def build(j):
        city, country = j.get("city") or {}, j.get("country") or {}
        place = ", ".join(x for x in (city.get("cityName"), city.get("countryName"), country.get("iso3166")) if x)
        return job(c, j["title"], place, f"https://join.com/companies/{slug}/{j['idParam']}", "",
                   day(j.get("createdAt")), (j.get("employmentType") or {}).get("name", ""))
    capped = not (isinstance(total, int) and total == len(items))
    return records(c, items, build, capped=capped)


# ---------- Workday ----------

def _wd_austria_facet(facets):
    """The facet that keeps Austrian postings: the country value when the board has one, else the place values
    that name Austria (Cloudflight, Tricentis list cities). None when no facet names Austria."""
    groups = [g for f in facets or [] for g in (f.get("values") or [])
              if isinstance(g, dict) and g.get("facetParameter") in ("locationCountry", "locations")]

    def pick(param):
        return [v for g in groups if g["facetParameter"] == param for v in (g.get("values") or [])
                if isinstance(v, dict) and v.get("id") and _at_name(v.get("descriptor", ""))]
    country = pick("locationCountry")
    param, places = ("locationCountry", country) if country else ("locations", pick("locations"))
    return {param: [v["id"] for v in places]} if places else None


def _wd_location(s):
    """Workday writes '8 Locations' for multi-site jobs; the Austria facet already kept only jobs with an
    Austrian site, so such a job gets 'Austria' as its location."""
    s = (s or "").strip()
    if re.match(r"^\d+ Locations?$", s):
        s = ""
    if s and _names_austria(s):
        return s
    return f"{s}, Austria" if s else "Austria"


def _wd_posted(s):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' -> date; 'Posted 30+ Days Ago' -> '' (unknown)."""
    s, today = (s or "").lower(), date.today()
    if "today" in s:
        d = today
    elif "yesterday" in s:
        d = today - timedelta(days=1)
    else:
        m = re.search(r"posted (\d+) days? ago", s)
        if not m:
            return ""
        d = today - timedelta(days=int(m.group(1)))
    return d.isoformat()


def workday(c):
    """Workday career site (feed_url = https://<tenant>.wdN.myworkdayjobs.com/<site>). The site's own list calls
    POST /wday/cxs/<tenant>/<site>/jobs. One call with no filter gives the facets, the Austria facet is taken
    from them, then the postings are read 20 per page (offset moves by the postings actually read).
    Workday sends the total on offset 0 only: that first total is the target. Reading stops when the total is
    reached, a page comes back empty, WD_MAX_PAGES pages are read, or WD_DEADLINE seconds have passed; any stop
    before the total is capped=True, so a partial board never closes jobs. A board whose facets name no Austrian
    place is complete only when the board's first page states total 0 with no postings; any other board without an
    Austria facet is an empty capped feed (nothing closed)."""
    started = time.monotonic()

    def left():
        return WD_DEADLINE - (time.monotonic() - started)
    parts = urllib.parse.urlsplit(c["feed_url"])
    host, site = parts.netloc, parts.path.strip("/").split("/")[-1]
    api = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/jobs"
    head = _post_json(api, {"limit": 1, "offset": 0, "searchText": "", "appliedFacets": {}},
                      timeout=min(40, max(left(), 1)))
    facet = _wd_austria_facet(head.get("facets"))
    if facet is None:
        # No Austrian place among the facets: complete only when the board itself is empty (first page total 0,
        # no postings). Any other board without an Austria facet stays capped and closes nothing.
        empty = head.get("total") == 0 and not head.get("jobPostings")
        return Feed([], capped=not empty)
    posts, total, offset, pages = [], None, 0, 0
    while pages < WD_MAX_PAGES and left() > 0:
        time.sleep(1)  # one host: at least 1 s between requests
        if left() <= 0:
            break
        d = _post_json(api, {"limit": WD_PAGE, "offset": offset, "searchText": "", "appliedFacets": facet},
                       timeout=min(40, left()))
        pages += 1
        batch = d.get("jobPostings") or []
        if offset == 0 and isinstance(d.get("total"), int) and d["total"] >= len(batch):
            total = d["total"]  # a first page cannot promise fewer postings than it holds
        if not batch:
            break
        posts += batch
        offset += len(batch)
        if total is not None and offset >= total:
            break
    if total == 0 and not posts:
        return Feed([], capped=False)  # the facet is there and the board says nothing is open: a true zero
    return records(c, posts, lambda p: job(
        c, p.get("title", ""), _wd_location(p.get("locationsText", "")),
        f"https://{host}/{site}{p.get('externalPath', '')}", "", _wd_posted(p.get("postedOn", ""))),
        capped=total is None or offset < total)


# ---------- eRecruiter ----------

ER_TABLE_ROW = re.compile(r'<tr>\s*<td class="td-job-title">(.*?)</tr>', re.S)


def _er_date(s):
    """eRecruiter dates come as '10/22/2026' (JSON model) or '23.10.2026' (table)."""
    for fmt in ("%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(s or "").strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def erecruiter(c):
    """eRecruiter job boards (karriere.<bank>.at/Jobs and similar). Two page layouts, same system:
    (a) the page embeds the list as a JobList model (Jobs, TotalJobsCount): read from the model;
    (b) the page is a static table (td-job-title / td-location / td-date rows): read from the rows, checked
    against the page's own 'N Ergebnisse' count when it has one. Jobs with no location take default_location
    (set only for employers that hire in one Austrian city). Job URL: /Job/<Id> or the table's own link."""
    url = c["feed_url"]
    host = urllib.parse.urlsplit(url).netloc
    page = get(url, "text/html").decode("utf-8", "replace")
    i = page.find("new JobList(")
    if i >= 0:
        model, _ = json.JSONDecoder().raw_decode(page[page.index("{", i):])
        items, total = model.get("Jobs") or [], model.get("TotalJobsCount")
        return records(c, items, lambda j: job(
            c, j.get("Title", ""), j.get("Location") or c.get("default_location", ""),
            f"https://{host}/Job/{j.get('Id')}", "", _er_date(j.get("Date")), j.get("SubTitle", "")),
            capped=not (isinstance(total, int) and total == len(items)))
    rows = ER_TABLE_ROW.findall(page)
    if not rows:
        raise ValueError("eRecruiter page has neither a JobList model nor job rows")
    claimed = re.search(r"(\d+)\s*(?:Ergebnisse|results)", page)

    def build(r):
        a = re.search(r'<a href="([^"]*/Job/[^"]*)"[^>]*>(.*?)</a>', r, re.S)
        loc = re.search(r'<td class="td-location">(.*?)</td>', r, re.S)
        when = re.search(r'<td class="td-date">(.*?)</td>', r, re.S)
        return job(c, a.group(2), text(loc.group(1)) if loc else c.get("default_location", ""),
                   urllib.parse.urljoin(url, a.group(1)), "", _er_date(text(when.group(1)) if when else ""))
    return records(c, rows, build, capped=not (claimed and int(claimed.group(1)) == len(rows)))


# ---------- BambooHR ----------

def bamboohr(c):
    """BambooHR (<tenant>.bamboohr.com/careers/list): JSON, result = open postings, meta.totalCount = full count.
    Job URL: /careers/<id> (the share URL the list's own detail returns). Location from city/state/country."""
    host = urllib.parse.urlsplit(c["feed_url"]).netloc
    data = json.loads(get(c["feed_url"]))
    items, total = data.get("result") or [], (data.get("meta") or {}).get("totalCount")

    def build(j):
        loc, ats = j.get("location") or {}, j.get("atsLocation") or {}
        place = ", ".join(dict.fromkeys(x for x in (loc.get("city"), loc.get("state"), ats.get("country")) if x))
        return job(c, j.get("jobOpeningName", ""), place or c.get("default_location", ""),
                   f"https://{host}/careers/{j.get('id')}", "", "", j.get("employmentStatusLabel") or "")
    return records(c, items, build, capped=not (isinstance(total, int) and total == len(items)))


# ---------- Personio boards (JSON) ----------

PERSONIO_JOB = re.compile(r'href="(?:https://[^"/]+)?/job/(\d+)"')


def personio_json(c):
    """Personio job board (<tenant>.jobs.personio.com or .de). The board loads its open positions from GET
    /search.json: one object per position (id, name, office, offices, schedule). The board page links the same
    jobs as /job/<id>; when the two id sets differ the feed is capped, so a job missing from the JSON is never
    closed. Job URL: /job/<id>, the board's own link. Personio's XML feed is not served on these tenants (404),
    which is why the 'personio' adapter (XML) does not apply here."""
    started = time.monotonic()

    def left():
        return ADAPTER_DEADLINE - (time.monotonic() - started)
    origin = _origin(c["feed_url"])
    items = json.loads(_get(f"{origin}/search.json", timeout=min(40, left())))
    if not isinstance(items, list):
        raise ValueError("Personio search.json is not a list of positions")
    time.sleep(1)  # one host: at least 1 s between requests
    ids = {str(j.get("id")) for j in items if isinstance(j, dict)}
    shown = None  # no board page read in time: completeness is unknown, so capped
    if left() > 5:  # a board page needs a few seconds; less than that is not worth a request
        page = _get(f"{origin}/", "text/html", timeout=min(40, left()))
        shown = set(PERSONIO_JOB.findall(page.decode("utf-8", "replace")))

    def build(j):
        places = [o.replace("Office: ", "") for o in (j.get("offices") or []) if o]
        place = ", ".join(dict.fromkeys(places)) or (j.get("office") or "").replace("Office: ", "")
        return job(c, j.get("name", ""), place, f"{origin}/job/{j.get('id')}", "", "", j.get("schedule") or "")
    return records(c, items, build, capped=shown is None or shown != ids)


# ---------- GoHire ----------

GH_CARD = re.compile(r'<a class="gohire-job" href="([^"]+)">(.*?)</a>', re.S)


def _gh_date(s):
    """'Posted 22 October, 2026' -> '2026-10-22'."""
    s = re.sub(r"^\s*Posted\s+", "", s or "").strip()
    try:
        return datetime.strptime(s, "%d %B, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def gohire(c):
    """GoHire job board (jobs.gohire.io/<company>/): the page renders one card per job (a.gohire-job with
    h3.job-title, p.careers-location, p.date-posted). The page states 'N open jobs'; the list shows fewer when
    it pages (Cryptonow: 10 cards, 20 open jobs), so the feed is capped unless the cards match the count."""
    page = get(c["feed_url"], "text/html").decode("utf-8", "replace")
    cards = GH_CARD.findall(page)
    if not cards:
        raise ValueError("GoHire page has no job cards")
    claimed = re.search(r"(\d+)\s*(?:Job openings|open jobs)", page)

    def build(card):
        href, body = card
        t = re.search(r'job-title[^>]*>(.*?)</h3>', body, re.S)
        loc = re.search(r'careers-location">(.*?)</p>', body, re.S)
        posted = re.search(r'date-posted">(.*?)</p>', body, re.S)
        return job(c, t.group(1) if t else "", text(loc.group(1)) if loc else "", href, "",
                   _gh_date(text(posted.group(1)) if posted else ""))
    return records(c, cards, build, capped=not (claimed and int(claimed.group(1)) == len(cards)))


# ---------- Kula ----------

def kula(c):
    """Kula careers (careers.kula.ai/<company>): the job list is in the page HTML as Chakra cards, each with a
    /<company>/<id>-<slug> link, the title as the first paragraph and 'City, Country' as a later one. The page
    states no total, so the feed is always capped: it adds jobs and never closes one. robots.txt disallows /api/,
    so only the list page is read."""
    page = get(c["feed_url"], "text/html").decode("utf-8", "replace")
    origin = _origin(c["feed_url"])
    seen = {}
    for seg in page.split("chakra-card")[1:]:
        m = re.search(r'href="(/[^"]+/\d+-[^"]*)"', seg)
        if not m or m.group(1) in seen:
            continue
        paras = [text(p) for p in re.findall(r'<p class="chakra-text[^"]*">(.*?)</p>', seg, re.S)]
        seen[m.group(1)] = (paras[0] if paras else "", next((p for p in paras[1:] if "," in p), ""))
    if not seen:
        raise ValueError("Kula page has no job cards")
    return records(c, list(seen.items()), lambda it: job(c, it[1][0], it[1][1], origin + it[0]), capped=True)


# ---------- Factorial ----------

def factorial(c):
    """Factorial job board (<company>.factorial.com): the page renders one row per job (li.job-offer-item with
    data-job-postings-url and data-location-key such as 'city:wien' and a bold title). The page states no total,
    so the feed is always capped."""
    page = get(c["feed_url"], "text/html").decode("utf-8", "replace")
    items = []
    for row in page.split("job-offer-item")[1:]:
        u = re.search(r"data-job-postings-url='([^']+)'", row)
        if not u:
            continue
        key = re.search(r"data-location-key='([^']*)'", row)
        title = re.search(r'font-bold[^>]*>(.*?)</div>', row, re.S)
        place = key.group(1).split(":", 1)[1].replace("-", " ").title() if key and ":" in key.group(1) else ""
        items.append((u.group(1), title.group(1) if title else "", place))
    if not items:
        raise ValueError("Factorial page has no job rows")
    return records(c, items, lambda it: job(c, it[1], it[2], it[0]), capped=True)


# ---------- mokahr (BingX EU, Bitget EU) ----------

MOKA_API = "https://hire-r1.mokahr.com/api/outer/ats-apply/website/jobs"
MOKA_PAGE = 100       # postings per request (the largest page the board's own list asks for)
MOKA_MAX_PAGES = 5    # 500 postings read at most; a bigger board is capped
MOKA_ORG = re.compile(r"/social-recruitment/([^/]+)/(\d+)")


def mokahr(c):
    """Mokahr job board (hire-r1.mokahr.com/social-recruitment/<org>/<siteId>). feed_url is that careers page; the
    org and site ids are read from it. POST /api/outer/ats-apply/website/jobs with {orgId, siteId, limit, offset}
    answers {code: 0, data: {jobStats: {total}, jobs: [...]}}; a refusal answers code != 0 and raises. Only
    status 'open' postings are listed. The feed gives no location for BingX postings, so none is invented.
    Job link: the careers page with the posting's id as ?jobId= and as its fragment (the board is a single-page app)."""
    m = MOKA_ORG.search(c["feed_url"])
    if not m:
        raise ValueError("Mokahr feed_url is not a social-recruitment page")
    org, site = m.group(1), int(m.group(2))
    items, total, offset, pages = [], None, 0, 0
    while pages < MOKA_MAX_PAGES:
        if pages:
            time.sleep(1)  # one host: at least 1 s between requests
        d = _post_json(MOKA_API, {"orgId": org, "siteId": site, "limit": MOKA_PAGE, "offset": offset}, timeout=40)
        pages += 1
        if d.get("code") != 0:
            raise ValueError(f"Mokahr refused the list: code {d.get('code')} {d.get('msg', '')}")
        data = d.get("data") or {}
        stated = (data.get("jobStats") or {}).get("total")
        if total is None and isinstance(stated, int):
            total = stated
        batch = data.get("jobs") or []
        if not batch:
            break
        items += batch
        offset += len(batch)
        if total is not None and offset >= total:
            break
    open_items = [j for j in items if isinstance(j, dict) and j.get("status", "open") == "open"]

    def build(j):
        sep = "&" if "?" in c["feed_url"] else "?"  # ?jobId= keeps each posting's link distinct (store.link_id)
        return job(c, j.get("title", ""), flat(j.get("locations")),
                   f"{c['feed_url']}{sep}jobId={j.get('id')}#/job/{j.get('id')}", "", day(j.get("createdAt")))
    return records(c, open_items, build, capped=total is None or offset < total)


# ---------- Jibe (iCIMS career sites: Booking.com) ----------

JIBE_PAGE = 100       # jobs per request
JIBE_MAX_PAGES = 5    # 500 jobs read at most; a bigger board is capped


def jibe(c):
    """Jibe job API (iCIMS career sites, e.g. jobs.booking.com). feed_url is GET <origin>/api/jobs; the adapter pages
    it with limit and page (1-based). totalCount is the full count: reading stops when it is reached, a page comes
    back empty, or JIBE_MAX_PAGES requests are made; anything short of the total is capped. Each job's own
    data holds title, full_location, posted_date and the canonical job page (meta_data.canonical_url)."""
    parts = urllib.parse.urlsplit(c["feed_url"])
    api = f"{parts.scheme}://{parts.netloc}{parts.path}"
    items, total, pages = [], None, 0
    while pages < JIBE_MAX_PAGES:
        if pages:
            time.sleep(1)  # one host: at least 1 s between requests
        pages += 1
        d = json.loads(get(f"{api}?limit={JIBE_PAGE}&page={pages}", timeout=40))
        if total is None and isinstance(d.get("totalCount"), int):
            total = d["totalCount"]
        batch = d.get("jobs") or []
        if not batch:
            break
        items += batch
        if total is not None and len(items) >= total:
            break

    def build(item):
        d = item["data"]
        url = (d.get("meta_data") or {}).get("canonical_url") or d.get("apply_url") or ""
        return job(c, d.get("title", ""), d.get("full_location") or d.get("country", ""), url,
                   d.get("description", ""), d.get("posted_date", ""), d.get("employment_type", ""))
    return records(c, items, build, capped=total is None or len(items) < total)


ADAPTERS = {"join": join, "workday": workday, "erecruiter": erecruiter, "bamboohr": bamboohr,
            "gohire": gohire, "kula": kula, "factorial": factorial, "personio_json": personio_json,
            "mokahr": mokahr, "jibe": jibe}
