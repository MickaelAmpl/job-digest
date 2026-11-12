"""Library mode (Misha 09.10.2026): free-form rewriting read as chaotic, so the summary and bullets of a
tailored CV come only from the approved wording library (cv/library.json). The model returns choices
(summary variant id, bullet ids per role, synonym swaps); code checks every choice and assembles the
text. Anything invalid puts that role or the summary back to the base selection - never free text.

Selection shape: {"summary": {"id", "text"}, "roles": [[{"id", "text"}, ...], ...], "swaps": [...applied]}."""

import copy
import json
import re
from pathlib import Path

from . import checks

LIBRARY = checks.ROOT / "library.json"
FACTS = checks.ROOT / "facts.md"
MAX_SWAPS = 2


class LibraryError(Exception):
    """A broken setup file: the run stops loudly and the bot names `file` (library.json, skills_allowed.json,
    facts.md, never_claim.json, allowed_numbers.json) instead of sending a CV built on damaged rules."""

    def __init__(self, message, file="library.json"):
        super().__init__(message)
        self.file = file


# ---------- loading and validation ----------

def load(base, path=None, allowed=None, words=None):
    """The validated library; raises LibraryError (loudly, no fallback) when it does not match the base CV
    or a text fails the code checks - a broken library must be fixed, not worked around."""
    path = Path(path or LIBRARY)
    try:
        lib = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LibraryError(f"{path.name}: {e}", path.name)
    problems = validate(base, lib, allowed, words)
    if problems:
        raise LibraryError(f"{path.name}: " + "; ".join(problems), path.name)
    return lib


def load_skills(base, path=None):
    """The skills whitelist (cv/skills_allowed.json) as checks.allowed_items uses it; LibraryError when it is
    unreadable, lists a never-claim term, or misses a group or an item of the base CV's skills."""
    path = Path(path or checks.SKILLS_ALLOWED)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not all(isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
                                                for k, v in raw.items() if k != "note"):
            raise ValueError("must be {group: [item, ...]}")
        allowed = checks.load_skills_allowed(path)
    except (OSError, ValueError) as e:
        raise LibraryError(f"{path.name}: {e}", path.name)
    never, cs = checks.load_never_claim()
    problems = []
    for g, items in raw.items():
        if g == "note":
            continue
        for x in items:
            if hits := checks.blocklist_hits(x, never, cs):
                problems.append(f'{g}: "{x}" is never-claim ({", ".join(hits)})')
            if not checks.markup_ok(x, False) or checks.german_words(x) or checks.uncovered(x):
                problems.append(f'{g}: "{x}" has markup, German words or a glyph the font lacks')
    for s in base["skills"]:
        ok = allowed.get(checks._fold(s["group"]))
        if ok is None:
            problems.append(f'base group "{s["group"]}" missing')
            continue
        problems += [f'{s["group"]}: base item "{x}" not listed' for x in checks.split_items(s["items"])
                     if checks._fold(x) not in ok]
    if problems:
        raise LibraryError(f"{path.name}: " + "; ".join(problems), path.name)
    return allowed


def _texts(lib):
    """(where, text, rich) of every text the library can put on the page."""
    out = [(f"summaries.{k}", v, True) for k, v in lib.get("summaries", {}).items()]
    for r in lib.get("roles", []):
        out += [(f'{r.get("role")}.{k}', v, True) for k, v in r.get("bullets", {}).items()]
    out += [(f"synonyms: {p}", p, False) for g in lib.get("synonyms", []) for p in g]
    return out


TOP_KEYS = {"note", "summaries", "roles", "synonyms"}
ROLE_KEYS = {"role", "bullets", "must_keep", "exclusive", "pin_last"}  # a typo ('must_kep') must not drop a rule


