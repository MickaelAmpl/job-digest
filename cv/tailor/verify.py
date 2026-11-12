"""NOT USED since 09.10.2026 (library mode): summary and bullets come from cv/library.json, skill items are
checked in code (checks.unknown_items). Kept only until the product decides to delete it.

Step 3: a separate Claude opus call judges every line the tailoring changed against the fact bank.
Which lines changed is computed by code (not taken from the model's own change log); a line the
verifier rejects, skips or answers twice with any 'no' goes back to the base wording ("откатил")."""

import json
from pathlib import Path

from . import checks, llm

MODEL = "opus"
PROMPT = Path(__file__).parent / "prompts" / "verify.md"

revert = checks.revert_many


def run(base, facts, result):
    """-> result plus 'reverted'; raises LLMError if the verifier answer is unusable."""
    changed = checks.changed_fields(base, result["content"])
    if not changed:
        return dict(result, reverted=list(result.get("reverted", [])))
    items = [{"id": f"c{n}", "field": path, "text": checks.strip_tags(text)}
             for n, (path, text) in enumerate(changed, 1)]
    prompt = (PROMPT.read_text(encoding="utf-8") +
              "\n\n## Fact bank (the only source of truth)\n" + facts +
              "\n\n## Base CV (JSON)\n" + json.dumps(base, ensure_ascii=False, indent=1) +
              "\n\n## Changed lines to check\n" + json.dumps(items, ensure_ascii=False, indent=1))
    answer = llm.ask_json(prompt, MODEL, timeout=180)
    verdicts = answer.get("items") if isinstance(answer, dict) else answer
    if not isinstance(verdicts, list):
        raise llm.LLMError("verifier answer has no items")
    ok, why = {}, {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id"))
        vid = "c" + vid if vid.isdigit() else vid
        ok[vid] = ok.get(vid, True) and v.get("supported") is True  # any 'no' for a line wins
        if v.get("supported") is not True:
            why[vid] = str(v.get("why") or "unsupported")
    flagged = []
    for item in items:
        if item["id"] not in ok:
            flagged.append((item["field"], "verifier gave no verdict"))
        elif not ok[item["id"]]:
            flagged.append((item["field"], "verifier: " + why.get(item["id"], "unsupported")))
    return revert(base, result, flagged)
