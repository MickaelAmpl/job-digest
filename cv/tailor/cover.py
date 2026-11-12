"""Term coverage in library mode (Misha 09.10.2026, Bitmovin case: 'AI tools' sat in pm7, the model did not
pick it, and the bot said 0% -> 0%). Code knows where every ad term lives in the library and tells the model;
after the selection it puts in a library unit for a claimable term that is still missing - only within the
library rules, the page budget and the 3-mentions cap. A claimable term that no unit can ever cover is
reported to Misha as missing wording (claimable_no_wording), never written by anyone.

Matching is checks.mentions - the same tolerant matching as coverage (case, simple plurals, hyphen/space)."""

import copy

from . import checks, library

FILL_ORDER = {"skill": 0, "swap": 1, "bullet": 2}   # least disruptive first


def _fixed_text(base):
    """Visible text the tailoring never changes: headline, role titles, education, certifications, group names."""
    c = copy.deepcopy(base)
    c["summary"] = ""
    for e in c["experience"]:
        e["bullets"] = []
    for s in c["skills"]:
        s["items"] = ""
    return checks.cv_text(c)


def variants(text, groups):
    """(text, swap or None): the text as it is, then after each single listed synonym swap."""
    out = [(text, None)]
    for g in groups:
        for src in g:
            for dst in g:
                if dst != src:
                    new, why = library._swap(text, {"from": src, "to": dst}, groups)
                    if not why:
                        out.append((new, {"from": src, "to": dst}))
    return out


def term_index(base, lib, terms, allowed=None):
    """{term: [unit]} - every library unit that contains the term: fixed text, summary variants, bullets (with the
    synonym swap that brings the term in, if one is needed), whitelisted skill items of the base groups."""
    allowed = checks.load_skills_allowed() if allowed is None else allowed
    groups = lib.get("synonyms", [])
    fixed = _fixed_text(base)
    names = {checks._fold(s["group"]): s["group"] for s in base["skills"]}
    index = {}
    for t in checks._unique(terms):
        units = [{"kind": "fixed"}] if checks.mentions(t, fixed) else []
        units += [{"kind": "summary", "id": k} for k, v in lib["summaries"].items() if checks.mentions(t, v)]
        for i, r in enumerate(lib["roles"]):
            for bid, text in r["bullets"].items():
                swap = next((s for v, s in variants(text, groups) if checks.mentions(t, v)), False)
                if swap is not False:
                    units.append({"kind": "bullet", "role": i, "id": bid, "swap": swap})
        for g, items in allowed.items():
            if g in names:
                units += [{"kind": "skill", "group": names[g], "item": x} for x in items.values() if checks.mentions(t, x)]
        index[t] = units
    return index


def label(unit):
    k = unit["kind"]
    if k == "fixed":
        return "already on the page (fixed text)"
    if k == "summary":
        return f'summary {unit["id"]}'
    if k == "bullet":
        s = unit.get("swap")
        return unit["id"] + (f' (with swap "{s["from"]}" -> "{s["to"]}")' if s else "")
    return f'skill item "{unit["item"]}" ({unit["group"]})'


def prompt_text(index):
    lines = [f'- "{t}": ' + (", ".join(label(u) for u in units) if units else "nothing in the library or the skills list")
             for t, units in index.items()]
    return ("\n\n## Where each ad term appears in the library (code-computed; case-insensitive, plurals and "
            "hyphens tolerated)\n" + "\n".join(lines))


def no_wording(index, claimable):
    """Claimable terms that no library unit, skill item or fixed text contains: Misha decides on new wording."""
    return [t for t in checks._unique(claimable) if not index.get(t)]


# ---------- deterministic fill after the selection ----------

def _covered(terms, text):
    return {t for t in terms if checks.mentions(t, text)}


def _ok(base, old, new, term, claim, terms, budget):
    """The fill keeps every rule the selection had: the term now shows, no claimable term is lost, the page
    does not grow past its budget, no term goes over 3 mentions."""
    a, b = checks.cv_text(old["content"]), checks.cv_text(new["content"])
    if not checks.mentions(term, b) or not _covered(claim, a) <= _covered(claim, b):
        return False
    if checks.total_lines(new["content"], base) > max(budget, checks.total_lines(old["content"], base)):
        return False
    return set(checks.overused(terms, b)) <= set(checks.overused(terms, a))