def _structure(lib, n_roles):
    """Shape problems; when there are any, nothing else is checked (QA 09.10: bullets as a list crashed)."""
    if not isinstance(lib, dict):
        return ["not a JSON object"]
    problems = [f"unknown key {k!r}" for k in lib if k not in TOP_KEYS]
    if "synonyms" not in lib:
        problems.append("synonyms key missing")
    sums, roles, syn = lib.get("summaries"), lib.get("roles"), lib.get("synonyms", [])
    if not isinstance(sums, dict) or not sums or not all(isinstance(v, str) and v for v in sums.values()):
        problems.append("summaries must be a non-empty {id: text} object")
    if not isinstance(syn, list) or not all(isinstance(g, list) and len(g) >= 2 and all(isinstance(p, str) and p for p in g)
                                            for g in syn):
        problems.append("synonyms must be a list of groups of 2+ phrases")
    else:
        where = {}
        for n, g in enumerate(syn):
            for phrase in g:
                if phrase in where:
                    problems.append(f'synonym "{phrase}" is in two groups' if where[phrase] != n
                                    else f'synonym "{phrase}" twice in one group')
                where[phrase] = n
    if not isinstance(roles, list) or len(roles) != n_roles:
        return problems + [f"roles must list the {n_roles} base roles in order"]
    ids = lambda x: isinstance(x, list) and all(isinstance(k, str) for k in x)
    for i, r in enumerate(roles):
        if not isinstance(r, dict):
            problems.append(f"role {i}: not an object")
            continue
        problems += [f"role {i}: unknown key {k!r}" for k in r if k not in ROLE_KEYS]
        bullets = r.get("bullets")
        if not isinstance(bullets, dict) or not bullets or not all(isinstance(v, str) and v for v in bullets.values()):
            problems.append(f"role {i}: bullets must be a non-empty {{id: text}} object")
        if "must_keep" not in r:  # QA 09.10: a removed must_keep silently weakened the rules; [] = none on purpose
            problems.append(f"role {i}: must_keep key missing (use [] for none)")
        elif not ids(r["must_keep"]):
            problems.append(f"role {i}: must_keep must be a list of ids")
        if not (isinstance(r.get("exclusive", []), list) and all(ids(p) for p in r.get("exclusive", []))):
            problems.append(f"role {i}: exclusive must be a list of [id, id] pairs")
    return problems


def validate(base, lib, allowed=None, words=None):
    """[problem] - empty when the library is usable with this base CV."""
    problems = _structure(lib, len(base["experience"]))
    if problems:
        return problems
    try:
        return _validate(base, lib, allowed, words)
    except (TypeError, AttributeError, KeyError, ValueError) as e:  # a shape _structure did not foresee
        return [f"malformed library ({type(e).__name__}: {e})"]


def _validate(base, lib, allowed=None, words=None):
    if allowed is None:
        allowed = checks.cv_text(base) + "\n" + " ".join(checks.load_allowed_numbers())
    if words is None:
        words = checks.cv_text(base) + "\n" + FACTS.read_text(encoding="utf-8")
    sums, roles = lib["summaries"], lib["roles"]
    problems, seen = [], {"summary"}
    base_sid = [k for k, v in sums.items() if v == base.get("summary")]
    if not base_sid or not base_sid[0].upper().startswith("A"):
        problems.append("base summary is not summary A")
    for i, (r, e) in enumerate(zip(roles, base["experience"])):
        bullets = r.get("bullets") if isinstance(r, dict) else None
        if not isinstance(bullets, dict) or not bullets or not all(isinstance(v, str) and v for v in bullets.values()):
            problems.append(f"role {i}: bullets must be a non-empty {{id: text}} object")
            continue
        if r.get("role") != e["role"]:
            problems.append(f'role {i}: "{r.get("role")}" is not the base role "{e["role"]}"')
        for bid in bullets:
            if bid in seen:
                problems.append(f"duplicate id {bid}")
            seen.add(bid)
        for b in e["bullets"]:
            if b not in bullets.values():
                problems.append(f'{e["role"]}: base bullet not in the library: "{checks.strip_tags(b)[:60]}"')
        for k in r.get("must_keep", []):
            if k not in bullets:
                problems.append(f'{e["role"]}: must_keep id {k} not in the library')
        for pair in r.get("exclusive", []):
            if not (isinstance(pair, list) and len(pair) == 2 and all(k in bullets for k in pair)):
                problems.append(f'{e["role"]}: bad exclusive pair {pair}')
        pin = r.get("pin_last")
        if pin is not None and not (isinstance(pin, str) and pin in bullets):
            problems.append(f'{e["role"]}: pin_last id {pin!r} not in the library')
    if not problems:
        for i, ids in enumerate(base_ids(base, lib)):
            if why := _role_problem(lib["roles"][i], len(ids), ids):
                problems.append(f'{base["experience"][i]["role"]}: the base selection breaks a rule ({why})')
            elif _pinned(lib["roles"][i], ids) != ids:
                problems.append(f'{base["experience"][i]["role"]}: pin_last {lib["roles"][i]["pin_last"]} is not last in the base CV')
    never, cs = checks.load_never_claim()
    for where, text, rich in _texts(lib):
        why = []
        if not checks.markup_ok(text, rich):
            why.append("markup")
        if hits := checks.blocklist_hits(text, never, cs):
            why.append("never-claim " + ", ".join(hits))
        if nums := checks.new_numbers(allowed, text, words):
            why.append("new number " + ", ".join(nums))
        if de := checks.german_words(text):
            why.append("German " + ", ".join(de))
        if miss := checks.uncovered(text):
            why.append("glyph " + " ".join(miss[:5]))
        if mixed := checks.mixed_script(text):
            why.append("mixed script " + ", ".join(mixed[:3]))
        if why:
            problems.append(f"{where}: " + ", ".join(why))
    return problems


