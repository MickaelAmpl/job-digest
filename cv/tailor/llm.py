"""One call to the claude CLI on Misha's subscription (same way as score.py: `claude -p`, prompt on
stdin) that must come back as JSON. Every failure - CLI missing, login expired, limit hit, timeout,
prose instead of JSON - becomes LLMError, so the orchestrator can fall back to the base CV."""

import json
import os
import re
import subprocess
import tempfile
import time


class LLMError(Exception):
    pass


# These calls only read the prompt and answer JSON: no tool may run. The prompt embeds untrusted ad text, so a
# hostile ad must not get Read/Bash/WebFetch, and the CLI starts in an empty temp folder, not the app folder
# that holds .env (QA cvtailor-15). --disallowedTools is accepted by every claude CLI version we run.
NO_TOOLS = ["--disallowedTools", "Bash,Read,Edit,MultiEdit,Write,NotebookEdit,NotebookRead,Glob,Grep,LS,"
            "WebFetch,WebSearch,Task,TodoWrite"]


def command(model):
    return [os.environ.get("CLAUDE_BIN") or "claude", "-p", "--model", model, "--output-format", "json", *NO_TOOLS]


def run_claude(prompt, model, timeout):
    """Default runner: returns the CLI's stdout (the JSON envelope)."""
    try:
        with tempfile.TemporaryDirectory(prefix="cv-claude-") as cwd:
            out = subprocess.run(command(model), input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude timed out after {timeout} s")
    except FileNotFoundError:
        raise LLMError("claude CLI not found")
    if out.returncode != 0 and not out.stdout.strip():
        raise LLMError(f"claude exited {out.returncode}: {(out.stderr or '').strip()[:200]}")
    return out.stdout


RUN = run_claude  # tests replace this with a fake: RUN(prompt, model, timeout) -> stdout
DEADLINE = None   # time.monotonic() value set by run.py: no call may run past the whole-run deadline


def extract_json(text):
    """The first JSON object or array in the model's text (it may add prose or a ``` fence)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(value, (dict, list)):
                return value
    raise LLMError("no JSON in the answer: " + text[:120])


def ask_json(prompt, model, timeout=180):
    """claude -p --model <model> --output-format json; returns the parsed dict/list or raises LLMError.
    The timeout is cut to what is left of the whole-run DEADLINE."""
    if DEADLINE is not None:
        left = DEADLINE - time.monotonic()
        if left < 5:
            raise LLMError("whole-run deadline passed")
        timeout = min(timeout, int(left))
    stdout = RUN(prompt, model, timeout)
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError):
        raise LLMError("claude answer is not JSON: " + str(stdout).strip()[:200])
    if not isinstance(envelope, dict):
        raise LLMError("unexpected claude answer")
    if envelope.get("is_error"):
        raise LLMError("claude error: " + str(envelope.get("result"))[:200])
    return _scrub(extract_json(envelope.get("result") or ""))


def _scrub(value):
    """Lone surrogates ('\\ud800' in the model's JSON) are dropped from every string, keys included."""
    if isinstance(value, str):
        return re.sub("[\ud800-\udfff]", "", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {_scrub(k): _scrub(v) for k, v in value.items()}
    return value
