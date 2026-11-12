"""Tailor the base CV to one job: requirements -> library selection -> code checks -> PDF -> checks ->
saved version (data/cv/<job_id>/<n>/ + a cv_versions row). Whatever goes wrong with a run, the caller gets
a PDF: a failed tailoring returns the base CV with a reason ('fallback'), never silence. Only a broken
cv/library.json stops the run (LibraryError): that is a setup error to fix, not something to send around.
Library mode (09.10.2026): summary and bullets are approved library texts, so there is no verifier call.

CLI: python3 -m cv.tailor.run <job_id> [--text path]   (prints the result as JSON)
"""

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))  # the bot's worker may start us from another cwd

import store  # noqa: E402
from cv.source import build as builder  # noqa: E402
from cv.tailor import checks, cover, library, llm, pdfcheck, requirements, tailor  # noqa: E402

CV = PROJECT / "cv"
BASE_JSON = CV / "source" / "content" / "en.json"
FACTS = CV / "facts.md"
APPROVED_PDF = CV / "out" / "Alex_Morgan_CV_EN.pdf"
PREFIX = "Alex-Morgan-CV-"
MAX_NAME = 80
MIN_DESC = 200          # chars: below this a 'description' is a title or a teaser, not an ad to tailor from
PART_TIME_HOURS = 20    # facts.md: availability, 20 h/week
RUN_DEADLINE = 600      # seconds for one whole run; each Claude call also stops at 180 s

BUILD = builder.build   # tests swap in a fake renderer
CHECK = pdfcheck.check  # tests swap in a fake poppler


def cv_dir():
    """Versions live next to state.db (data/cv/); JOBDIGEST_CV_DIR overrides it for tests."""
    return Path(os.environ.get("JOBDIGEST_CV_DIR") or store.DB.parent / "cv")


def _slug(text):
    text = re.sub(r"\([^)]*/[^)]*\)", " ", text or "")  # gender markers like (m/w/d)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


def _cut(slug, n):
    if len(slug) <= n:
        return slug
    head = slug[:n]
    return head.rsplit("-", 1)[0] if slug[n] != "-" and "-" in head else head.rstrip("-")


def file_name(company, role):
    """Alex-Morgan-CV-<Company>-<Role>.pdf: ASCII, hyphens, at most 80 chars."""
    budget = MAX_NAME - len(PREFIX) - len(".pdf")
    c, r = _slug(company), _slug(role)
    if c and r and len(c) + 1 + len(r) > budget:  # the role says more than a long legal name: cut the company first
        c = _cut(c, max(12, budget - 1 - len(r)))
        r = _cut(r, budget - 1 - len(c))
    parts = "-".join(p for p in (c, r) if p)
    return PREFIX + (_cut(parts, budget) or "Application") + ".pdf"


SHEET_HEADER = re.compile(r"^## Knockout answer sheet.*?\n(.*?)(?=^## |\Z)", re.S | re.M)
MAX_SHEET = 3000   # characters: the sheet goes out as one Telegram message with a header


def answers(facts, reqs):
    """Knockout answer sheet: the facts.md section, with this ad's salary filled in."""
    m = SHEET_HEADER.search(facts)
    lines = []
    for line in (m.group(1) if m else "").splitlines():
        line = re.sub(r"\s*\[[^\]]*\]", "", line).strip()
        if line.startswith("- ") and not line.lower().startswith("- salary"):
            lines.append(line)
    lines.insert(2 if len(lines) >= 2 else len(lines), "- Salary: " + salary_line(reqs or {}))
    return "\n".join(lines)[:MAX_SHEET]


def load_facts(path=None):
    """cv/facts.md, which must hold a 'Knockout answer sheet' section with its lines: a missing, empty or renamed
    file stops the run (LibraryError naming facts.md) instead of a sheet without permit and languages (QA cvtailor-14)."""
    path = Path(path or FACTS)
    try:
        facts = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        raise library.LibraryError(f"{path.name}: {type(e).__name__}: {e}", path.name)
    m = SHEET_HEADER.search(facts)
    if not m or sum(1 for l in m.group(1).splitlines() if l.strip().startswith("- ")) < 2:
        raise library.LibraryError(f"{path.name}: no '## Knockout answer sheet' section with its lines", path.name)
    return facts