# ---------- selection ----------

def base_ids(base, lib):
    """Ids of the base CV's bullets, per role, in base order."""
    out = []
    for e, r in zip(base["experience"], lib["roles"]):
        by_text = {v: k for k, v in r["bullets"].items()}
        out.append([by_text[b] for b in e["bullets"]])
    return out


def base_summary_id(base, lib):
    return next(k for k, v in lib["summaries"].items() if v == base["summary"])


def base_selection(base, lib):
    return {"summary": {"id": base_summary_id(base, lib), "text": base["summary"]},
            "roles": [[{"id": k, "text": lib["roles"][i]["bullets"][k]} for k in ids]
                      for i, ids in enumerate(base_ids(base, lib))],
            "swaps": []}


def _role_problem(role, base_count, pick):
    """Why a list of bullet ids is not a valid choice for this role, or '' when it is."""
    if not isinstance(pick, list) or not all(isinstance(x, str) for x in pick):
        return "not a list of bullet ids"
    if not 1 <= len(pick) <= base_count:
        return f"{len(pick)} bullets, allowed 1-{base_count}"
    if len(set(pick)) != len(pick):
        return "a bullet id repeats"
    if unknown := [x for x in pick if x not in role["bullets"]]:
        return "unknown id for this role: " + ", ".join(unknown)
    if missing := [x for x in role.get("must_keep", []) if x not in pick]:
        return "must keep " + ", ".join(missing)
    for a, b in role.get("exclusive", []):
        if a in pick and b in pick:
            return f"{a} and {b} exclude each other"
    return ""


def _pinned(role, pick):
    """The role's pin_last id, when picked, moved to the end ('Exited the business in 2022' closes Founder)."""
    pin = role.get("pin_last")
    return [x for x in pick if x != pin] + [pin] if pin in pick else list(pick)


def _swap(text, swap, groups):
    """(new text, '') or (text, why): one synonym swap inside one approved text."""
    if not isinstance(swap, dict) or not isinstance(swap.get("from"), str) or not isinstance(swap.get("to"), str):
        return text, "swap needs 'from' and 'to' phrases"
    src, dst = swap["from"], swap["to"]
    if src == dst or not any(src in g and dst in g for g in groups):
        return text, f'"{src}" -> "{dst}" is not a listed synonym pair'
    if dst in text:
        return text, f'"{dst}" is already in the text'  # stops 'tickets' -> 'Jira tickets' on 'Jira tickets'
    m = re.search(rf"(?<!\w){re.escape(src)}(?!\w)", text)
    if not m:
        return text, f'"{src}" is not in the text'
    return text[:m.start()] + dst + text[m.end():], ""


def _fmt_role(ids):
    return " / ".join(map(str, ids)) if isinstance(ids, list) else str(ids)  # ids may be ints/NaN from the model


