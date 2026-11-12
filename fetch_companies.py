"""Pull open vacancies straight from the job systems of approved companies
(companies.json). One adapter per job system; companies on systems without
an adapter are skipped and listed at the end. Also writes company_feeds.json: how many
jobs each feed returned today (before the Austria filter), which feeds failed and which
could only be read in part (capped), so prepare.py can tell a feed that answered in full
from one that failed, came back empty or showed only a slice - and never closes jobs on a slice.
Usage: python3 fetch_companies.py [--only slug1,slug2]   (or env FETCH_ONLY=slug1,slug2)"""

import html
import importlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import enrich
import fetch_posting
import store

ROOT = Path(__file__).parent
OUT = Path(os.environ.get("JOBDIGEST_OUT") or ROOT / "out")  # tests write to a scratch copy, like store.OUT
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36"
AUSTRIA_PLACE = re.compile(r"\b(wien|vienna|austria|österreich|oesterreich|osterreich|linz|graz|salzburg|innsbruck|"
                           r"klagenfurt|st\.? pölten|st\.? poelten|kärnten|kaernten|niederösterreich|niederoesterreich|"
                           r"oberösterreich|oberoesterreich|steiermark|tirol|tyrol|styria|vorarlberg|burgenland|"
                           r"villach|wels|dornbirn|eisenstadt|wiener[\s-]+neustadt|bregenz|krems|steyr|leoben|"
                           r"mödling|moedling|feldkirch|klosterneuburg|schwechat|österreichweit|oesterreichweit)\b", re.I)
# District towns and regions the list above misses: Wüstenrot, Generali, Novomatic and Magenta post jobs there
# (55 jobs dropped on 26.10.2026). Left out on purpose: names shared with German towns or English words
# (Feldkirchen, Neunkirchen, Rohrbach, Gmünd, Hard, Rust).
AUSTRIA_TOWNS = (
    "eisenstadt", "güssing", "jennersdorf", "mattersburg", "neusiedl am see", "oberpullendorf", "oberwart",
    "parndorf", "hermagor", "spittal an der drau", "völkermarkt", "wolfsberg",
    "amstetten", "bruck an der leitha", "gänserndorf", "gumpoldskirchen", "hollabrunn", "horn", "korneuburg",
    "lilienfeld", "melk", "mistelbach", "scheibbs", "stockerau", "traiskirchen", "tulln", "vösendorf",
    "waidhofen an der thaya", "waidhofen an der ybbs", "wieselburg", "wiener neudorf", "perchtoldsdorf",
    "leopoldsdorf", "zwettl", "brunn am gebirge", "hennersdorf", "ternitz",
    "ansfelden", "bad ischl", "braunau", "eferding", "enns", "freistadt", "gmunden", "grieskirchen",
    "kirchdorf an der krems", "leonding", "marchtrenk", "pasching", "perg", "ried im innkreis", "schärding",
    "traun", "vöcklabruck", "waxenberg", "mondsee",
    "bischofshofen", "flachgau", "hallein", "lungau", "pinzgau", "pongau", "saalfelden", "tamsweg", "tennengau",
    "wals-siezenheim", "zell am see",
    "bruck an der mur", "deutschlandsberg", "feldbach", "fürstenfeld", "gleisdorf", "hartberg", "judenburg",
    "kapfenberg", "knittelfeld", "köflach", "leibnitz", "liezen", "murau", "mürzzuschlag", "voitsberg", "weiz",
    "gratkorn", "premstätten", "schladming", "seiersberg",
    "hall in tirol", "imst", "kitzbühel", "kufstein", "landeck", "lienz", "reutte", "schwaz", "telfs", "wattens",
    "wörgl",
    "bludenz", "götzis", "hohenems", "lustenau", "rankweil",
)
_UMLAUT = (("ä", "(ä|ae|a)"), ("ö", "(ö|oe|o)"), ("ü", "(ü|ue|u)"))


def _town(name):
    pattern = re.escape(name).replace(r"\ ", r"\s+").replace(r"\-", r"[\s-]+")
    for letter, alternatives in _UMLAUT:
        pattern = pattern.replace(letter, alternatives)
    return pattern