def _with_skill(base, lib, result, unit, claim, limits):
    content = copy.deepcopy(result["content"])
    k = next((n for n, s in enumerate(content["skills"]) if s["group"] == unit["group"]), None)
    if k is None:
        return None
    line = content["skills"][k]
    items = checks.split_items(line["items"])
    if checks._fold(unit["item"]) in {checks._fold(x) for x in items}:
        return None
    text = checks.cv_text(content)
    keep = {x for x in items if any(checks.mentions(t, x) and checks.mentions(t, text) for t in claim)}
    limit = limits.get(checks._norm_text(line["group"]))
    size = lambda xs: len(f'{line["group"]}: {", ".join(xs)}')
    new = items + [unit["item"]]
    for x in reversed(items):  # make room: drop items from the end that cover no claimable term
        if len(new) <= checks.MAX_SKILL_ITEMS and (limit is None or size(new) <= limit):
            break
        if x not in keep:
            new.remove(x)
    if len(new) > checks.MAX_SKILL_ITEMS or (limit is not None and size(new) > limit):
        return None
    line["items"] = ", ".join(new)
    return library.refresh(base, lib, dict(result, content=content))


def _with_bullet(base, lib, result, unit):
    """Yields candidate results: a swap on the bullet when it is selected, else the bullet in place of each
    selected one from the end (lowest priority first) that is not must_keep or pin_last."""
    i, bid, swap = unit["role"], unit["id"], unit.get("swap")
    role, sel = lib["roles"][i], result["selection"]
    picks = [b["id"] for b in sel["roles"][i]]
    groups = lib.get("synonyms", [])
    if bid in picks:
        if not swap or sum(1 for s in sel["swaps"] if s["bullet"] == bid) >= library.MAX_SWAPS:
            return
        new_sel = copy.deepcopy(sel)
        slot = next(b for b in new_sel["roles"][i] if b["id"] == bid)
        text, why = library._swap(slot["text"], swap, groups)
        if why:
            return
        slot["text"] = text
        new_sel["swaps"].append(dict(bullet=bid, **swap))
        yield "swap", library.refresh(base, lib, dict(result, selection=new_sel))
        return
    text = role["bullets"][bid]
    if swap:
        text, why = library._swap(text, swap, groups)
        if why:
            return
    count = len(base["experience"][i]["bullets"])
    for victim in reversed(picks):
        if victim in role.get("must_keep", []) or victim == role.get("pin_last"):
            continue
        pick = [bid if x == victim else x for x in picks]
        if library._role_problem(role, count, pick):
            continue
        new_sel = copy.deepcopy(sel)
        slots = {b["id"]: b for b in new_sel["roles"][i]}
        slots[bid] = {"id": bid, "text": text}
        new_sel["roles"][i] = [slots[x] for x in library._pinned(role, pick)]
        new_sel["swaps"] = [s for s in new_sel["swaps"] if s["bullet"] != victim]
        if swap:
            new_sel["swaps"].append(dict(bullet=bid, **swap))
        yield "bullet", library.refresh(base, lib, dict(result, selection=new_sel))


def _candidates(base, lib, result, unit, claim, limits):
    if unit["kind"] == "skill":
        new = _with_skill(base, lib, result, unit, claim, limits)
        if new is not None:
            yield "skill", new
    elif unit["kind"] == "bullet":
        yield from _with_bullet(base, lib, result, unit)


def _order(unit, result):
    if unit["kind"] == "bullet":
        selected = unit["id"] in [b["id"] for b in result["selection"]["roles"][unit["role"]]]
        return FILL_ORDER["swap" if selected else "bullet"]
    return FILL_ORDER.get(unit["kind"], 9)


def fill(base, lib, result, terms, allowed, index=None):
    """For each claimable term still missing from the page: the first library unit that brings it in within
    the rules (skill item, swap in a selected bullet, then a bullet replacing the lowest-priority selected
    one). Each fill is recorded in result['filled'] so library.changes() can say 'добавлено под требование'."""
    claim = checks._unique([t for t in terms if (result.get("claimable") or {}).get(t)])
    if not claim:
        return result
    index = index if index is not None else term_index(base, lib, claim, allowed)
    b = checks.budgets(base)
    for t in claim:
        if checks.mentions(t, checks.cv_text(result["content"])):
            continue
        done = False
        for unit in sorted(index.get(t, []), key=lambda u: _order(u, result)):
            for kind, new in _candidates(base, lib, result, unit, claim, b["skills"]):
                if _ok(base, result, new, t, claim, terms, b["lines"]):
                    rec = {"kind": "skill", "item": unit["item"]} if kind == "skill" else {"kind": "bullet", "id": unit["id"]}
                    filled = list(result.get("filled", [])) + [dict(rec, term=t)]
                    result = library.refresh(base, lib, dict(new, filled=filled))
                    done = True
                    break
            if done:
                break
    return result
