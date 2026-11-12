"""Code checks on a tailored CV that never trust the model: locked fields copied back from the base,
markup limited to what the renderer prints as formatting, blocklisted tools, numbers that are not in the
base or the curated fact list, German words, keyword coverage, length budgets.
Pure functions; the orchestrator (run.py) decides what to revert.
Every check reads the same text the PDF prints: only <b>/<i> are removed, nothing else is hidden."""

import copy
import difflib
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # cv/
NEVER_CLAIM = ROOT / "never_claim.json"
ALLOWED_NUMBERS = ROOT / "allowed_numbers.json"
SKILLS_ALLOWED = ROOT / "skills_allowed.json"
MAX_SKILL_ITEMS = 10    # items per skill line (QA 09.10: no line is longer than the page allows anyway)
MAX_SKILLS_RAW = 600    # characters of one model skills line read at all; 20k items took 15 s (QA 09.10)
MAX_SKILL_LINES = 20    # skill lines read from the model answer

LOCKED_TOP = ("lang", "name", "contacts", "headline", "education", "certifications", "languages", "extra",
              "labels", "order")
LOCKED_ROLE = ("role", "org", "dates", "location")

ZERO_WIDTH = re.compile("[­​-‏ -‮⁠-⁤﻿]")
# A number token: digits not glued to a word (GA4, B1, Web3 are names, not figures), with grouping,
# decimals and the suffixes that make a claim: + % x/× k/m/bn, ordinals, #1, ×3.
NUM = re.compile(r"(?<![A-Za-z\d])(?:#|[x×](?=\d))?\d+(?:[,.  ]\d{3}(?!\d))*(?:[.,]\d+)?"
                 r"(?:(?:k|m|mn|bn)(?![A-Za-z]))?(?:\+|%|[x×](?![A-Za-z])|(?:st|nd|rd|th)(?![A-Za-z]))?", re.I)
NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million", "billion",
    "dozen", "dozens", "hundreds", "thousands", "millions", "billions", "double", "doubled", "doubling",
    "triple", "tripled", "tripling", "quadruple", "quadrupled", "first", "twice", "thrice",
}
FOLD = re.compile(r"\b(?:two|three|four|five|six|seven|eight|nine|ten|twenty|hundred|thousand|many|\d+)-?fold\b",
                  re.I)                            # twofold, two-fold, tenfold, 3-fold
HALF = re.compile(r"\bhalf\s+a\s+(?:million|thousand|billion)\b", re.I)
# Unambiguous German words (no English homographs like 'also', 'in', 'will', 'art'): a hit means the CV
# kept German wording from a German ad (Misha 08.10.2026: English only).
GERMAN_STOP = {
    "und", "oder", "der", "die", "das", "den", "dem", "des", "mit", "für", "von", "zur", "zum", "ein", "eine",
    "einer", "eines", "ist", "sind", "wir", "nicht", "auch", "bei", "auf", "nach", "über", "sowie", "sehr",
    "gute", "guten", "kenntnisse", "erfahrung", "erfahrungen", "aufgaben", "anforderungen", "unternehmen",
    "bereich", "mitarbeiter", "mitarbeiterin", "marktforschung", "produktmanagement", "projektmanagement",
    "vertrieb", "kunden", "entwicklung", "deutschkenntnisse", "englischkenntnisse", "teilzeit", "vollzeit",
    "abgeschlossenes", "studium", "werkstudent", "werkstudentin", "praktikum", "verantwortung", "umsetzung",
    "betreuung", "unterstützung", "durchführung", "erstellung", "zusammenarbeit", "kommunikation",
    "selbstständige", "arbeitsweise", "fähigkeit", "wünschenswert", "vorteil", "idealerweise",
}
LINE_CHARS = 105   # prompt hint only: bullets up to about this many characters stay on one line


# ---------- visible text ----------

def scrub(text):
    """Drop lone surrogates (a JSON '\\ud800' from the model): they crash every UTF-8 write."""
    return re.sub("[\ud800-\udfff]", "", text or "")