def load_rules():
    """never_claim.json and allowed_numbers.json, each checked on its own so an error names the broken file."""
    out = []
    for loader, path in ((checks.load_never_claim, checks.NEVER_CLAIM), (checks.load_allowed_numbers, checks.ALLOWED_NUMBERS)):
        try:
            out.append(loader(path))
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise library.LibraryError(f"{Path(path).name}: {type(e).__name__}: {e}", Path(path).name)
    return out


HOURLY = re.compile(r"/\s*(?:h|hr|hour|std|stunde)\b|per hour|an hour|pro stunde|hourly|stundenlohn|stundensatz", re.I)
MAX_AMOUNT = 10 ** 7   # a salary figure above this is not a salary (QA security-10: a 400-digit 'amount')


def _full_time(hours):
    """Full-time only when the ad says so or states 35+ hours; 'Part-time, 30 h/week' is part-time."""
    if re.search(r"part|teilzeit|geringf", hours, re.I):
        return False, None
    nums = [float(n.replace(",", ".")) for n in re.findall(r"\d{1,4}(?:[.,]\d+)?", hours)]
    full_hours = next((n for n in nums if 35 <= n <= 80), None)
    return bool(full_hours or re.search(r"full|vollzeit", hours, re.I)), full_hours


def salary_line(reqs):
    """Never state one where optional; if a form forces it, the ad's minimum, pro-rated when full-time.
    An hourly rate is the same for any number of hours, so it is never pro-rated."""
    minimum, hours = reqs.get("min_salary"), str(reqs.get("hours") or "")
    if reqs.get("salary_unclear"):
        return "leave empty if optional; the ad's pay line could not be read - check the salary in the ad."
    if not minimum:
        # QA cvtailor-01: the stored ad text can be cut, so 'the ad states no minimum' may be false
        return "leave empty if optional (no minimum found in the ad text; check the full ad if a form requires it)."
    text = f"leave empty if optional; if required: {minimum} (minimum stated in the ad)"
    if HOURLY.search(minimum):
        return text + "; hourly rate, not pro-rated."
    stated = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:h\b|hours|std|stunden)", minimum, re.I)
    if stated or re.search(r"part|teilzeit", minimum, re.I):  # the figure already names its own hours
        if re.search(r"part|teilzeit", minimum, re.I) or float(stated.group(1).replace(",", ".")) < 35:
            return text + "."
        hours = stated.group(0)
    full, full_hours = _full_time(hours)
    if not full:
        return text + "."
    amount = _amount(minimum)
    if full_hours and amount:
        pro = amount * PART_TIME_HOURS / full_hours
        return text + (f"; the ad is full-time ({full_hours:g} h/week), pro-rated to {PART_TIME_HOURS} h/week: "
                       f"about {pro:,.2f} (same currency and period).")
    return text + f"; the ad is full-time, pro-rate it to {PART_TIME_HOURS} h/week."


def _amount(text):
    """First money amount, cents kept: '3.200,50' / '3,200.50' -> 3200.5, '2,800' -> 2800.0, '2.6k' -> 2600.0.
    None when there is none or it is not a plausible salary (over MAX_AMOUNT, absurdly long digit runs)."""
    m = re.search(r"(?<!\d)(\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d{1,9}(?:[.,]\d{1,2})?)(?!\d)(\s?[kK](?![A-Za-z]))?", text)
    if not m:
        return None
    tok = m.group(1)
    cents = re.search(r"[.,](\d{1,2})$", tok)
    whole = re.sub(r"[.,\s]", "", tok[:cents.start()] if cents else tok)
    if not whole.isdigit() or len(whole) > 9:
        return None
    amount = int(whole) + (int(cents.group(1).ljust(2, "0")) / 100 if cents else 0)
    if m.group(2):
        amount *= 1000   # QA cvtailor-05: '2.6k' is 2,600, not 2.6
    return amount if 0 < amount <= MAX_AMOUNT else None