AUSTRIA_TOWN = re.compile(r"\b((st\.?|sankt|saint)\s*(pölten|poelten|johann im pongau|veit an der glan)|"
                          + "|".join(_town(t) for t in AUSTRIA_TOWNS) + r")\b", re.I)
# 'Baden' is also Baden-Baden, Baden-Württemberg, 'Baden, Germany', 'Baden, DE', 'Baden (AG)' (Switzerland):
# only the Lower Austrian town counts. A dash next to the word, or a German or Swiss name after it, means another place.
TOWN_BADEN = re.compile(
    r"(?<![-–])(?<![-–]\s)\bbaden\b(?![-–]|\s*[-–]\s*baden\b|\s+(baden|württemberg|wuerttemberg)\b"
    r"|\s*,\s*(de|ch|germany|deutschland|switzerland|schweiz)\b|\s*\((ag|ch|de)\))", re.I)
# A foreign country named without Austria makes the whole location foreign, even when a town in it is Austrian
# ('Horn, Germany', 'Vienna, Virginia'). Country words match anywhere; codes only as a capitalised token of their own
# (lowercase 'de' or 'ch' are French and English words; 'IT' is a job-title word, so it is not a code here).
FOREIGN_COUNTRY = re.compile(
    r"\b(germany|deutschland|switzerland|schweiz|suisse|czechia|czech republic|czech|tschechien|slovakia|slowakei|"
    r"hungary|ungarn|italy|italien|netherlands|niederlande|france|frankreich|poland|polen|slovenia|slowenien|"
    r"united kingdom|great britain|großbritannien|england)\b", re.I)
FOREIGN_CODE = re.compile(r"(?<![A-Za-z])(DE|CH|GB|UK|CZ|SK|HU)(?![A-Za-z&])")  # case-sensitive
# US namesakes (QA S7-3, round 3): 'Vienna, Illinois', 'Vienna, VA' are not the Austrian city, so any US state name,
# USA or United States in a part makes it foreign.
US_NAMESAKE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    r"illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    r"missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|"
    r"ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    r"virginia|washington|west virginia|wisconsin|wyoming|usa|united states)\b", re.I)
# A two-letter US code counts only as the last token of a comma part, optionally followed by a 5-digit ZIP: 'Vienna,
# VA', 'Vienna, VA 22180'. Never 'Wien, MA 48' (Magistratsabteilung), 'Wien (MA 01)' or 'Wien, OR Abteilung'. MA, ME, IN,
# OR, CO, ID, OK, HI and AL are left out: Vienna abbreviations or German and English words.
US_CODE_TAIL = re.compile(r",\s*(AK|AZ|AR|CA|CT|FL|GA|IL|IA|KS|KY|LA|MD|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|"
                          r"OH|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|US)(?:\s+\d{5}(?:-\d{4})?)?\s*(?=,|\Z)")
BIG_AUSTRIAN_CITY = re.compile(r"\b(wien|vienna|graz|linz|salzburg|innsbruck)\b", re.I)
# One location can list several places: 'Wien; Berlin, DE', 'Wien oder München', 'Graz / Zürich, Schweiz' (QA N1).
LOCATION_SEGMENT = re.compile(r";|/|\||\n|\s+oder\s+|\s+or\s+", re.I)
AUSTRIA_NAME = re.compile(r"\b(austria|österreich|oesterreich|osterreich|österreichweit|oesterreichweit)\b", re.I)
AT_CODE = re.compile(r"(?<![A-Za-z])AT(?![A-Za-z&])")  # case-sensitive: lowercase 'at' is the English word
# SmartRecruiters (Deloitte Austria) writes the country as lowercase last token: 'Steyr, at', 'Dornbirn, at'.
AT_CODE_TAIL = re.compile(r",\s*at\s*$")
# A location that only says remote/hybrid/home office names no country: all companies.json employers are
# Austrian, so such a job counts as Austrian. Any other word ('GB', 'Germany', 'in EU') breaks the match.
REMOTE_ONLY = re.compile(r"^[\s,;/|()\-–]*((remote|hybrid|home[\s-]*office|work\s+from\s+home|from\s+home|worker|job"
                         r"|mobiles\s+arbeiten|telearbeit)"
                         r"[\s,;/|()\-–]*)+$", re.I)