def clean(text):
    """What a model string becomes before any check: NFKC, no zero-width/control chars, single spaces."""
    text = unicodedata.normalize("NFKC", scrub(text)).replace("\u2014", "-").replace("\u2013", "-")  # Misha: plain hyphens only
    return re.sub(r"\s+", " ", ZERO_WIDTH.sub("", text)).strip()


def strip_tags(text):
    """Visible text: only <b>/<i> are formatting; anything else in angle brackets is printed, so it stays."""
    return re.sub(r"</?[bi]>", "", clean(text))


def markup_ok(text, rich=True):
    """rich fields (summary, bullets) may carry balanced <b>/<i>; skills print raw text, so no brackets at all."""
    if not rich:
        return "<" not in text and ">" not in text
    stack = []
    for m in re.finditer(r"<(/?)([^<>]*)>|[<>]", text):
        if m.group(0) in ("<", ">") or m.group(2) not in ("b", "i"):
            return False
        if m.group(1):
            if not stack or stack.pop() != m.group(2):
                return False
        else:
            stack.append(m.group(2))
    return not stack


def cv_text(content):
    """All visible text in render order (mirrors cv/source/build.py), one block per line."""
    c, L = content, content.get("labels", {})
    lines = [c.get("name", "")]
    if c.get("headline"):
        lines.append(c["headline"])
    lines.append(" | ".join(x.get("text", "") for x in c.get("contacts", [])))
    for key in c.get("order", []):
        if key == "summary" and c.get("summary"):
            lines += [L.get(key, key), c["summary"]]
        elif key == "experience":
            lines.append(L.get(key, key))
            for e in c.get("experience", []):
                lines.append(f'{e.get("role", "")} {e.get("dates", "")}')
                lines.append(f'{e.get("org", "")} {e.get("location", "")}'.strip())
                lines += e.get("bullets", [])
        elif key == "education":
            lines.append(L.get(key, key))
            for e in c.get("education", []):
                lines += [f'{e.get("school", "")} {e.get("dates", "")}',
                          f'{e.get("degree", "")} {e.get("location", "")}'.strip()]
                if e.get("thesis"):
                    lines.append(f'{L.get("thesis", "Thesis")}: {e["thesis"]}')
        elif key == "certifications":
            lines.append(L.get(key, key))
            lines += [f'{x["title"]}, {x["issuer"]} | {L.get("verify", "verify")} {x["dates"]}'
                      for x in c.get("certifications", [])]
        elif key == "skills":
            lines.append(L.get(key, key))
            lines += [f'{k["group"]}: {k["items"]}' for k in c.get("skills", [])]
        elif key == "languages":
            lines.append(L.get(key, key))
            lines.append(", ".join(f'{x["name"]} ({x["level"]})' for x in c.get("languages", [])))
        elif key == "extra" and c.get("extra"):
            lines.append(L.get(key, key))
            lines += [f'{x["title"]}: {x["text"]} {x["dates"]}' for x in c["extra"]]
    return "\n".join(strip_tags(x) for x in lines)


# ---------- locked fields ----------

def _same(a, b):
    """Equal up to whitespace: a model re-emitting 'Product Manager ' did not change the title."""
    norm = lambda v: json.loads(re.sub(r"\s+", " ", json.dumps(v, ensure_ascii=False)))
    return norm(a) == norm(b)