def _load_job(job_id):
    con = store.connect()
    try:
        row = con.execute("select * from jobs where id=?", (job_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise LookupError(f"job {job_id} not found")
    try:
        data = json.loads(row["data"] or "{}")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"id": row["id"], "title": row["title"] or data.get("title") or "",
            "company": row["company"] or data.get("company") or "",
            "description": str(data.get("description") or "")}


def pasted_path(job_id):
    """The ad text Misha pasted for a job without a description; reused by the next press (QA cvtailor-09)."""
    return cv_dir() / job_id / "pasted.txt"


def _new_version_dir(job_id):
    root = cv_dir() / job_id
    root.mkdir(parents=True, exist_ok=True)
    n = max([int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()] or [0]) + 1
    while True:
        try:
            (root / str(n)).mkdir()
            return root / str(n)
        except FileExistsError:  # a parallel run took this number
            n += 1


def skill_items(base, lib, result, skills_allowed):
    """Skill lines through the exact whitelist (cv/skills_allowed.json, per group): items not listed, repeated or
    past the 10th are dropped and logged; a line left empty goes back to base. Deterministic, no Claude call."""
    content = copy.deepcopy(result["content"])
    reverted, empty = list(result.get("reverted", [])), []
    for path, _ in checks.changed_fields(base, content):
        if not path.startswith("skills"):
            continue
        k = int(path[7:-1])
        line = content["skills"][k]
        keep, dropped = checks.allowed_items(line["group"], line["items"], skills_allowed)
        if not dropped and ", ".join(keep) == line["items"]:
            continue
        line["items"] = ", ".join(keep)
        reverted += [{"field": path, "text": x[:200], "base": "", "why": why} for x, why in dropped]
        if not keep:
            empty.append((path, "no skill item left"))
    result = library.refresh(base, lib, dict(result, content=content, reverted=reverted))
    return library.revert_paths(base, lib, result, empty)


def code_checks(base, lib, allowed, words, result, never, case_sensitive, terms, skills_allowed):
    """Rules in code: skill items outside the whitelist dropped; a changed line with stray markup, a blocklisted tool, a new
    number or German words puts its unit (summary, role, skill line) back to base - for library texts this
    is a tripwire, they passed the same checks at load; a term used more than 3 times loses its latest
    changed unit; then the length guard; then cover.fill puts in a library unit for each claimable term still
    missing (within the rules), and the length guard runs again."""
    result = skill_items(base, lib, result, skills_allowed)
    content = result["content"]
    flagged = checks.bad_markup(content) + checks.bad_glyphs(content)
    for path, text in checks.changed_fields(base, content):
        why = []
        if hits := checks.blocklist_hits(text, never, case_sensitive):
            why.append("never-claim: " + ", ".join(hits))
        if nums := checks.new_numbers(allowed, text, words):
            why.append("new number: " + ", ".join(nums))
        if de := checks.german_words(text):
            why.append("German words: " + ", ".join(de))
        if why:
            flagged.append((path, "code check: " + "; ".join(why)))
    result = library.revert_paths(base, lib, result, flagged)
    for term in overused(terms, base, result["content"]):
        while overused([term], base, result["content"]):
            hits = [library.unit_of(p) for p, t in checks.changed_fields(base, result["content"]) if checks.mentions(term, t)]
            if not hits:
                break
            result = library.revert(base, lib, result, hits[-1], f"term used more than 3 times: {term}")
    result = library.length_guard(base, lib, result)
    result = cover.fill(base, lib, result, terms, skills_allowed)
    return library.length_guard(base, lib, result)


def overused(terms, base, content, limit=3):
    """Terms used more than `limit` times. For a term the base CV itself already uses more than `limit` times
    ('research' x4), only the tailoring's own additions count against the limit, so the research variant is not
    reverted on exactly the research ads (QA cvtailor-04); any other term keeps the spec's 'no term more than 3
    times'."""
    base_text, text = checks.strip_tags(checks.cv_text(base)), checks.strip_tags(checks.cv_text(content))
    out = []
    for t in checks._unique(terms):
        n, b = checks._count(t, text), checks._count(t, base_text)
        if n > limit and (b <= limit or n - b > limit):
            out.append(t)
    return out


def render(content, vdir, name):
    """Write content.json, build the PDF, check it. Without poppler the page count comes from the bytes."""
    content_path = vdir / "content.json"
    content_path.write_text(json.dumps(_scrub(content), ensure_ascii=False, indent=1), encoding="utf-8")
    pdf = Path(BUILD(content_path, vdir / name, html_dir=vdir))
    try:
        report = CHECK(pdf, checks.cv_text(content))
    except pdfcheck.ToolMissing as e:
        print(f"ATS checks skipped: {e}", file=sys.stderr, flush=True)  # QA cvtailor-11: loud in worker.log
        data = pdf.read_bytes()
        report = {"pages": pdfcheck.page_count(data), "size": len(data), "text": "",
                  "issues": [f"pdf text checks skipped: {e}"]}
    return pdf, report


def overflow(base, content, report):
    """Which section grew most against the base, and roughly how many lines spill onto page 2."""
    def size(c, key):
        if key == "summary":
            return len(c.get("summary", ""))
        if key == "skills":
            return sum(len(s["group"]) + len(s["items"]) for s in c["skills"])
        i = int(key[11:-1])
        return sum(len(checks.strip_tags(b)) for b in c["experience"][i]["bullets"])
    keys = ["summary", "skills"] + [f"experience[{i}]" for i in range(len(content["experience"]))]
    section = max(keys, key=lambda k: size(content, k) - size(base, k))
    spill = report.get("text", "").split("\f")[1:]
    lines = sum(1 for page in spill for l in page.splitlines() if l.strip())
    return section, max(1, min(10, lines or 3))


def longest_role(content):
    return max(range(len(content["experience"])),
               key=lambda i: sum(len(checks.strip_tags(b)) for b in content["experience"][i]["bullets"]))


def _merge_reverted(*lists):
    out, seen = [], set()
    for item in (x for lst in lists for x in lst):
        key = (item["field"], item["text"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _scrub(obj):
    """No lone surrogate may reach a UTF-8 file or the bot (it would crash the write)."""
    if isinstance(obj, str):
        return checks.scrub(obj)
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {_scrub(k): _scrub(v) for k, v in obj.items()}
    return obj


def save(job_id, vdir, out, content, status):
    out.update(_scrub(out))
    content = _scrub(content)
    (vdir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    con = store.connect()
    try:
        con.execute("insert into cv_versions (job_id, created_at, pdf_path, content, changes, gaps, reverted,"
                    " coverage_before, coverage_after, status) values (?,?,?,?,?,?,?,?,?,?)",
                    (job_id, store.now(), out["pdf"], json.dumps(content, ensure_ascii=False),
                     json.dumps(out["changes"], ensure_ascii=False), json.dumps(out["gaps"], ensure_ascii=False),
                     json.dumps(out["reverted"], ensure_ascii=False), out["coverage"][0], out["coverage"][1], status))
        con.commit()
    finally:
        con.close()


class NoGain(Exception):
    pass


class TooLong(Exception):
    """The tailored CV still runs over one page after every guard."""


def _coverage(terms, base, content):
    if not terms:
        return [None, None]
    return [round(checks.coverage(terms, checks.cv_text(base)), 2), round(checks.coverage(terms, checks.cv_text(content)), 2)]


def counts(terms, claimable, base, content):
    """What the bot says instead of a percentage: ad terms N, claimable K, claimable on the page before/after."""
    terms, claimable = checks._unique(terms), checks._unique(claimable)
    on = lambda c: sum(1 for t in claimable if checks.mentions(t, checks.cv_text(c)))
    return {"terms": len(terms), "claimable": len(claimable), "before": on(base), "after": on(content)}


def _no_wording(base, lib, claimable):
    return cover.no_wording(cover.term_index(base, lib, claimable), claimable)


def _check_deadline():
    if llm.DEADLINE is not None and time.monotonic() > llm.DEADLINE:
        raise TimeoutError(f"не уложился в {RUN_DEADLINE // 60} минут")


def tailor_job(job_id, pasted_text=None):
    """-> {status: ok|fallback|need_text, pdf, coverage: [before, after] over the claimable ad terms,
    coverage_all: [before, after] over all ad terms, counts: {terms, claimable, before, after} (None without
    terms), claimable_no_wording: claimable terms no library unit contains, changes, gaps, reverted, answers,
    reason, issues, violations, version}. Coverage values are None when there is nothing to measure."""
    job = _load_job(job_id)
    pasted = (pasted_text or "").strip()
    used_pasted = False
    if pasted and (len(pasted) >= MIN_DESC or len(job["description"].strip()) < MIN_DESC):
        job["description"] = pasted  # a short reply never replaces a real stored description
        if len(pasted) >= MIN_DESC:
            _keep_pasted(job_id, pasted)
    elif not pasted and len(job["description"].strip()) < MIN_DESC:
        try:
            stored = pasted_path(job_id).read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            stored = ""
        if len(stored) >= MIN_DESC:
            job["description"], used_pasted = stored, True
    base = json.loads(BASE_JSON.read_text(encoding="utf-8"))
    out = {"status": "need_text", "pdf": "", "coverage": [None, None], "coverage_all": [None, None], "changes": [],
           "gaps": [], "reverted": [], "answers": "", "reason": "", "issues": [], "violations": [], "version": "",
           "counts": None, "claimable_no_wording": [], "unmet": [], "pdf_issues": [], "used_pasted": used_pasted}
    if len(job["description"].strip()) < MIN_DESC:
        out["reason"] = "нет текста вакансии"
        return out

    facts = load_facts()  # these raise LibraryError naming the broken file: fixed, not sent around
    (never, case_sensitive), numbers = load_rules()
    lib = library.load(base)
    skills_ok = library.load_skills(base)  # the same for the skills whitelist
    vdir = _new_version_dir(job_id)
    name = file_name(job["company"], job["title"])
    out["version"] = str(vdir)
    allowed = checks.cv_text(base) + "\n" + " ".join(numbers)
    words = checks.cv_text(base) + "\n" + facts
    reqs, result, notes = None, None, []
    llm.DEADLINE = time.monotonic() + RUN_DEADLINE
    try:
        reqs = requirements.extract(job)
        (vdir / "requirements.json").write_text(json.dumps(_scrub(reqs), ensure_ascii=False, indent=1), encoding="utf-8")
        terms = reqs["terms"]
        result = tailor.run(base, facts, reqs, lib)
        result = code_checks(base, lib, allowed, words, result, never, case_sensitive, terms, skills_ok)
        _check_deadline()
        pdf, report = render(result["content"], vdir, name)
        if report["pages"] > 1:
            section, lines = overflow(base, result["content"], report)
            try:
                retry = tailor.shorten(base, facts, reqs, lib, result, section, lines)
                retry = code_checks(base, lib, allowed, words, retry, never, case_sensitive, terms, skills_ok)
                retry["reverted"] = _merge_reverted(result["reverted"], retry["reverted"])
                result = retry
                _check_deadline()
                pdf, report = render(result["content"], vdir, name)
            except llm.LLMError as e:
                notes.append(f"shortening retry failed ({section}): {e}")
        if report["pages"] > 1:
            i = longest_role(result["content"])
            result = library.revert(base, lib, result, f"experience[{i}]", "one page: base wording for the longest role")
            _check_deadline()
            pdf, report = render(result["content"], vdir, name)
        if report["pages"] > 1:
            raise TooLong("адаптированная версия не влезла на одну страницу")
        claimable = [t for t in terms if result.get("claimable", {}).get(t)]
        cov = _coverage(claimable, base, result["content"])
        if cov[0] is not None and cov[1] < cov[0]:
            raise NoGain("адаптация не улучшила совпадение")
    except Exception as e:  # LLMError, overflow, no gain, deadline, a crashed Chrome: never leave Misha without a PDF
        out["issues"] = notes
        return fallback(job_id, base, facts, reqs, result, vdir, name, out, e)
    finally:
        llm.DEADLINE = None

    reason = ("не удалось выделить ключевые слова" if not terms else
              "ни одно ключевое слово вакансии не подтверждается фактами" if not claimable else "")
    out.update(status="ok", pdf=str(pdf), changes=result["changes"], gaps=result["gaps"], reason=reason,
               reverted=result["reverted"], violations=result["violations"], answers=answers(facts, reqs),
               coverage=cov, coverage_all=_coverage(terms, base, result["content"]),
               claimable=claimable, counts=counts(terms, claimable, base, result["content"]),
               claimable_no_wording=_no_wording(base, lib, claimable), unmet=_unmet(terms, claimable),
               pdf_issues=report["issues"],
               issues=notes + report["issues"] + [f"term used more than 3 times: {t}" for t in overused(terms, base, result["content"])])
    save(job_id, vdir, out, result["content"], "ok")
    return out


def _keep_pasted(job_id, text):
    try:
        path = pasted_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as e:  # reuse is a convenience; the run goes on
        print(f"pasted text not kept for {job_id}: {e}", file=sys.stderr, flush=True)


def _unmet(terms, claimable):
    """Ad terms the facts do not cover - listed by code, whatever the model wrote as gaps (QA cvtailor-03)."""
    have = {t.lower() for t in claimable}
    return [t for t in checks._unique(terms) if t.lower() not in have]


def _reason(e):
    """Short plain words for Misha; the raw exception (paths, CLI output) stays in result.json and worker.log
    (QA cvtailor-13)."""
    if isinstance(e, llm.LLMError):
        msg = str(e).lower()
        if "deadline" in msg:
            return f"не уложился в {RUN_DEADLINE // 60} минут"
        if "limit" in msg:
            return "лимит Claude исчерпан, попробуй позже"
        if "login" in msg or "auth" in msg or "expired" in msg or "401" in msg:
            return "Claude не залогинен на сервере"
        if "timed out" in msg:
            return "Claude не ответил вовремя"
        if "not found" in msg:
            return "на сервере не найден Claude"
        return "Claude ответил не по формату"
    if isinstance(e, (TooLong, NoGain)) or type(e) is TimeoutError:  # our own messages, plain Russian
        return str(e)
    if isinstance(e, FileNotFoundError):
        return "не собрался PDF (не найден Chrome или файл шаблона)"
    if isinstance(e, (subprocess.SubprocessError, RuntimeError, OSError)):
        return f"не собрался PDF ({type(e).__name__})"
    return f"внутренняя ошибка ({type(e).__name__})"


def fallback(job_id, base, facts, reqs, result, vdir, name, out, error):
    """The base CV under this job's file name, with the reason; the approved PDF if even the build fails."""
    issues = []
    try:
        pdf, report = render(base, vdir, name)
        issues = report["issues"]
    except Exception as e:
        pdf = vdir / name
        shutil.copyfile(APPROVED_PDF, pdf)
        issues = [f"base CV rebuild failed, sent the approved PDF: {type(e).__name__}: {e}"]
    terms = (reqs or {}).get("terms", [])
    claimable = [t for t in terms if (result or {}).get("claimable", {}).get(t)]
    same = lambda ts: [_coverage(ts, base, base)[0]] * 2
    if terms:
        out["counts"] = counts(terms, claimable, base, base)
        try:
            out["claimable_no_wording"] = _no_wording(base, library.load(base), claimable)
        except Exception:  # the report line is a bonus; a fallback must still go out
            pass
    out.update(status="fallback", pdf=str(pdf), reason=_reason(error), coverage=same(claimable),
               coverage_all=same(terms), gaps=(result or {}).get("gaps", []), answers=answers(facts, reqs),
               unmet=_unmet(terms, claimable) if terms else [], pdf_issues=issues,
               issues=out["issues"] + issues + [f"error: {type(error).__name__}: {str(error)[:300]}"],
               violations=(result or {}).get("violations", []))
    save(job_id, vdir, out, base, "fallback")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Tailor the base CV to one job id.")
    ap.add_argument("job_id")
    ap.add_argument("--text", help="file with the ad text, when the job has no description")
    args = ap.parse_args(argv)
    pasted = Path(args.text).read_text(encoding="utf-8") if args.text else None
    try:
        result = tailor_job(args.job_id, pasted)
    except (LookupError, library.LibraryError) as e:
        print(json.dumps({"status": "error", "reason": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
