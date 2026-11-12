"""Shared fakes: the claude CLI is never called from tests."""
import json


def envelope(result, is_error=False):
    """What `claude -p --output-format json` prints."""
    return json.dumps({"type": "result", "is_error": is_error, "result": result})


class FakeRun:
    """Stands in for llm.RUN: records prompts, returns canned CLI stdout in order, or raises."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, prompt, model, timeout):
        self.calls.append({"prompt": prompt, "model": model, "timeout": timeout})
        if not self.answers:
            raise AssertionError("unexpected claude call")
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a