DESC_LIMIT = 8000   # the whole posting: requirements, hours and salary often sit after the first 2500 chars
MAX_FEED_BYTES = 40_000_000  # a full SuccessFactors feed is 0.1-11 MB; EY's global one (125 MB) is not read
RSS_CAP = 20        # SuccessFactors RSS returns the 20 newest postings, whatever the paging parameters
SR_PAGE, SR_MAX_PAGES = 100, 10
SR_DEADLINE = 60  # seconds for one company's whole SmartRecruiters read: 10 pages of a stalled API took ~400 s (QA N6)
# The run is killed at 1200 s (run.sh) and writes nothing before its end: no company is started after this many
# seconds. A company not started is recorded as failed, never as an empty answer (QA 26.10, minor 2).
FETCH_DEADLINE = 900
_clock = time.monotonic  # tests replace it with a fake clock


def _austrian_segment(seg):
    """One part of a location ('Wien', 'Berlin, DE'). It names Austria, or an Austrian place without a foreign country.
    A foreign country named without Austria makes the part foreign, except a big Austrian city (Wien, Vienna, Graz,
    Linz, Salzburg, Innsbruck) in the same part: 'Vienna, Munich, Germany' and 'Wien, Italien' are Austrian (QA N1,
    S7-3). A US namesake makes the part foreign even then ('Vienna, Illinois'). Smaller names shared with German
    towns (Horn, Wolfsberg) never count."""
    if AUSTRIA_NAME.search(seg) or AT_CODE.search(seg):
        return True
    if US_NAMESAKE.search(seg) or US_CODE_TAIL.search(seg):
        return False
    if FOREIGN_COUNTRY.search(seg) or FOREIGN_CODE.search(seg):
        return bool(BIG_AUSTRIAN_CITY.search(seg))
    return bool(AUSTRIA_PLACE.search(seg) or AUSTRIA_TOWN.search(seg) or TOWN_BADEN.search(seg))


def is_austrian(location, title=""):
    """True when a part of the location names Austria (name, city or state), carries the country code AT as its own
    token ('Wien, AT', 'AT-1030 Wien', 'Remote, AT'), or the whole location says only remote/hybrid/home office (no
    country named: the employers are Austrian). Each part is judged on its own (see _austrian_segment): a foreign
    country named without Austria ('Remote, GB', 'Munich, DE', 'Horn, Germany', 'Vienna, Virginia') is not Austrian,
    but 'Wien; Berlin, DE' is. The title is read only when the feed gave no location, because SuccessFactors
    sometimes puts it there."""
    text = location if (location or "").strip() else (title or "")
    if AT_CODE_TAIL.search(location or ""):  # SmartRecruiters writes 'Steyr, at'
        return True
    if any(_austrian_segment(seg) for seg in LOCATION_SEGMENT.split(text) if seg.strip()):
        return True
    return bool(text.strip()) and bool(REMOTE_ONLY.match(text))