def enforce_locked(base, new):
    """Copy every locked field from base into new. A field the model left out is copied silently; one it
    supplied with a different value is restored and listed as a violation. Roles stay in base order;
    bullets must be strings, within the base count +-1 (the length guard later trims to the base count)."""
    new = copy.deepcopy(new) if isinstance(new, dict) else {}
    out, violations = copy.deepcopy(base), []
    for key in LOCKED_TOP:
        if key in new and key in base and not _same(new[key], base[key]):
            violations.append(key)
    if isinstance(new.get("summary"), str) and clean(new["summary"]):
        out["summary"] = clean(new["summary"])
    skills = new.get("skills")
    if isinstance(skills, list) and all(isinstance(k, dict) for k in skills):
        out["skills"] = lock_skill_groups(base, skills)
    elif "skills" in new:
        violations.append("skills")
    roles = new.get("experience") if isinstance(new.get("experience"), list) else []
    if len(roles) > len(base["experience"]):
        violations.append(f"experience[{len(base['experience'])}]")  # an invented role
    for i, b in enumerate(base["experience"]):
        e = roles[i] if i < len(roles) and isinstance(roles[i], dict) else None
        if e is None:
            if "experience" in new:
                violations.append(f"experience[{i}]")
            continue
        for key in LOCKED_ROLE:
            if key in e and not _same(e[key], b.get(key, "")):
                violations.append(f"experience[{i}].{key}")
        bullets = e.get("bullets")
        if isinstance(bullets, list) and all(isinstance(x, str) for x in bullets):
            bullets = [clean(x) for x in bullets if strip_tags(x)]  # empty and '<b></b>' bullets are dropped
        if isinstance(bullets, list) and all(isinstance(x, str) for x in bullets) and bullets \
                and abs(len(bullets) - len(b["bullets"])) <= 1:
            out["experience"][i]["bullets"] = bullets
        elif bullets is not None:
            violations.append(f"experience[{i}].bullets")
    return out, violations


def lock_skill_groups(base, skills):
    """Skill groups are locked by name: the output has exactly the base groups (order may change). A model
    group matching a base name keeps its items under the base spelling; an unknown or empty group is
    ignored, and any base group left out comes back with its base line at its base position."""
    names = {_fold(s["group"]): s for s in base["skills"]}
    out, used = [], set()
    for k in skills[:MAX_SKILL_LINES]:
        if not isinstance(k, dict):
            continue
        g = clean(k.get("group")[:200]) if isinstance(k.get("group"), str) else ""
        items = clean(k.get("items")[:MAX_SKILLS_RAW]) if isinstance(k.get("items"), str) else ""
        b = names.get(_fold(g))
        if b and _fold(g) not in used and items:
            used.add(_fold(g))
            out.append({"group": b["group"], "items": items})
    for n, b in enumerate(base["skills"]):
        if _fold(b["group"]) not in used:
            out.insert(min(n, len(out)), copy.deepcopy(b))
    return out


# ---------- blocklist ----------

