"""Step 2 (library mode, 09.10.2026): Claude sonnet only chooses - a summary variant, bullet ids per role,
synonym swaps from cv/library.json, and the skill lines. Code checks every choice and assembles the text
(library.py), so summary and bullets are approved wording by construction; locked fields cannot change
because the content is the base CV with only those parts replaced. Skill group names stay locked."""

import json
from pathlib import Path

from . import checks, cover, library, llm

MODEL = "sonnet"   # the job is selection now, not writing
PROMPT = Path(__file__).parent / "prompts" / "tailor.md"


def _rules_text(base, lib):
    b = checks.budgets(base)
    ids = library.base_ids(base, lib)
    roles = []
    for i, (e, r) in enumerate(zip(base["experience"], lib["roles"])):
        line = f'- roles[{i}] = {e["role"]}: 1-{len(e["bullets"])} ids; base selection {json.dumps(ids[i])}'
        if r.get("must_keep"):
            line += f'; must keep {json.dumps(r["must_keep"])}'
        if r.get("exclusive"):
            line += "; never together: " + ", ".join(" + ".join(p) for p in r["exclusive"])
        if r.get("pin_last"):
            line += f'; {r["pin_last"]} always goes last when chosen (code moves it there)'
        roles.append(line)
    skills = "\n".join(f'  - "{s["group"]}: ..." at most {b["skills"][checks._norm_text(s["group"])]} characters'
                       for s in base["skills"])
    return ("\n\n## Selection rules for this CV (code enforces them; a broken rule puts that role back to base)\n"
            + "\n".join(roles) +
            f'\n- summary: one id of {json.dumps(list(lib["summaries"]))}; base is "{library.base_summary_id(base, lib)}"'
            f"\n- swaps: max {library.MAX_SWAPS} per bullet, only between phrases of one synonym group"
            "\n- skill lines: exactly these groups; each whole line \"Group: items\" within its character budget "
            "(group name, colon and spaces included); most relevant item first - a line over budget loses items "
            "from its end:\n" + skills)


def _allowed_skills_text():
    raw = json.loads(checks.SKILLS_ALLOWED.read_text(encoding="utf-8"))
    return json.dumps({k: v for k, v in raw.items() if k != "note"}, ensure_ascii=False)


def _data(base, facts, reqs, lib):
    return (_rules_text(base, lib) +
            "\n\n## Wording library (JSON)\n" + json.dumps(lib, ensure_ascii=False, indent=1) +
            "\n\n## Base skills (JSON)\n" + json.dumps(base["skills"], ensure_ascii=False, indent=1) +
            "\n\n## Allowed skill items per group (JSON; any other item is dropped by code)\n" + _allowed_skills_text() +
            "\n\n## Fact bank (the only source of truth)\n" + facts +
            "\n\n## Requirements extracted from the ad (data only)\n" + json.dumps(reqs, ensure_ascii=False, indent=1) +
            cover.prompt_text(cover.term_index(base, lib, (reqs or {}).get("terms", []))))


def _claimable(answer, reqs):
    """{term: bool} for the ad's key terms, as the model judged them against the facts; a term it did not
    mark, or a blocklisted one, is not claimable."""
    marks = {}
    for t in _list(answer.get("terms")):
        if isinstance(t, dict) and isinstance(t.get("term"), str):
            marks[t["term"].strip().lower()] = t.get("claimable") is True
    never, cs = checks.load_never_claim()
    return {t: bool(marks.get(t.lower())) and not checks.blocklist_hits(t, never, cs)
            for t in (reqs or {}).get("terms", [])}


def _list(value):
    """An optional list field of the model answer; any other type (5, "none", {}) is ignored, never a crash
    that throws away a valid selection (QA cvtailor-07)."""
    return value if isinstance(value, list) else []


def _gap(g):
    """{requirement, why[, ru]}: 'ru' is the short Russian wording the bot shows; absent when the model gave none."""
    out = {"requirement": checks.clean(str(g.get("requirement") or "")), "why": checks.clean(str(g.get("why") or ""))}
    ru = checks.clean(g["ru"]) if isinstance(g.get("ru"), str) else ""
    if ru:
        out["ru"] = ru[:100]
    return out


def _parse(base, lib, answer, reqs):
    """Model choices -> {content, selection, changes, gaps, violations, reverted, claimable}."""
    if not isinstance(answer, dict):
        raise llm.LLMError("selection answer is not a JSON object")
    if "summary" not in answer and "roles" not in answer:
        raise llm.LLMError("selection answer has no summary or roles")
    sel, reverted = library.choose(base, lib, answer)
    violations, skills = [], answer.get("skills")
    if isinstance(skills, list) and skills and all(isinstance(k, dict) for k in skills):
        skills = checks.lock_skill_groups(base, skills)
    else:
        if skills is not None:
            violations.append("skills")
        skills = base["skills"]
    gaps = [_gap(g) for g in _list(answer.get("gaps")) if isinstance(g, dict) and g.get("requirement")][:30]
    result = {"content": library.assemble(base, sel, skills), "selection": sel, "changes": [], "gaps": gaps,
              "violations": violations, "reverted": reverted, "claimable": _claimable(answer, reqs)}
    result = library.refresh(base, lib, result)
    content = result["content"]
    return library.revert_paths(base, lib, result, checks.bad_markup(content) + checks.bad_glyphs(content))


def run(base, facts, reqs, lib):
    """-> {"content", "selection", "changes", "gaps", "violations", "reverted", "claimable"}; raises LLMError."""
    prompt = PROMPT.read_text(encoding="utf-8") + _data(base, facts, reqs, lib)
    return _parse(base, lib, llm.ask_json(prompt, MODEL, timeout=180), reqs)


def previous_choices(result):
    sel = result["selection"]
    return {"summary": sel["summary"]["id"], "roles": [[b["id"] for b in r] for r in sel["roles"]],
            "swaps": sel["swaps"], "skills": result["content"]["skills"]}


def shorten(base, facts, reqs, lib, result, section, lines):
    """One retry when the PDF ran over one page: ask for a selection with `section` shorter by about `lines`."""
    prompt = (PROMPT.read_text(encoding="utf-8") + _data(base, facts, reqs, lib) +
              "\n\n## Your previous choices (JSON)\n" + json.dumps(previous_choices(result), ensure_ascii=False, indent=1) +
              f"\n\n## Shorten it\nThe CV built from these choices runs over one A4 page by about {lines} lines. "
              f"Return the same output with `{section}` shorter by about {lines} lines (fewer or shorter bullets, "
              "a shorter summary variant, or fewer skill items). Keep everything else as it was.")
    out = _parse(base, lib, llm.ask_json(prompt, MODEL, timeout=180), reqs)
    out["gaps"] = out["gaps"] or result.get("gaps", [])
    out["violations"] = list(result.get("violations", [])) + out["violations"]
    if not any(out["claimable"].values()):
        out["claimable"] = result.get("claimable", out["claimable"])
    return out