class Feed(list):
    """An adapter's jobs plus what the run must know about the feed: capped = only part of the board was
    read (no 'company removed the vacancy' closures from it), bad = records skipped as malformed."""

    def __init__(self, jobs=(), capped=False, bad=0):
        super().__init__(jobs)
        self.capped, self.bad = capped, bad


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """A company feed may redirect only to a public host that is not a job board. Every hop is checked with
    fetch_posting's guards (DENY, host form, public address) before urllib follows it (QA 26.10, minor 8).
    robots.txt is not read here: the feeds are APIs, and the adapters never read robots for them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = fetch_posting._parts(newurl)
        if not parsed or enrich.registrable_domain(parsed[1]) in fetch_posting.DENY:
            raise urllib.error.URLError(f"redirect refused: {newurl[:200]}")
        try:
            public = fetch_posting._public_host(parsed[0])
        except OSError:  # a name that does not resolve is not a public host
            public = False
        if not public:
            raise urllib.error.URLError(f"redirect refused: {newurl[:200]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirect)


def get(url, accept="application/json", max_bytes=MAX_FEED_BYTES, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"feed larger than {max_bytes // 1_000_000} MB")
    return data


def flat(s):
    """Feed fields are sometimes objects (TalentLyft Location is a dict): their text parts, joined."""
    if s is None:
        return ""
    if isinstance(s, dict):
        for k in ("CityCountry", "name", "Name", "label", "city", "City"):
            if isinstance(s.get(k), str) and s[k]:
                return s[k]
        return ", ".join(flat(v) for v in s.values() if isinstance(v, (str, int, float)) and str(v).strip())
    if isinstance(s, (list, tuple)):
        return ", ".join(x for x in (flat(v) for v in s) if x)
    return str(s)


def text(s, limit=DESC_LIMIT):
    """Plain text of an HTML fragment. Unescaped twice: SuccessFactors full feeds escape the HTML inside CDATA."""
    s = html.unescape(flat(s))
    s = html.unescape(re.sub(r"<[^<>]*>", " ", s))
    return store.scrub(re.sub(r"\s+", " ", s).strip()[:limit])


def job(c, title, location, url, desc="", posted="", employment=""):
    return dict(source="company", company=c["name"], company_id=c["id"], niche=c["niche"], top=c["top"],
                title=text(title, 300), location=text(location, 200), url=store.scrub(flat(url))[:2000],
                posted=day(posted), employment=text(employment, 100), description=text(desc))


def day(value):
    """'2026-10-08T...', 'Thu, 08 Oct 2026 ...' or a Unix time in ms -> '2026-10-08'; '' if unknown."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    v = str(value).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    try:
        return parsedate_to_datetime(v).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        pass
    try:  # SuccessFactors: 'Thu Oct 01 02:00:00 UTC 2026'
        return datetime.strptime(re.sub(r"\s+[A-Z]{3,4}\s+", " ", v), "%a %b %d %H:%M:%S %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def records(c, items, build, capped=False):
    """Build one job per record; a record with a null date, a missing link or an odd type is skipped and
    counted, the company's other jobs still come through (pipeline-10)."""
    out, bad = [], 0
    for item in items or []:
        try:
            j = build(item)
        except Exception:
            bad += 1
            continue
        if j and j["title"] and j["url"]:
            out.append(j)
        else:
            bad += 1
    return Feed(out, capped=capped, bad=bad)


# ---------- adapters ----------

def greenhouse(c):
    token = re.search(r"boards/([^/]+)/jobs", c["feed_url"]).group(1)
    data = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"))
    return records(c, data.get("jobs"), lambda j: job(
        c, j["title"], (j.get("location") or {}).get("name", ""), j.get("absolute_url"),
        j.get("content", ""), j.get("updated_at") or j.get("first_published")))


def lever(c):
    data = json.loads(get(c["feed_url"]))
    return records(c, data, lambda j: job(
        c, j["text"], (j.get("categories") or {}).get("location", ""), j.get("hostedUrl"),
        " ".join(x for x in (j.get("descriptionPlain"), j.get("additionalPlain")) if isinstance(x, str)),
        j.get("createdAt"), (j.get("categories") or {}).get("commitment", "")))


def ashby(c):
    data = json.loads(get(c["feed_url"]))
    return records(c, data.get("jobs"), lambda j: job(
        c, j["title"], j.get("location", ""), j.get("jobUrl", ""), j.get("descriptionPlain", ""),
        j.get("publishedAt"), j.get("employmentType", "")))


def smartrecruiters(c):
    """Paged by offset until totalFound (the API returns at most 100 per call). Capped if the pages run out before
    totalFound (more than SR_MAX_PAGES pages), or if the SR_DEADLINE stops the read (QA N6): what was read is kept,
    and no request waits longer than what is left of the deadline."""
    started = _clock()
    m = re.search(r"companies/([^/]+)/postings", c["feed_url"])
    out, total, bad, cut = [], None, 0, False
    for page in range(SR_MAX_PAGES):
        if page:
            time.sleep(1)
        left = SR_DEADLINE - (_clock() - started)
        if left <= 0:
            cut = True
            break
        # Big global boards: ask for Austria only.
        url = (f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings"
               f"?limit={SR_PAGE}&offset={page * SR_PAGE}&country=at")
        try:  # the default timeout is 40 s, so a shorter one is passed only near the deadline: min(40, left)
            data = json.loads(get(url) if left >= 40 else get(url, timeout=left))
        except OSError:  # a request cut short by the deadline stops the read; an error before it still raises
            if _clock() - started < SR_DEADLINE - 1:
                raise
            cut = True
            break
        total = data.get("totalFound") if isinstance(data.get("totalFound"), int) else total
        content = data.get("content") or []

        def build(j):
            loc = j.get("location") or {}
            return job(c, j["name"], f"{loc.get('city') or ''}, {loc.get('country') or ''}",
                       f"https://jobs.smartrecruiters.com/{m.group(1)}/{j['id']}", "",
                       j.get("releasedDate"), (j.get("typeOfEmployment") or {}).get("label", ""))
        part = records(c, content, build)
        out += part
        bad += part.bad
        if len(content) < SR_PAGE or (total is not None and (page + 1) * SR_PAGE >= total):
            break
    capped = cut or (total is not None and len(out) + bad < total)
    return Feed(out, capped=capped, bad=bad)


def personio(c):
    root = ET.fromstring(get(c["feed_url"], "application/xml"))
    host = urllib.parse.urlparse(c["feed_url"]).netloc

    def build(p):
        desc = " ".join(text(d.findtext("value", "")) for d in p.findall("jobDescriptions/jobDescription"))
        return job(c, p.findtext("name", ""), p.findtext("office", ""), f"https://{host}/job/{p.findtext('id')}",
                   desc, p.findtext("createdAt", ""), p.findtext("schedule", ""))
    return records(c, root.findall("position"), build)


def recruitee(c):
    data = json.loads(get(c["feed_url"]))
    return records(c, data.get("offers"), lambda j: job(
        c, j["title"], j.get("location", ""), j.get("careers_url", ""),
        " ".join(x for x in (j.get("description"), j.get("requirements")) if isinstance(x, str)),
        j.get("published_at"), j.get("employment_type_code", "")))


def workable(c):
    data = json.loads(get(c["feed_url"]))
    return records(c, data.get("jobs"), lambda j: job(
        c, j["title"], f"{j.get('city') or ''}, {j.get('country') or ''}", j.get("url", ""), "",
        j.get("published_on"), j.get("employment_type", "")))


def breezy(c):
    data = json.loads(get(c["feed_url"]))
    return records(c, data, lambda j: job(
        c, j["name"], (j.get("location") or {}).get("name", ""), j.get("url", ""), "",
        j.get("published_date"), (j.get("type") or {}).get("name", "")))


def _title_location(title, location=""):
    """SuccessFactors puts the location in brackets at the end of the title: 'X (Wien, Wien, AT, 1030)'.
    Only a bracket that looks like a place (has a comma, or is the feed's own location) is taken off,
    so '(w/m/x)' stays in the title."""
    m = re.search(r"\s*\(([^()]*)\)\s*$", title or "")
    if m and ("," in m.group(1) or m.group(1).strip() == (location or "").strip()):
        return title[: m.start()].strip(), location or m.group(1)
    return (title or "").strip(), location


def rss(c, url=None):
    """SuccessFactors / Avature / generic RSS. A SuccessFactors RSS shows only the RSS_CAP newest postings, and an
    Avature RSS only RSS_CAP postings with the lowest ids (UniCredit, 26.10: 20 of 25 on the site, paging ignored),
    so a full one is marked capped: it never closes jobs (QA N4)."""
    root = ET.fromstring(get(url or c["feed_url"], "application/rss+xml"))

    def build(it):
        title, loc = _title_location(it.findtext("title", ""))
        return job(c, title, loc, it.findtext("link", ""), it.findtext("description", ""), it.findtext("pubDate", ""))
    feed = records(c, list(root.iter("item")), build)
    feed.capped = c["ats"] in ("successfactors", "avature") and len(feed) + feed.bad >= RSS_CAP
    return feed


G = "{http://base.google.com/ns/1.0}"


def successfactors(c):
    """The whole board from the career site's Google-jobs feed (<host>/sitemal.xml: every posting with its
    text; RBI 88, KPMG 133 on 09.10.2026, where the RSS shows 20). Posting dates come from the RSS when it
    has them. Companies whose full feed is too big (EY: 8,886 jobs worldwide, 125 MB) are marked
    full_feed=false in companies.json and read from the keyword RSS, capped."""
    if c.get("full_feed") is False:
        return rss(c)
    parts = urllib.parse.urlsplit(c["feed_url"])
    try:
        root = ET.fromstring(get(f"{parts.scheme}://{parts.netloc}/sitemal.xml", "application/xml"))
    except Exception as e:  # no full feed today: the RSS slice, marked capped, is better than nothing
        print(f"{c['name']}: full feed failed ({type(e).__name__}), using the RSS", file=sys.stderr)
        return rss(c)
    posted = {}
    try:
        time.sleep(0.5)
        for j in rss(c):
            posted[store.link_id(j["url"])] = j["posted"]
    except Exception:
        pass  # dates are a nice-to-have

    def build(it):
        title, loc = _title_location(it.findtext("title", ""), it.findtext(f"{G}location", "") or "")
        link = it.findtext("link", "")
        lid = store.link_id(link)
        twins = [f"{lid.split('#')[0]}#{int(lid.split('#')[1]) + d}" for d in (100, -100)] if "#" in lid else []
        return job(c, title, loc, link, it.findtext("description", ""),
                   next((posted[x] for x in [lid] + twins if posted.get(x)), ""))
    feed = records(c, list(root.iter("item")), build)
    # The same posting in two languages has two ids exactly 100 apart (RBI 1369252457 / 1369252557): keep one.
    by_id = {store.link_id(j["url"]): j for j in feed}
    twins = {lid for lid, j in by_id.items() if lid and "#" in lid and (
        (t := by_id.get(f"{lid.split('#')[0]}#{int(lid.split('#')[1]) - 100}")) and store.similar_titles(t["title"], j["title"]))}
    return Feed([j for j in feed if store.link_id(j["url"]) not in twins], capped=False, bad=feed.bad)


def teamtailor(c):
    """Teamtailor RSS: the location is in tt:locations, not in the title."""
    ns = "{https://teamtailor.com/locations}"
    root = ET.fromstring(get(c["feed_url"], "application/rss+xml"))
    return records(c, list(root.iter("item")), lambda it: job(
        c, it.findtext("title", ""),
        "; ".join(", ".join(x for x in (loc.findtext(f"{ns}city", ""), loc.findtext(f"{ns}country", "")) if x)
                  for loc in it.iter(f"{ns}location")),
        it.findtext("link", ""), it.findtext("description", ""), it.findtext("pubDate", "")))


def dvinci(c):
    data = json.loads(get(c["feed_url"]))

    def build(j):
        jo = j.get("jobOpening") or {}
        url = (j.get("applicationFormURL") or "").removesuffix("/apply")
        desc = " ".join(text(j.get(k)) for k in ("introduction", "tasks", "profile", "weOffer"))
        return job(c, j.get("position", ""), jo.get("location", ""), url, desc, jo.get("createdDate"),
                   ", ".join(w.get("name", "") for w in jo.get("workingTimes") or [] if isinstance(w, dict)))
    return records(c, data, build)


def talentlyft(c):
    """TalentLyft public API: Location is an object (city, country), paged by Pages.Next."""
    data = json.loads(get(c["feed_url"]))
    items = list(data.get("Results") or data.get("results") or data.get("data") or [])
    pages = 1
    while isinstance(data.get("Pages"), dict) and data["Pages"].get("Next") and pages < 10:
        time.sleep(1)
        data = json.loads(get(data["Pages"]["Next"]))
        items += data.get("Results") or []
        pages += 1
    return records(c, items, lambda j: job(
        c, j.get("Title") or j.get("title", ""), j.get("Location") or j.get("location", ""),
        j.get("AbsoluteUrl") or j.get("Url") or j.get("url", ""), j.get("Description") or "",
        j.get("LastModified")))


def capgemini(c):
    """Capgemini's own job-search API (JSON, not SuccessFactors RSS - that mislabel failed every morning)."""
    data = json.loads(get(c["feed_url"]))
    items = data.get("data") or []
    feed = records(c, items, lambda j: job(
        c, j["title"], j.get("location", ""), j.get("apply_job_url", ""), j.get("description", ""),
        j.get("updated_at") or j.get("indexed_at"), j.get("contract_type", "")))
    total = data.get("total")
    feed.capped = isinstance(total, int) and total > len(items)
    return feed


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "personio": personio, "recruitee": recruitee, "workable": workable, "breezy": breezy,
            "successfactors": successfactors, "avature": rss, "dvinci": dvinci, "talentlyft": talentlyft,
            "teamtailor": teamtailor, "capgemini": capgemini}