def choose(base, lib, answer):
    """Model answer -> (selection, reverted). Each role and the summary is taken from the answer only when
    every rule holds for it, swaps included; otherwise it stays at the base selection and is logged."""
    sel, reverted = base_selection(base, lib), []
    base_sel = copy.deepcopy(sel)
    texts = [" / ".join(checks.strip_tags(b["text"]) for b in r) for r in base_sel["roles"]]

    def log(unit, chose, why):
        base_text = checks.strip_tags(base["summary"]) if unit == "summary" else texts[int(unit[11:-1])]
        reverted.append({"field": unit, "text": chose[:200], "base": base_text, "why": "library: " + why[:300]})

    sid = answer.get("summary")
    if isinstance(sid, str) and sid in lib["summaries"]:
        sel["summary"] = {"id": sid, "text": lib["summaries"][sid]}
    else:
        log("summary", str(sid), f"unknown summary id {str(sid)[:60]!r}")
    roles = answer.get("roles")
    picks = roles if isinstance(roles, list) else []
    if not isinstance(roles, list):
        reverted.append({"field": "experience", "text": str(roles)[:200], "base": "", "why": "library: no role selection"})
    ok_roles = set()
    for i, role in enumerate(lib["roles"]):
        pick = picks[i] if i < len(picks) else None
        if pick is None and isinstance(roles, list):
            log(f"experience[{i}]", "", "no selection for this role")
            continue
        if pick is None:
            continue
        if why := _role_problem(role, len(base["experience"][i]["bullets"]), pick):
            log(f"experience[{i}]", _fmt_role(pick), why)
            continue
        sel["roles"][i] = [{"id": k, "text": role["bullets"][k]} for k in _pinned(role, pick)]
        ok_roles.add(i)

    owner = {k: i for i, r in enumerate(lib["roles"]) for k in r["bullets"]}
    swaps = answer.get("swaps") or []
    if not isinstance(swaps, list):
        reverted.append({"field": "swaps", "text": str(swaps)[:200], "base": "", "why": "library: swaps not a list"})
        swaps = []
    by_target, broken = {}, {}
    for s in swaps:
        target = s.get("bullet") if isinstance(s, dict) else None
        if not isinstance(target, str):  # a list/dict 'bullet' is unhashable: log it, never crash the run
            target = None if target is None else repr(target)[:60]
        if target == "summary":
            unit = "summary"
        elif target in owner:
            unit = f"experience[{owner[target]}]"
        else:
            reverted.append({"field": "swaps", "text": json.dumps(s, ensure_ascii=False)[:200], "base": "",
                             "why": f"library: swap target {target!r} is not a bullet id"})
            continue
        by_target.setdefault((unit, target), []).append(s)
    groups = lib.get("synonyms", [])
    for (unit, target), group in by_target.items():
        if unit in broken:
            continue
        if unit == "summary":
            if sel["summary"]["id"] != sid:
                continue  # the summary already fell back; its swaps go with it
            slot = sel["summary"]
        else:
            i = int(unit[11:-1])
            if i not in ok_roles:
                continue
            slot = next((b for b in sel["roles"][i] if b["id"] == target), None)
            if slot is None:  # QA 09.10: drop just these swaps, the role selection stays
                reverted += [{"field": "swaps", "text": json.dumps(x, ensure_ascii=False)[:200], "base": "",
                              "why": f"library: swap on {target}, which is not selected - swap dropped"} for x in group]
                continue
        if len(group) > MAX_SWAPS:
            broken[unit] = f"{len(group)} swaps on {target}, max {MAX_SWAPS}"
            continue
        text = slot["text"]
        for s in group:
            text, why = _swap(text, s, groups)
            if why:
                broken[unit] = f"swap on {target}: {why}"
                break
        else:
            slot["text"] = text
            sel["swaps"] += [dict(bullet=target, **{k: s[k] for k in ("from", "to")}) for s in group]
    for unit, why in broken.items():
        if unit == "summary":
            log("summary", sel["summary"]["text"], why)
            sel["summary"] = base_sel["summary"]
        else:
            i = int(unit[11:-1])
            log(unit, _fmt_role([b["id"] for b in sel["roles"][i]]), why)
            sel["roles"][i] = base_sel["roles"][i]
        drop = (lambda b: b == "summary") if unit == "summary" else (lambda b: owner.get(b) == int(unit[11:-1]))
        sel["swaps"] = [s for s in sel["swaps"] if not drop(s["bullet"])]
    return sel, reverted


# ---------- content, changes, reverts ----------

