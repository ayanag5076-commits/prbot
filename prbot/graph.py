"""The LangGraph StateGraph wiring the agents together.

    START -> baseline --(suite already failing)--> finalize (aborted) -> END
                |
                v
             reviewer --(no actionable findings)--> finalize
                |
                v
             patcher -> tester --(pass | attempts exhausted)--> finalize
                ^          |
                +--(fail: every failed diff + logs fed back, pivot required)--+
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from langgraph.graph import END, START, StateGraph

from prbot.agents.patcher import Patcher
from prbot.agents.reviewer import Reviewer
from prbot.agents.tester import Tester
from prbot.models import ReviewState
from prbot.publishers import Publisher
from prbot.report import render_report
from prbot.workspace import Workspace

ABORT_MAIN_FAILING = "Review aborted: Main test suite is currently failing."
ABORT_PR_FAILING = "Review aborted: the test suite fails on this PR before any patch, so a patch can't be verified."
ABORT_LOCAL_FAILING = "Review aborted: the test suite is already failing before any patch."


@dataclass
class Agents:
    reviewer: Reviewer
    patcher: Patcher
    tester: Tester
    publisher: Optional[Publisher] = None


# ------------------------------------------------------------------ routing (pure functions, unit-tested)
def route_after_baseline(state: ReviewState) -> str:
    return "finalize" if state.abort_reason else "reviewer"


def route_after_review(state: ReviewState) -> str:
    return "patcher" if state.actionable_findings() else "finalize"


def route_after_test(state: ReviewState) -> str:
    if state.test_result is not None and state.test_result.passed:
        return "finalize"
    if state.attempts >= state.max_attempts:
        return "finalize"
    return "patcher"


def final_status(state: ReviewState) -> str:
    if state.abort_reason:
        return "aborted"
    if not state.actionable_findings():
        return "clean" if not state.findings else "review_only"
    if state.test_result is not None and state.test_result.passed and state.patch is not None:
        return "patched"
    return "failed"


def abort_reason_for(head_passed: bool, base_passed: bool | None) -> str:
    """head = unpatched PR head; base = PR base branch (None when there is no base checkout)."""
    if head_passed:
        return ""
    if base_passed is None:
        return ABORT_LOCAL_FAILING
    return ABORT_MAIN_FAILING if not base_passed else ABORT_PR_FAILING


# ------------------------------------------------------------------ graph
def build_graph(agents: Agents):
    def baseline_node(state: ReviewState) -> dict:
        if not state.run_baseline:
            return {}
        head = agents.tester.baseline(state.repo_path)
        update: dict = {"baseline": head}
        base_passed = None
        if not head.passed and state.base_path:
            base = agents.tester.baseline(state.base_path)
            update["base_baseline"] = base
            base_passed = base.passed
        update["abort_reason"] = abort_reason_for(head.passed, base_passed)
        return update

    def reviewer_node(state: ReviewState) -> dict:
        ws = Workspace(state.repo_path)
        result = agents.reviewer.review(ws, state.changed_files)
        update: dict = {"findings": result.findings, "review_summary": result.summary}
        usage = getattr(agents.reviewer, "last_usage", None)
        if usage:
            entries = usage if isinstance(usage, list) else [usage]
            update["token_usage"] = state.token_usage + [dict(u) for u in entries]
        return update

    def patcher_node(state: ReviewState) -> dict:
        ws = Workspace(state.repo_path)
        targets = state.patch_targets()
        patch = agents.patcher.propose(ws, targets, state.feedback, state.attempted_patches)
        narrowed = len(targets) < len(state.actionable_findings())
        update: dict = {"patch": patch, "attempts": state.attempts + 1, "test_result": None,
                        "patch_scope": [f"{f.file}:{f.line}" for f in targets] if narrowed else []}
        usage = getattr(agents.patcher, "last_usage", None)
        if usage:
            update["token_usage"] = state.token_usage + [dict(usage)]
        return update

    def tester_node(state: ReviewState) -> dict:
        result = agents.tester.verify(state.repo_path, state.patch)
        history = state.attempt_history + [{
            "attempt": state.attempts, "has_patch": state.patch is not None,
            "passed": result.passed, "exit_code": result.exit_code, "runner": result.runner,
            "scope": ", ".join(state.patch_scope) if state.patch_scope else "all findings",
        }]
        update: dict = {"test_result": result, "attempt_history": history, "test_runs": state.test_runs + [result]}
        if not result.passed:
            update["feedback"] = state.feedback + [result.log]  # error recovery: logs go back to Agent B
            if state.patch is not None:
                update["attempted_patches"] = state.attempted_patches + [state.patch.diff]
        return update

    def finalize_node(state: ReviewState) -> dict:
        status = final_status(state)
        done = state.model_copy(update={"status": status})
        report = render_report(done)
        done = done.model_copy(update={"report": report})
        published = agents.publisher.publish(done) if agents.publisher else []
        return {"status": status, "report": report, "published": published}

    g = StateGraph(ReviewState)
    g.add_node("baseline", baseline_node)
    g.add_node("reviewer", reviewer_node)
    g.add_node("patcher", patcher_node)
    g.add_node("tester", tester_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "baseline")
    g.add_conditional_edges("baseline", route_after_baseline, {"reviewer": "reviewer", "finalize": "finalize"})
    g.add_conditional_edges("reviewer", route_after_review, {"patcher": "patcher", "finalize": "finalize"})
    g.add_edge("patcher", "tester")
    g.add_conditional_edges("tester", route_after_test, {"patcher": "patcher", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


def run_review(agents: Agents, initial: ReviewState) -> ReviewState:
    graph = build_graph(agents)
    out = graph.invoke(initial, config={"recursion_limit": 10 + 3 * initial.max_attempts})
    return out if isinstance(out, ReviewState) else ReviewState.model_validate(out)
