"""Markdown rendering for the final report and the GitHub review body."""
from __future__ import annotations

from prbot.models import Finding, ReviewState

ICON = {"critical": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡"}


def finding_comment(f: Finding) -> str:
    body = f"**{ICON[f.severity]} {f.severity.upper()} · {f.category}: {f.title}**\n\n{f.rationale}"
    if f.suggestion:
        body += f"\n\n_Suggested fix:_ {f.suggestion}"
    return body + "\n\n<sub>prbot · automated review</sub>"


def render_report(state: ReviewState) -> str:
    lines = ["# prbot review report", ""]
    if state.pr:
        lines += [f"**PR:** {state.pr.repo}#{state.pr.number} {state.pr.title}".rstrip(), ""]
    if state.baseline is not None and not state.abort_reason:
        lines += [f"**Baseline:** tests pass on the unpatched PR ({state.baseline.runner}).", ""]
    lines += [f"**Status:** `{state.status}` · **Patch attempts:** {state.attempts}/{state.max_attempts}", ""]
    if state.abort_reason:
        lines += ["## ⛔ Aborted", state.abort_reason, ""]
        for label, r in (("PR head (unpatched)", state.baseline), ("Base branch", state.base_baseline)):
            if r is not None:
                lines += [f"**{label}:** exit code {r.exit_code}", "```", r.log.strip()[-2000:], "```", ""]
        return "\n".join(lines)
    if state.review_summary:
        lines += ["## Summary", state.review_summary, ""]
    lines += ["## Findings", ""]
    if not state.findings:
        lines.append("No security or performance issues found.")
    else:
        lines += ["| Severity | Category | Location | Issue |", "|---|---|---|---|"]
        for f in state.findings:
            lines.append(f"| {ICON[f.severity]} {f.severity} | {f.category} | `{f.file}:{f.line}` | {f.title} |")
        lines.append("")
        for f in state.findings:
            lines += [f"### `{f.file}:{f.line}` {f.title}", f.rationale]
            if f.suggestion:
                lines.append(f"*Fix:* {f.suggestion}")
            lines.append("")
    if state.attempt_history:
        lines += ["## Self-correction loop", "", "| Attempt | Scope | Patch | Tests | Runner |", "|---|---|---|---|---|"]
        for h in state.attempt_history:
            lines.append(f"| {h['attempt']} | {h.get('scope', 'all findings')} | {'yes' if h['has_patch'] else 'no'} | {'✅ pass' if h['passed'] else '❌ fail'} | {h['runner']} |")
        lines.append("")
        if state.patch_scope and state.status == "patched":
            left = [f for f in state.actionable_findings() if f"{f.file}:{f.line}" not in state.patch_scope]
            if left:
                lines += ["**Partial fix:** the full patch kept failing, so the last attempt fixed only the most severe "
                          "finding(s). Still open: " + ", ".join(f"`{f.file}:{f.line}` {f.title}" for f in left), ""]
    if state.patch:
        verdict = "verified ✅" if state.status == "patched" else "NOT verified ❌"
        lines += [f"## Proposed patch ({verdict})", "", state.patch.explanation, "", "```diff", state.patch.diff.rstrip(), "```", ""]
    if state.test_result:
        lines += ["## Last test run", "", f"exit code {state.test_result.exit_code} · {state.test_result.duration_s}s · {state.test_result.runner}",
                  "", "```", state.test_result.log.strip()[-2500:], "```", ""]
    return "\n".join(lines)