def load_never_claim(path=NEVER_CLAIM):
    """(terms, case_sensitive) from cv/never_claim.json; ValueError naming the file when its shape is wrong
    (QA cvtailor-14: '{}' was reported as a library.json error)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    strs = lambda v: isinstance(v, list) and all(isinstance(t, str) and t.strip() for t in v)
    if not isinstance(data, dict) or not strs(data.get("terms")) or not data["terms"] \
            or not strs(data.get("case_sensitive", [])):
        raise ValueError(f"{Path(path).name}: must be {{\"terms\": [str, ...], \"case_sensitive\": [str, ...]}}")
    return data["terms"], data.get("case_sensitive", [])


def _term_parts(term):
    """'ChatGPT' -> Chat|G|P|T, 'Power BI' -> Power|B|I, 'A/B test' -> A|B|test: the places where a
    writer may put a space, dot or hyphen (chat-gpt, S.Q.L, Power-BI, PowerBI, AB test)."""
    parts, cur, prev = [], "", ""
    for ch in term:
        if ch in " ./-":
            if cur:
                parts.append(cur)
            cur, prev = "", ""
            continue
        if cur and ((prev.islower() and ch.isupper()) or (prev.isupper() and ch.isupper())
                    or (prev.isalpha() != ch.isalpha())):
            parts.append(cur)
            cur = ""
        cur += ch
        prev = ch
    if cur:
        parts.append(cur)
    return parts


def _term_re(term, case_sensitive=False):
    if len(term) == 1:
        pattern = rf"(?<![\w&/.'-]){re.escape(term)}(?![\w&/'-]|\.\w)"  # 'R', 'in R.', 'R,' but not 'R&D', 'R.E.'
    else:
        body = r"[\s./\-]*".join(re.escape(p) for p in _term_parts(term))
        pattern = rf"(?<!\w){body}(?:s|es|ing|\d+)?(?!\w)"           # plurals, 'testing', 'python3'
    return re.compile(pattern, 0 if case_sensitive else re.I)


def _sentence_start(text, pos):
    before = text[:pos].rstrip()
    return not before or before[-1] in ".!?"


def blocklist_hits(text, terms, case_sensitive=()):
    """Blocklisted terms found in the visible text (whole word, case-insensitive unless listed in
    case_sensitive). A case-sensitive word that is also English ('Linear') counts only mid-sentence or
    in a skills line, so 'Linear growth' at the start of a bullet is not the tool."""
    text = strip_tags(text)
    hits = []
    for t in terms:
        cs = t in case_sensitive
        for m in _term_re(t, cs).finditer(text):
            if cs and len(t) > 1 and _sentence_start(text, m.start()):
                continue
            hits.append(t)
            break
    return hits


# ---------- numbers ----------

def _canon(tok):
    tok = tok.lower().replace("×", "x")
    return re.sub(r"(?<=\d)[,.  ](?=\d{3}(?!\d))", "", tok)   # 150,000 = 385 000 = 385000


def load_allowed_numbers(path=ALLOWED_NUMBERS):
    """Numbers from facts.md that may appear in a CV (meta dates like 08.10.2026 are not in it)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    nums = data.get("numbers") if isinstance(data, dict) else None
    if not isinstance(nums, list) or not all(isinstance(x, dict) and isinstance(x.get("value"), (str, int, float))
                                             for x in nums):
        raise ValueError(f"{Path(path).name}: must be {{\"numbers\": [{{\"value\": ...}}, ...]}}")
    return [str(x["value"]) for x in nums]


def _allowed_numbers(allowed_text):
    allowed = set()
    for tok in NUM.findall(strip_tags(allowed_text)):
        c = _canon(tok)
        allowed |= {c, c.rstrip("+")}
    return allowed


def new_numbers(allowed_text, text, words_text=None):
    """Number tokens in text that allowed_text never states, and count/growth words (three, doubled,
    dozens, tenfold, half a million) that words_text (default: allowed_text) never uses.
    Dropping a '+' is fine (120,000+ -> 120,000); adding one is not (5 -> 5+)."""
    allowed = _allowed_numbers(allowed_text)
    words_seen = {w.lower() for w in re.findall(r"[A-Za-z]+(?:-?fold)?", strip_tags(words_text or allowed_text))}
    text = strip_tags(text)
    out = [t for t in NUM.findall(text) if _canon(t) not in allowed and _canon(t) + "+" not in allowed]
    out += [m.group(0) for m in HALF.finditer(text) if m.group(0).lower() not in (words_text or allowed_text).lower()]
    out += [m.group(0) for m in FOLD.finditer(text) if m.group(0).lower() not in words_seen]
    for word in re.findall(r"[A-Za-z]+", FOLD.sub(" ", text)):
        if word.lower() in NUMBER_WORDS and word.lower() not in words_seen:
            out.append(word)
    return list(dict.fromkeys(out))


GERMAN_SHAPE = re.compile(r"^[a-zäöüß]{3,}(?:ungs|heits|keits|schafts|ions)[a-zäöüß]{4,}$"   # Anforderungsmanagement
                          r"|^[a-zäöüß]{4,}(?:ung|ungen|heit|keit|schaft|ierung)$")             # Leistung, Planung


def german_words(text):
    """Stop-list words, words with umlauts/ß (except the ÖSD certificate) and German word shapes."""
    words = re.findall(r"[A-Za-zÄÖÜäöüß]+", strip_tags(text))
    return list(dict.fromkeys(w for w in words if w.lower() != "ösd" and (
        w.lower() in GERMAN_STOP or re.search("[äöüß]", w.lower()) or GERMAN_SHAPE.match(w.lower()))))


# ---------- keyword coverage ----------