# Stage 7 wave 2 adapters live in their own modules, which import the helpers above - so they are loaded
# from main(), when this module is complete. A module that fails to load stops only its own companies.
EXTRA_MODULES = ("ats_api", "ats_pages", "site_jobs")


def load_extra_adapters():
    failed = {}
    for name in EXTRA_MODULES:
        try:
            ADAPTERS.update(importlib.import_module(name).ADAPTERS)
        except Exception as e:
            failed[name] = f"{type(e).__name__}: {e}"
    return failed


def only_ids():
    """--only a,b or FETCH_ONLY=a,b: fetch just these companies (trying new feeds without a full run)."""
    arg = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv[:-1] else os.environ.get("FETCH_ONLY", "")
    return {x.strip() for x in arg.split(",") if x.strip()}


def main():
    started = _clock()
    with open(ROOT / "companies.json", encoding="utf-8") as f:
        companies = json.load(f)
    only = only_ids()
    if only:
        unknown = only - {c["id"] for c in companies}
        if unknown:
            print("not in companies.json:", sorted(unknown), file=sys.stderr)
        companies = [c for c in companies if c["id"] in only]
    pool, skipped, failed, totals, capped, bad = [], [], {}, {}, [], {}
    for name, err in load_extra_adapters().items():  # shown in the digest's health block like a failed feed
        failed[f"module:{name}"] = f"адаптеры {name}: {err}"[:300]
        print(f"adapter module {name} not loaded: {err}", file=sys.stderr)
    for c in companies:
        fn = ADAPTERS.get(c["ats"])
        if not fn or not c.get("feed_url"):
            skipped.append(c["name"])
            continue
        if _clock() - started > FETCH_DEADLINE:
            # Not read today. It is not in totals either, so prepare.py never takes it for an empty answer.
            failed[c["id"]] = f"{c['name']}: не успел (лимит времени)"
            print(f"{c['name']}: не успел (лимит времени)", file=sys.stderr)
            continue
        try:
            jobs = fn(c)
        except Exception as e:  # one broken feed must not stop the run
            failed[c["id"]] = f"{c['name']}: {type(e).__name__}"
            continue
        at = [j for j in jobs if is_austrian(j["location"], j["title"])]
        totals[c["id"]] = len(jobs)
        if getattr(jobs, "capped", False):
            capped.append(c["id"])
        if getattr(jobs, "bad", 0):
            bad[c["id"]] = jobs.bad
        pool += at
        print(f"{c['name'][:40]:40} {len(jobs):4} total {len(at):4} Austria"
              + (" (capped: only part of the board)" if c["id"] in capped else "")
              + (f" ({bad[c['id']]} malformed records skipped)" if c["id"] in bad else ""), file=sys.stderr)
        time.sleep(1.5 if c["ats"] == "workable" else 0.3)
    OUT.mkdir(exist_ok=True)
    status = {"at": store.now(), "totals": totals, "capped": capped, "failed": failed, "bad_records": bad,
              "asked": len(companies) - len(skipped),
              # an acknowledged cap (full_feed=false) is a known limit, not today's problem
              "known_capped": [c["id"] for c in companies if c.get("full_feed") is False]}
    # Jobs first, status second, each swapped in whole: prepare.py trusts the status only when it is
    # not older than the jobs file, so a run killed in between never closes jobs on yesterday's list.
    for name, data in (("company_jobs.json", pool), ("company_feeds.json", status)):
        tmp = OUT / (name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store.scrub(data), f, ensure_ascii=False, indent=1)
        tmp.replace(OUT / name)
    print(f"\njobs in Austria: {len(pool)} from {len(companies) - len(skipped) - len(failed)} companies")
    print(f"skipped (no adapter / no feed): {len(skipped)}")
    print("capped (only part of the board read, never closed by the feed rule):", capped)
    print("failed:", list(failed.values()))


if __name__ == "__main__":
    main()
