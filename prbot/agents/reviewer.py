"""Agent A: reviews the changed code and returns structured Findings."""
from __future__ import annotations

from typing import Protocol

from langchain_core.messages import HumanMessage, SystemMessage

from prbot.context import build_review_context
from prbot.models import ChangedFile, Findings
from prbot.workspace import Workspace

REVIEWER_SYSTEM = """You are a senior application-security and performance reviewer.
Review ONLY the changed code in a pull request. Report real, concrete bugs in two categories:
- security: injection (SQL, shell, path), unsafe deserialization, secrets in code, missing authz checks, SSRF, XSS, weak crypto.
- performance: N+1 queries, accidental quadratic loops, repeated I/O in loops, unbounded memory growth, blocking calls in hot paths.
Rules:
- Every finding must point at an exact line number from the numbered code (new side of the diff).
- Prefer lines marked with '>' (changed lines). Do not report style nits, naming, or missing docs.
- severity: critical (exploitable/data loss), high, medium, low.
- If nothing qualifies, return an empty findings list.
"""


class Reviewer(Protocol):
    def review(self, ws: Workspace, changed: list[ChangedFile]) -> Findings: ...


class LLMReviewer:
    """Uses the chat model's structured-output (function-calling / JSON-schema) mode to return Findings."""

    def __init__(self, chat_model):
        try:
            self.structured = chat_model.with_structured_output(Findings, include_raw=True)
        except TypeError:  # simple fakes
            self.structured = chat_model.with_structured_output(Findings)
        self.last_usage: dict | None = None

    def review(self, ws: Workspace, changed: list[ChangedFile]) -> Findings:
        context = build_review_context(ws, changed)
        messages = [
            SystemMessage(REVIEWER_SYSTEM),
            HumanMessage(f"Pull request changes:\n\n{context}\n\nReturn your findings."),
        ]
        result = self.structured.invoke(messages)
        if isinstance(result, dict) and "parsed" in result:  # include_raw=True
            from prbot.metrics import usage_from_message

            i, c, o = usage_from_message(result.get("raw"))
            self.last_usage = {"agent": "reviewer", "input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "calls": 1}
            if result.get("parsed") is None:
                raise ValueError(f"Reviewer output failed schema validation: {result.get('parsing_error')}")
            result = result["parsed"]
        findings = result if isinstance(result, Findings) else Findings.model_validate(result)
        return _ground(findings, ws, changed)


def _ground(findings: Findings, ws: Workspace, changed: list[ChangedFile]) -> Findings:
    """Drop hallucinated findings (unknown files / out-of-range lines)."""
    paths = {c.path for c in changed}
    kept = []
    for f in findings.findings:
        if f.file not in paths or not ws.exists(f.file):
            continue
        if f.line > len(ws.read_text(f.file).splitlines()):
            continue
        kept.append(f)
    return Findings(findings=kept, summary=findings.summary)