def _term_pattern(term):
    """'Product Owner' also matches 'Product Owners', 'market research' matches 'market-research'."""
    words = [w for w in re.split(r"[\s\-]+", term.strip()) if w]
    if not words:
        return None
    last = words[-1]
    if len(last) > 3 and last.lower().endswith("s") and not last.lower().endswith("ss"):
        words[-1] = last[:-1]
    body = r"[\s\-]*".join(re.escape(w) for w in words)
    return re.compile(rf"(?<!\w){body}(?:s|es)?(?!\w)", re.I)


def _count(term, text):
    p = _term_pattern(term)
    return len(p.findall(text)) if p else 0


def _unique(terms):
    seen, out = set(), []
    for t in terms:
        if isinstance(t, str) and t.strip() and t.strip().lower() not in seen:
            seen.add(t.strip().lower())
            out.append(t.strip())
    return out


def coverage(terms, text):
    """Share of the ad's key terms found as phrases (case-insensitive, simple plurals, hyphen/space); no terms -> 1.0."""
    terms = _unique(terms)
    if not terms:
        return 1.0
    text = strip_tags(text)
    return sum(1 for t in terms if _count(t, text)) / len(terms)


def overused(terms, text, limit=3):
    text = strip_tags(text)
    return [t for t in _unique(terms) if _count(t, text) > limit]


def mentions(term, text):
    return _count(term, strip_tags(text)) > 0


# ---------- editable fields ----------

def _norm_text(s):
    """Visible text for 'is this the base line?': case-sensitive, so a case-only edit is a change."""
    return strip_tags(s)


def _fold(s):
    """Case-insensitive form for duplicates, moved bullets and similarity."""
    return strip_tags(s).lower()


def fields(content):
    """(path, text) of every field the tailoring may change: summary, bullets, skill lines."""
    out = [("summary", content.get("summary", ""))]
    for i, e in enumerate(content.get("experience", [])):
        out += [(f"experience[{i}].bullets[{j}]", b) for j, b in enumerate(e.get("bullets", []))]
    out += [(f"skills[{k}]", f'{s["group"]}: {s["items"]}') for k, s in enumerate(content.get("skills", []))]
    return out


def changed_fields(base, content):
    """Fields whose text is not already in the base at that place (reordering alone is not a change)."""
    base_bullets = [{_norm_text(b) for b in e["bullets"]} for e in base["experience"]]
    base_skills = {_norm_text(f'{s["group"]}: {s["items"]}') for s in base["skills"]}
    out = []
    for path, text in fields(content):
        m = re.match(r"experience\[(\d+)\]", path)
        if path == "summary":
            same = _norm_text(text) == _norm_text(base.get("summary", ""))
        elif m:
            same = _norm_text(text) in base_bullets[int(m.group(1))]
        else:
            same = _norm_text(text) in base_skills
        if not same:
            out.append((path, text))
    return out


def get_field(content, path):
    for p, text in fields(content):
        if p == path or (path.startswith(p + ".")):
            return text
    return None


def revert_field(base, content, path, before=None):
    """Put base wording back into one field. A bullet goes back to the base bullet it replaced (`before`, or
    the most similar base bullet of that role not already shown); a bullet with no base counterpart is
    dropped. Returns (new content, restored base text or None if the field was dropped)."""
    out = copy.deepcopy(content)
    if path == "summary":
        out["summary"] = base.get("summary", "")
        return out, out["summary"]
    m = re.match(r"experience\[(\d+)\]\.bullets\[(\d+)\]$", path)
    if m:
        i, j = int(m.group(1)), int(m.group(2))
        bullets = out["experience"][i]["bullets"]
        if j >= len(bullets):
            return out, None
        shown = {_norm_text(b) for k, b in enumerate(bullets) if k != j}
        unused = [b for b in base["experience"][i]["bullets"] if _norm_text(b) not in shown]
        pick = next((b for b in unused if before and _norm_text(b) == _norm_text(before)), None)
        if pick is None and unused:
            score, best = max((difflib.SequenceMatcher(None, _fold(bullets[j]), _fold(b)).ratio(), b)
                              for b in unused)
            pick = best if score >= 0.4 else None
        if pick is None:
            bullets.pop(j)
            return out, None
        bullets[j] = pick
        return out, pick
    m = re.match(r"skills\[(\d+)\]", path)
    if m:
        k = int(m.group(1))
        if k >= len(out["skills"]):
            return out, None
        group = _fold(out["skills"][k]["group"])
        match = next((s for s in base["skills"] if _fold(s["group"]) == group), None)
        if match is None:
            out["skills"].pop(k)
            return out, None
        out["skills"][k] = copy.deepcopy(match)
        return out, f'{match["group"]}: {match["items"]}'
    raise ValueError(f"not an editable field: {path}")