def assemble(base, sel, skills=None):
    """Base CV with the selected summary and bullets (and skills); every other field is the base's."""
    content = copy.deepcopy(base)
    content["summary"] = sel["summary"]["text"]
    for e, picks in zip(content["experience"], sel["roles"]):
        e["bullets"] = [b["text"] for b in picks]
    if skills is not None:
        content["skills"] = copy.deepcopy(skills)
    return content


def changes(base, lib, sel, content):
    """Change list for the bot, computed by code from the selection (not taken from the model)."""
    out = []

    def swapped(target):
        pairs = [f'{s["from"]} -> {s["to"]}' for s in sel.get("swaps", []) if s["bullet"] == target]
        return f' (swap: {"; ".join(pairs)})' if pairs else ""
    if sel["summary"]["text"] != base["summary"]:
        out.append({"field": "summary", "before": base["summary"], "after": sel["summary"]["text"],
                    "reason": f'summary variant {sel["summary"]["id"]}' + swapped("summary"), "fact": "approved library"})
    for i, (picks, ids) in enumerate(zip(sel["roles"], base_ids(base, lib))):
        bullets = lib["roles"][i]["bullets"]
        for j, b in enumerate(picks):
            if b["text"] in base["experience"][i]["bullets"]:
                continue
            is_base = b["id"] in ids
            out.append({"field": f"experience[{i}].bullets[{j}]", "before": bullets[b["id"]] if is_base else "",
                        "after": b["text"], "reason": ("synonym swap in " if is_base else "library bullet ") + b["id"] + swapped(b["id"]),
                        "fact": "approved library"})
        chosen = [b["id"] for b in picks]
        for k in ids:
            if k not in chosen:
                out.append({"field": f"experience[{i}]", "before": bullets[k], "after": "",
                            "reason": f"dropped {k} for this ad", "fact": "approved library"})
        kept = [k for k in chosen if k in ids]
        if kept != [k for k in ids if k in chosen]:
            out.append({"field": f"experience[{i}].order", "before": ", ".join(ids), "after": ", ".join(chosen),
                        "reason": "bullets reordered", "fact": "approved library"})
    order = [s["group"] for s in content.get("skills", [])]
    if order != [s["group"] for s in base["skills"]]:
        out.append({"field": "skills.order", "before": ", ".join(s["group"] for s in base["skills"]),
                    "after": ", ".join(order), "reason": "skill groups reordered", "fact": "base CV"})
    base_lines = {s["group"]: f'{s["group"]}: {s["items"]}' for s in base["skills"]}
    for k, s in enumerate(content.get("skills", [])):
        line = f'{s["group"]}: {s["items"]}'
        if line not in base_lines.values():
            out.append({"field": f"skills[{k}]", "before": base_lines.get(s["group"], ""), "after": line,
                        "reason": "skills picked and ordered for the ad", "fact": "fact bank"})
    return out


def note_filled(chs, filled):
    """Changes that code made for a claimable term (cover.fill) say so; a filled unit reverted later has no
    change left to mark, so its note goes with it."""
    for f in filled or []:
        note = f'добавлено под требование «{f["term"]}»'
        for c in chs:
            if f["kind"] == "bullet":
                hit = re.match(rf'(?:library bullet|synonym swap in) {re.escape(f["id"])}(?!\w)', c["reason"])
            else:
                hit = c["field"].startswith("skills[") and checks._fold(f["item"]) in \
                    {checks._fold(x) for x in checks.split_items(c["after"].partition(": ")[2])}
            if hit:
                c["note"] = c["note"] + "; " + note if c.get("note") else note
                break
    return chs


def refresh(base, lib, result):
    """Content and changes rebuilt from the selection, skills kept as they are."""
    content = assemble(base, result["selection"], result["content"]["skills"])
    chs = note_filled(changes(base, lib, result["selection"], content), result.get("filled"))
    return dict(result, content=content, changes=chs)


def unit_of(path):
    """'experience[2].bullets[1]' -> 'experience[2]': a bullet is never edited alone, its role is the unit."""
    m = re.match(r"(summary|experience\[\d+\]|skills\[\d+\])", path)
    return m.group(1) if m else path


