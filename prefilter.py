"""Stage 3 rule pre-filter: cheap rules from the approved filter page, applied
before Claude scores anything. Each dropped job keeps the rule that dropped it,
so the rules can be checked against Misha's labels. Reads and writes store.OUT (JOBDIGEST_OUT in tests).
Reads only today's files: a missing email/Adzuna table or yesterday's company feed is reported, not reused."""

import csv
import json
import re
import sys
from datetime import date

import store

csv.field_size_limit(10_000_000)  # one hostile 200 KB line in an alert mail must not stop the morning (security-02)
CAPS = {"title": 300, "company": 200, "location": 200, "url": 2000, "teaser": 1000, "posted": 10}

# Target roles or student formats. Anything else is not our field.
TARGET = re.compile(
    r"^(?!.*(?:production|produktion))(?=.*(?:product|produkt|analyst|analytics|market(ing)? research|marktforschung"
    r"|market intelligence|growth|praktik|intern\b|internship|trainee|einsteiger|berufseinst|junior))",
    re.I)

SENIOR = re.compile(r"\b(senior|sr\.?|lead|head|principal|director|chief|vp|vice\s+president|leiter|leitung"
                    r"|gruppenleit\w*|teamlead|teamleiter"
                    r"|expert(in)?|architect|manager:in mit mehrjähriger)\b", re.I)

# Fields Misha rejected on 05.10.2026, plus business/finance/quant/fraud roles dropped on 06.10.2026.
# Hard: no AI or automation in the title lifts these. Sales roles moved out on 04.11.2026 (owner); field sales stays.
EXCLUDED = re.compile(
    r"content|social media|community|bi analyst|business intelligence|\bbi\b"
    r"|financ\w* analyst|treasury|quantitative|quant\b|fraud|steuer|tax\b|audit|prüfung|aktuar|versicherungsmathem|jurist|legal"
    r"|call ?cent|kundenservice|customer (service|support)|rezeption|hotel|verk[aä]uf|filial|einzelhandel"
    # 04.11.2026 round 2: back-office sales support (assistant, office, support) stays out, as before.
    r"|außendienst|aussendienst|sales\s+(?:assistant|office|support)"
    # 'ingenieur' not after 'wirtschafts' (Wirtschaftsingenieur) and not as 'Ingenieurwesen' (round 2, N3)
    r"|developer|engineer|devops|entwickler|techniker|elektriker|automatisierungstechnik"
    r"|(?<!wirtschafts)(?<!wirtschafts-)ingenieur(?!wesen)|data\s+scientist"
    r"|accountant|buchhalt|controller"
    r"|lehrling|lehre\b|lehrstelle|ausbildung"
    # open applications are not vacancies: company boards list them next to real jobs (wave 2, 26.10.2026)
    r"|initiativ(bewerbung)?\b|open application|spontaneous application|general application|blindbewerbung"
    r"|talent ?pool"
    r"|apprentice",  # apprenticeships are not for Misha (product 09.10.2026)
    re.I)

# Student formats (30.10.2026, owner): the job itself needs a student, and Misha is not enrolled. Hard: no AI word
# lifts these. 'Student Recruiting' is a recruiting role about students, so the gender-marker rule skips it.
STUDENT_FORMAT = re.compile("|".join([
    r"\bwerkstudent\w*", r"\bworking\s+student", r"\bstudent[\s-]*jobs?\b(?!\.[a-z])",
    r"\bstudent[\s-]*workers?\b",
    r"\bstudentenjob\w*",
    r"\bstudentisch\w*", r"\bstudent\s+(?:assistant|position|support)",
    r"^\s*student\s+(?!recruit)[^()]*\((?:[mwdfx]/[mwdfx](?:/[mwdfx])?|all\s+genders)\)",
    r"\bpflicht[\s-]*praktik\w*", r"\bstudienbegleitend\w*",
    r"\b(?:fh|uni)\b[\s/-]*(?:(?:fh|uni)\b[\s/-]*)?praktik\w*", r"\bf[üu]r\s+studierende",
]), re.I)

# Soft (wave 3, 26.10.2026): only an AI_EDGE title gets past these.
SOFT_EXCLUDED = re.compile(
    r"data analyst|business analyst|business operations|founder'?s associate|project coordinator|projektkoordinat",
    re.I)

# AI or automation work in the title (wave 3). Passes TARGET and lifts SOFT_EXCLUDED, never EXCLUDED.
# Agents count only as AI agents: a customer or backoffice 'Agent' is not AI work (26.10.2026).
AI_EDGE = re.compile(
    r"\b(ai|ki|genai)\b|artificial\s+intelligence|künstliche\s+intelligenz|automation|automatisierung"
    r"|\b(ai|ki)[\s-]+agents?\b|\bagentic\b|digitale?\s+transformation",
    re.I)