def live_changes(result):
    """Keep only change-log entries whose new text is still in the CV (after reverts and cuts)."""
    shown = {_norm_text(t) for _, t in fields(result["content"])}
    shown |= {_norm_text(s["items"]) for s in result["content"].get("skills", [])}
    return [c for c in result.get("changes", []) if _norm_text(c["after"]) in shown]


def _order(path):
    """Revert later list items first, so dropping a bullet never shifts one still to be reverted."""
    return tuple(int(n) for n in re.findall(r"\d+", path)) or (-1,)


def revert_many(base, result, flagged):
    """flagged: [(path, why)] -> result with those fields at base wording, listed in result['reverted']."""
    reverted = list(result.get("reverted", []))
    content = result["content"]
    befores = {c["field"]: c["before"] for c in result.get("changes", [])}
    done = set()
    for path, why in sorted(flagged, key=lambda f: _order(f[0]), reverse=True):
        text = get_field(content, path)
        if text is None or path in done:
            continue
        done.add(path)
        content, restored = revert_field(base, content, path, befores.get(path))
        reverted.append({"field": path, "text": strip_tags(text), "base": strip_tags(restored or ""), "why": why})
    out = dict(result, content=content, reverted=reverted)
    out["changes"] = live_changes(out)
    return out


# ---------- structure and length ----------

def bad_markup(content):
    """[(path, why)] for fields whose brackets would print as text or break the layout."""
    out = []
    for path, text in fields(content):
        if not markup_ok(text, rich=not path.startswith("skills")):
            out.append((path, "markup: only balanced <b>/<i> in bullets and summary, none in skills"))
    return out


def uncovered(text):
    """Characters PT Serif has no glyph for (emoji, arrows, CJK): they print in a fallback font of unknown
    width, so such a line cannot be measured and goes back to base wording."""
    from . import measure
    cmap = measure._font("Regular")[2]
    return sorted({ch for ch in strip_tags(text) if not cmap.get(ord(ch))})


def mixed_script(text):
    """Words mixing Latin with Cyrillic or Greek letters ('Pyth\u043en'): look-alikes that dodge the blocklist."""
    out = []
    for w in re.findall(r"\w+", strip_tags(text)):
        latin = any("a" <= ch.lower() <= "z" for ch in w)
        other = any("\u0370" <= ch <= "\u03ff" or "\u0400" <= ch <= "\u04ff" for ch in w)
        if latin and other:
            out.append(w)
    return out


def bad_glyphs(content):
    """[(path, why)] for fields with characters outside the bundled font or mixed-script words."""
    out = []
    for path, text in fields(content):
        if miss := uncovered(text):
            out.append((path, "glyph not in PT Serif: " + " ".join(miss[:5])))
        elif mixed := mixed_script(text):
            out.append((path, "mixed-script word: " + ", ".join(mixed[:3])))
    return out


def strip_new_formatting(base, content):
    """The model may not add bold/italic: a <b>/<i> span whose text is not a formatted span of the base
    loses its tags (base spans like '120,000+ users' stay). Run only on fields with valid markup."""
    spans = set()
    for _, text in fields(base):
        spans |= {(t, inner) for t, inner in re.findall(r"<([bi])>(.*?)</\1>", text)}
    content = copy.deepcopy(content)

    def fix(text):
        if not markup_ok(text):
            return text
        return re.sub(r"<([bi])>(.*?)</\1>", lambda m: m.group(0) if (m.group(1), m.group(2)) in spans else m.group(2), text)
    content["summary"] = fix(content.get("summary", ""))
    for e in content["experience"]:
        e["bullets"] = [fix(b) for b in e["bullets"]]
    return content


