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
- performance: N+1 queries (a query/request per loop iteration instead of one JOIN/IN/GROUP BY), accidental quadratic
  loops (nested loops over the same collection, `x in list` inside a loop), repeated I/O in loops, unbounded memory
  growth, blocking calls in hot paths.
Rules:
- A pull request often contains SEVERAL independent bugs. Examine every changed function separately and report every
  distinct issue; never stop after the first or most severe one.
- Every finding must point at an exact line number from the numbered code (new side of the diff).
- Prefer lines marked with '>' (changed lines). Do not report style nits, naming, or missing docs.
- severity: critical (exploitable/data loss), high (N+1 or O(n^2) on data that grows with users/orders/rows), medium, low.
- "Static hotspot hints" are machine-generated leads: confirm or dismiss each one explicitly in your reasoning.
- If nothing qualifies, return an empty findings list.
"""

PASS_FOCUS = {
    "security": "This pass: report ONLY security findings (category 'security').",
    "performance": "This pass: report ONLY performance findings (category 'performance'). Check every loop.",
}


class Reviewer(Protocol):
    def review(self, ws: Workspace, changed: list[ChangedFile]) -> Findings: ...


class LLMReviewer:
    """Uses the chat model's structured-output (function-calling / JSON-schema) mode to return Findings."""

    def __init__(self, chat_model, passes: tuple[str, ...] = ("security", "performance")):
        self.passes = passes
        try:
            self.structured = chat_model.with_structured_output(Findings, include_raw=True)
        except TypeError:  # simple fakes
            self.structured = chat_model.with_structured_output(Findings)
        self.last_usage: dict | None = None

    def review(self, ws: Workspace, changed: list[ChangedFile]) -> Findings:
        """One focused structured-output call per category pass (security, performance), merged and de-duplicated.
        Single-pass reviews tend to stop at the most severe bug; focused passes keep recall up for ~2x input tokens."""
        from prbot.metrics import usage_from_message

        context = build_review_context(ws, changed)
        usage = {"agent": "reviewer", "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "calls": 0}
        merged: list = []
        summaries: list[str] = []
        for focus in self.passes or ("",):
            messages = [
                SystemMessage(REVIEWER_SYSTEM + ("\n" + PASS_FOCUS[focus] if focus in PASS_FOCUS else "")),
                HumanMessage(f"Pull request changes:\n\n{context}\n\nReturn your findings."),
            ]
            result = self.structured.invoke(messages)
            if isinstance(result, dict) and "parsed" in result:  # include_raw=True
                i, c, o = usage_from_message(result.get("raw"))
                usage["input_tokens"] += i
                usage["cached_input_tokens"] += c
                usage["output_tokens"] += o
                usage["calls"] += 1
                if result.get("parsed") is None:
                    raise ValueError(f"Reviewer output failed schema validation: {result.get('parsing_error')}")
                result = result["parsed"]
            part = result if isinstance(result, Findings) else Findings.model_validate(result)
            merged.extend(f for f in part.findings if focus not in PASS_FOCUS or f.category == focus)
            if part.summary:
                summaries.append(part.summary)
        if usage["calls"]:
            self.last_usage = usage
        return _ground(Findings(findings=_dedupe(merged), summary=" ".join(summaries)), ws, changed)


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


def _dedupe(findings: list) -> list:
    """Collapse findings that hit the same file, line and category (passes can overlap)."""
    seen, out = set(), []
    for f in findings:
        key = (f.file, f.line, f.category)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