def revert(base, lib, result, unit, why):
    """One unit back to base: the summary to summary A, a role to its base bullets, a skill line to the base line."""
    if unit.startswith("skills"):
        return refresh(base, lib, checks.revert_many(base, result, [(unit, why)]))
    sel = copy.deepcopy(result["selection"])
    base_sel = base_selection(base, lib)
    if unit == "summary":
        shown, sel["summary"] = result["content"]["summary"], base_sel["summary"]
        swaps_of = lambda b: b == "summary"
    else:
        i = int(unit[11:-1])
        shown, sel["roles"][i] = " / ".join(result["content"]["experience"][i]["bullets"]), base_sel["roles"][i]
        ids = lib["roles"][i]["bullets"]
        swaps_of = lambda b: b in ids
    sel["swaps"] = [s for s in sel["swaps"] if not swaps_of(s["bullet"])]
    restored = assemble(base, sel)
    base_text = restored["summary"] if unit == "summary" else " / ".join(restored["experience"][int(unit[11:-1])]["bullets"])
    entry = {"field": unit, "text": checks.strip_tags(shown), "base": checks.strip_tags(base_text), "why": why}
    return refresh(base, lib, dict(result, selection=sel, reverted=list(result.get("reverted", [])) + [entry]))


def revert_paths(base, lib, result, flagged):
    """flagged [(field path, why)] -> each unit reverted once (skill lines via the usual field revert)."""
    done = set()
    for path, why in flagged:
        unit = unit_of(path)
        if unit in done:
            continue
        done.add(unit)
        result = revert(base, lib, result, unit, why)
    return result


def changed_units(base, lib, result):
    """Units that differ from the base CV, in page order (summary, roles, skill lines)."""
    content, out = result["content"], []
    if content["summary"] != base["summary"]:
        out.append("summary")
    out += [f"experience[{i}]" for i, e in enumerate(content["experience"])
            if e["bullets"] != base["experience"][i]["bullets"]]
    out += [p for p, _ in checks.changed_fields(base, content) if p.startswith("skills")]
    return out


def trim_skills(base, lib, result):
    """A changed skill line over its character budget first loses items from its end (the model lists the
    most relevant first, so the last are the least relevant) until it fits; only a line that is still too
    long with one item left goes back to the base line. Pilot 09.10: a whole RBI Tools line was lost for
    4 extra characters."""
    b = checks.budgets(base)
    content = copy.deepcopy(result["content"])
    reverted, flagged, trimmed = list(result.get("reverted", [])), [], False
    for path, text in checks.changed_fields(base, content):
        if not path.startswith("skills"):
            continue
        s = content["skills"][int(path[7:-1])]
        limit = b["skills"].get(checks._norm_text(s["group"]), b["skills_max"])
        size = lambda items: len(checks.strip_tags(f'{s["group"]}: {", ".join(items)}'))
        items = checks.split_items(s["items"])
        n = size(items)
        if n <= limit:
            continue
        why = f"length: skill line {n} > {limit} chars"
        dropped = []
        while len(items) > 1 and size(items) > limit:
            dropped.insert(0, items.pop())
        if size(items) > limit:
            flagged.append((path, why))
            continue
        s["items"], trimmed = ", ".join(items), True
        reverted += [{"field": path, "text": x, "base": "", "why": why + ", dropped from the end"} for x in dropped]
    if trimmed:
        result = refresh(base, lib, dict(result, content=content, reverted=reverted))
    return revert_paths(base, lib, result, flagged)


def length_guard(base, lib, result):
    """Pre-build guard against a second page (measure.py widths). Skill lines over their character budget
    are trimmed (trim_skills); then, while the page would print more lines than the base does, the changed
    unit whose revert saves the most lines goes back to base (the last one on a tie)."""
    b = checks.budgets(base)
    result = trim_skills(base, lib, result)
    for _ in range(50):
        total = checks.total_lines(result["content"], base)
        if total <= b["lines"]:
            break
        units = changed_units(base, lib, result)
        if not units:
            break
        why = f"length: CV would print {total} lines, the page holds {b['lines']}"
        tries = [(checks.total_lines(revert(base, lib, result, u, why)["content"], base), -n, u)
                 for n, u in enumerate(units)]
        result = revert(base, lib, result, min(tries)[2], why)
    return result