def dedupe_bullets(result):
    """A bullet repeated inside one role is dropped (the later copy)."""
    content = copy.deepcopy(result["content"])
    reverted = list(result.get("reverted", []))
    for i, e in enumerate(content["experience"]):
        seen, keep = set(), []
        for j, b in enumerate(e["bullets"]):
            if _fold(b) in seen:
                reverted.append({"field": f"experience[{i}].bullets[{j}]", "text": strip_tags(b), "base": "",
                                 "why": "duplicate bullet dropped"})
                continue
            seen.add(_fold(b))
            keep.append(b)
        e["bullets"] = keep
    return dict(result, content=content, reverted=reverted)


def foreign_bullets(base, content):
    """[(path, why)] for changed bullets that are a base bullet of ANOTHER role (a fact moved between jobs)."""
    own = [{_fold(b) for b in e["bullets"]} for e in base["experience"]]
    out = []
    for path, text in changed_fields(base, content):
        m = re.match(r"experience\[(\d+)\]\.bullets", path)
        if m and any(_fold(text) in s for k, s in enumerate(own) if k != int(m.group(1))):
            out.append((path, "bullet belongs to another role"))
    return out


def _field_lines(path, text, known=frozenset()):
    """Printed lines of one field; a line already in the base (known) is measured as laid out in the
    approved PDF, a new one against the safer, narrower box."""
    from . import measure
    safe = _norm_text(text) not in known
    if path == "summary":
        return measure.lines(text, "summary", safe=safe)
    if path.startswith("skills"):
        group, _, items = text.partition(": ")
        return measure.lines(items, "skills", group, safe=safe)
    return measure.lines(text, "bullet", safe=safe)


def _known(base):
    return frozenset(_norm_text(t) for _, t in fields(base)) if base else frozenset()


def total_lines(content, base=None):
    """Printed lines of all editable fields (summary, bullets, skills), measured with the bundled font.
    With base given, lines unchanged from the base count as they print in the approved PDF."""
    known = _known(base if base is not None else content)
    return sum(_field_lines(p, t, known) for p, t in fields(content))


def budgets(base):
    """Length limits taken from the approved one-page base: characters of visible text and printed lines."""
    bullets = [len(strip_tags(b)) for e in base["experience"] for b in e["bullets"]]
    return {"bullet": max(bullets),
            "bullets": [len(e["bullets"]) for e in base["experience"]],
            "lines": total_lines(base, base),
            "summary": int(len(strip_tags(base.get("summary", ""))) * 1.05),
            "skills": {_norm_text(s["group"]): len(f'{s["group"]}: {s["items"]}') for s in base["skills"]},
            "skills_max": max(len(f'{s["group"]}: {s["items"]}') for s in base["skills"]),
            "skills_count": len(base["skills"])}