# Graduate programmes (wave 3): a pass of their own, so the preview shows them apart from TARGET.
GRADUATE = re.compile(r"\bgraduate", re.I)

# Role groups added to TARGET (wave 3), one switch each. enabled=False: the group stops passing and its jobs drop
# as not_target_role. Misha decides per group after the preview. Order = credit order in pass_reason().
ROLE_GROUPS = {
    "venture": (re.compile(r"incubator|inkubator|accelerator|\bventures?\b|entrepreneur[\s-]+in[\s-]+residence|\beir\b",
                           re.I), True),
    # Lookahead veto: a deal, M&A, transaction or valuation title never passes this group.
    "strategy_research": (re.compile(
        r"^(?!.*(?:\bdeals?\b|\bm&a\b|transaction|valuation))(?=.*(?:"
        r"strateg(?:y|ie).*\b(?:associate|analyst|assistenz)|\b(?:associate|analyst|assistenz).*strateg(?:y|ie)"
        r"|research\s+associate|insights?\s+(?:associate|analyst|manager)))", re.I), True),
    "program_coordination": (re.compile(
        r"\bprogram(?:me)?\s+coordinator|programmkoordinat|\bproject\s+associate", re.I), True),
    "operations_ai": (re.compile(
        r"\b(?:ai|product|revenue|marketing|growth)\s+operations|\brevops\b|\bai\s+governance|business\s+enablement",
        re.I), True),
    "crm_lifecycle": (re.compile(r"\bcrm\b|lifecycle|retention", re.I), True),
    # Both word orders: "Digital Consultant" and "Consultant Digital" are the same job.
    "digital_consulting": (re.compile(
        r"\b(?:digital\w*|technology|transformation|digitalisierung\w*).*(?:consultant|berater)"
        r"|(?:consultant|berater).*\b(?:digital\w*|technology|transformation|digitalisierung\w*)", re.I), True),
    "marketing_associate": (re.compile(r"\bmarketing\s+(?:associate|executive|operations)", re.I), True),
    "customer_success": (re.compile(r"\b(?:customer|client)\s+success|kundenerfolg", re.I), True),
    # owner 04.11.2026; 'Vertrieb' titles not included (German field/insurance sales).
    "sales": (re.compile(r"\bsales\b|\bsdr\b|\bbdr\b|business\s+development|\bbizdev\b|account\s+(?:executive|manager)"
                         r"|\bpartnerships?\b|\bpartner\s+(?:manager|sales)", re.I), True),
}

# A required German level (C1/C2, Muttersprache, fluent) drops a job unless SOFTENED stands next to it.
# The forms in the last two alternatives are written as the postings print them, e.g. 'Deutsch (C2)', 'Deutsch: C1'.
# 04.11.2026: 'fließendes/sehr gutes Deutsch' were missed; adjective endings (-e, -en, -es, -em) now match too.
GERMAN = re.compile(
    r"(sehr gut|ausgezeichnet|exzellent|fließend|fliessend|verhandlungssicher|perfekt|hervorragend)(?:e[nsm]?)?\s+deutsch"
    r"|deutsch\s+(?:fließend|fliessend)"
    r"|deutsch(?:kenntnisse|sprachkenntnisse|sprache)?[\s(:\-–—]*(?:auf\s+|niveau\s+)?(?:c1|c2|muttersprach\w*)\)?"
    r"|deutsch als muttersprache"
    r"|fluent (in )?german|german (c1|c2|native)|native german|excellent german|business fluent german",
    re.I)
# 'Fluent German is not required', 'Sehr gute Deutschkenntnisse von Vorteil': the requirement is softened
# right next to it, so Claude judges the job instead of the rule dropping it (pipeline-13).
SOFTENED = re.compile(r"not (required|necessary|needed|a must)|no need|(is|would be|are) (a )?(plus|advantage|bonus)"
                      r"|an advantage|a plus|nice to have|preferred|desirable|optional|nicht (erforderlich|notwendig|nötig"
                      r"|zwingend|voraussetzung)|von vorteil|vorteilhaft|wünschenswert|ein plus|kein muss", re.I)


def german_phrase(text):
    """The first required-German phrase in the text, or '' when there is none (softened ones do not count)."""
    for m in GERMAN.finditer(text or ""):
        if not SOFTENED.search(text[max(0, m.start() - 40): m.end() + 60]):
            return m.group(0)
    return ""


def german_required(text):
    return bool(german_phrase(text))


def excluded(title):
    """Hard exclusions and student formats always drop a title; soft ones drop it unless AI_EDGE lifts them."""
    return bool(EXCLUDED.search(title) or STUDENT_FORMAT.search(title)
                or (SOFT_EXCLUDED.search(title) and not AI_EDGE.search(title)))


def pass_reason(title):
    """The rule that lets a title pass TARGET: target, ai_edge, graduate or an enabled role group; None if none does.
    First match wins, so a group is credited only with the jobs no earlier rule keeps. Switching it off then drops
    exactly the jobs it added on its own."""
    if TARGET.search(title):
        return "target"
    if AI_EDGE.search(title):
        return "ai_edge"
    if GRADUATE.search(title):
        return "graduate"
    for name, (pattern, enabled) in ROLE_GROUPS.items():
        if enabled and pattern.search(title):
            return name
    return None


def reason(job):
    """Which rule lets the job through the title check; None when the title check drops it. A soft exclusion is
    lifted only by AI_EDGE, so that is the rule credited for it. Used by scripts/wave3_preview.py."""
    title = job["title"]
    if SENIOR.search(title) or excluded(title):
        return None
    if SOFT_EXCLUDED.search(title):
        return "ai_edge"
    return pass_reason(title)


def verdict(job):
    title = job["title"]
    if SENIOR.search(title):
        return "senior"
    if excluded(title):
        return "excluded_field"
    if pass_reason(title) is None:
        return "not_target_role"
    if german_required(job.get("description", "")):
        return "german_required"
    return "keep"


def capped(row):
    return {k: (row.get(k) or "")[:CAPS[k]] if k in CAPS else row.get(k) for k in row}


def load_trial(today=None):
    """Today's email/Adzuna table from collect.py (no descriptions there; the alert's hours, contract and
    teaser line goes to Claude as 'teaser', pipeline-09). Only trial-<today>.csv: a stray or old table is
    never taken for today's (ops-15)."""
    path = store.OUT / f"trial-{(today or date.today()).isoformat()}.csv"
    if not path.exists():
        print(f"no email/Adzuna table for today ({path.name}): collect.py failed or did not run", file=sys.stderr)
        return []
    rows = []
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            r = {k: v for k, v in r.items() if isinstance(k, str)}  # an over-long row has extra cells under None
            rows.append(capped(dict(source=r.get("source") or "", company=r.get("company") or "",
                                    title=r.get("title") or "", location=r.get("location") or "",
                                    url=r.get("url") or "", teaser=r.get("extra") or "", posted=r.get("posted") or "",
                                    description="", niche="", top=False)))
    return rows


def load_companies():
    """Today's company feeds; yesterday's file (fetch_companies.py killed or skipped) is not today's feed."""
    if not store.fresh_out("company_jobs.json"):
        print("company_jobs.json is not from this morning: company feeds left out today", file=sys.stderr)
        return []
    try:
        with open(store.OUT / "company_jobs.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"company_jobs.json unreadable: {type(e).__name__}", file=sys.stderr)
        return []


VIENNA = re.compile(r"wien|vienna", re.I)


def main():
    jobs = [store.scrub(j) for j in load_companies() + load_trial() if isinstance(j, dict) and j.get("title")]
    seen, uniq = {}, []
    for j in jobs:
        title = re.sub(r"\s*-\s*ID:\s*\d+|\((m/w/d|w/m/d|f/m/d|m/f/d|w/m/x|m/f/x|f/m/x|all genders)\)", "", j["title"], flags=re.I)
        company = re.split(r"\s+[\(\[]", j.get("company") or "")[0]
        # no company: the ad's own link tells two employer-less ads apart (data-2)
        k = (company.lower() or "?" + (j.get("url") or ""), re.sub(r"\W+", " ", title.lower()).strip())
        if k not in seen:
            seen[k] = len(uniq)
            uniq.append(j)
        elif VIENNA.search(j.get("location") or "") and not VIENNA.search(uniq[seen[k]].get("location") or ""):
            uniq[seen[k]] = j  # same job posted for Graz and Vienna: Misha lives in Vienna (pipeline-03)
    counts = {}
    for j in uniq:
        j["rule"] = verdict(j)
        counts[j["rule"]] = counts.get(j["rule"], 0) + 1
    with open(store.OUT / "prefiltered.json", "w", encoding="utf-8") as f:
        json.dump(uniq, f, ensure_ascii=False, indent=1)
    print(f"unique jobs {len(uniq)}:", counts)


if __name__ == "__main__":
    main()