def length_guard(base, result):
    """Pre-build guard against a second page. No role gets more bullets than the base (new bullets are cut
    from the end), no more skill lines than the base; every changed line over its character budget goes
    back to base wording; then, while the editable fields would print more lines than the base does,
    the changed field taking the most lines goes back too (the base page is full, so equal is the limit)."""
    b = budgets(base)
    content = copy.deepcopy(result["content"])
    reverted = list(result.get("reverted", []))
    base_norm = [{_norm_text(x) for x in e["bullets"]} for e in base["experience"]]
    for i, e in enumerate(content["experience"]):
        while len(e["bullets"]) > b["bullets"][i]:
            new = [j for j, x in enumerate(e["bullets"]) if _norm_text(x) not in base_norm[i]]
            j = new[-1] if new else len(e["bullets"]) - 1
            reverted.append({"field": f"experience[{i}].bullets[{j}]", "text": strip_tags(e["bullets"][j]), "base": "",
                             "why": f"length: role has more than {b['bullets'][i]} bullets"})
            e["bullets"].pop(j)
    while len(content["skills"]) > b["skills_count"]:
        s = content["skills"].pop()
        reverted.append({"field": f"skills[{len(content['skills'])}]", "text": f'{s["group"]}: {s["items"]}',
                         "base": "", "why": f"length: more than {b['skills_count']} skill lines"})
    result = dict(result, content=content, reverted=reverted)
    flagged = []
    for path, text in changed_fields(base, content):
        n = len(strip_tags(text))
        if path == "summary" and n > b["summary"]:
            flagged.append((path, f"length: summary {n} > {b['summary']} chars"))
        elif path.startswith("experience") and n > b["bullet"]:
            flagged.append((path, f"length: bullet {n} > {b['bullet']} chars"))
        elif path.startswith("skills"):
            limit = b["skills"].get(_norm_text(text.split(":", 1)[0]), b["skills_max"])
            if n > limit:
                flagged.append((path, f"length: skill line {n} > {limit} chars"))
    result = revert_many(base, result, flagged)
    known = _known(base)
    for _ in range(50):
        total = total_lines(result["content"], base)
        if total <= b["lines"]:
            break
        changed = [(p, t, _field_lines(p, t, known)) for p, t in changed_fields(base, result["content"])]
        if not changed:
            break
        path = max(changed, key=lambda c: (c[2], _order(c[0])))[0]
        result = revert_many(base, result, [(path, f"length: CV would print {total} lines, the page holds {b['lines']}")])
    return result


# ---------- skill items (library mode: replaces the verifier for skills) ----------

def split_items(items):
    """'Jira (Kanban, Scrum), Figma' -> ['Jira (Kanban, Scrum)', 'Figma']: commas inside brackets stay."""
    out, cur, depth = [], "", 0
    for ch in items or "":
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth <= 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [x for x in out if x]


def load_skills_allowed(path=SKILLS_ALLOWED):
    """{folded group: {folded item: item as spelled in the file}} from cv/skills_allowed.json (raw JSON errors
    propagate; library.load_skills turns them into LibraryError)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {_fold(g): {_fold(x): clean(x) for x in items} for g, items in raw.items() if g != "note"}


def allowed_items(group, items, allowed):
    """(kept, dropped [(item, why)]) for one skill line: exact whitelist per group (case-insensitive, whole
    item, the file's spelling), duplicates out, at most MAX_SKILL_ITEMS. Replaces 'any fragment of the fact
    bank' (QA 09.10: 'Development was led by the CTO' passed as a skill)."""
    ok = allowed.get(_fold(group), {})
    kept, seen, dropped = [], set(), []
    for item in split_items(clean(items[:MAX_SKILLS_RAW])):
        key = _fold(item)
        if key not in ok:
            dropped.append((item, "skill not in cv/skills_allowed.json for this group"))
        elif key in seen:
            dropped.append((item, "duplicate skill item"))
        elif len(kept) >= MAX_SKILL_ITEMS:
            dropped.append((item, f"more than {MAX_SKILL_ITEMS} items in the line"))
        else:
            seen.add(key)
            kept.append(ok[key])
    return kept, dropped


def skill_source(base, facts):
    """Text a skill item must come from: the base CV and the fact bank without its 'NOT used' and removed lines."""
    keep = [l for l in facts.splitlines() if not re.match(r"\s*-\s*NOT used", l) and "REMOVED" not in l]
    return cv_text(base) + "\n" + "\n".join(keep)


def unknown_items(items, source):
    """Skill items that neither the base CV nor the fact bank mention: the whole item as a phrase, or else
    every part of it ('Figma (wireframes)' -> 'Figma' and 'wireframes') found separately."""
    out = []
    for item in split_items(items):
        if mentions(item, source):
            continue
        parts = [p.strip(" ,") for p in re.split(r"[()]", item) if p.strip(" ,")]
        if not parts or not all(mentions(p, source) for p in parts):
            out.append(item)
    return out
