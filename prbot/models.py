"""Pydantic models: structured LLM outputs and the LangGraph state."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
Category = Literal["security", "performance"]
SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ---------------------------------------------------------------- reviewer (Agent A)
class Finding(BaseModel):
    """One issue found by the reviewer. This is the structured-output schema."""

    file: str = Field(description="Repo-relative path of the file, e.g. 'app/users.py'.")
    line: int = Field(ge=1, description="1-based line number (new side of the diff) where the issue is.")
    end_line: Optional[int] = Field(default=None, ge=1, description="Optional last line of the issue span.")
    severity: Severity
    category: Category
    title: str = Field(description="Short headline, e.g. 'SQL injection via f-string query'.")
    rationale: str = Field(description="Why this is a bug and what the impact is.")
    suggestion: str = Field(default="", description="Concrete fix suggestion.")

    def is_actionable(self, min_severity: str = "medium") -> bool:
        return SEVERITY_RANK[self.severity] >= SEVERITY_RANK[min_severity]


class Findings(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    summary: str = Field(default="", description="One-paragraph overall review summary.")


# ---------------------------------------------------------------- inputs
class ChangedFile(BaseModel):
    path: str
    status: str = "modified"  # added | modified | removed | renamed
    patch: Optional[str] = None  # unified-diff hunks for this file (GitHub's `patch` field)
    changed_lines: list[int] = Field(default_factory=list)  # new-side lines added/modified


class PRMeta(BaseModel):
    repo: str
    number: int
    title: str = ""
    head_ref: str = ""
    head_sha: str = ""
    base_ref: str = ""
    same_repo: bool = True
    url: str = ""


# ---------------------------------------------------------------- patcher (Agent B)
class PatchProposal(BaseModel):
    diff: str = Field(description="Unified diff (git style, a/ b/ prefixes) against the repo root.")
    explanation: str = ""
    files: list[str] = Field(default_factory=list)
    tool_trace: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- tester (Agent C)
class TestResult(BaseModel):
    __test__ = False  # keep pytest from collecting this class

    passed: bool
    exit_code: int
    log: str = ""
    duration_s: float = 0.0
    runner: str = "docker"
    timed_out: bool = False


# ---------------------------------------------------------------- graph state
Status = Literal["pending", "clean", "patched", "failed", "review_only", "aborted"]


class ReviewState(BaseModel):
    """Shared state flowing through the StateGraph."""

    repo_path: str
    base_path: Optional[str] = None  # checkout of the PR's base (merge target), used to attribute baseline failures
    changed_files: list[ChangedFile] = Field(default_factory=list)
    pr: Optional[PRMeta] = None

    findings: list[Finding] = Field(default_factory=list)
    review_summary: str = ""

    patch: Optional[PatchProposal] = None
    test_result: Optional[TestResult] = None
    attempts: int = 0
    max_attempts: int = 3
    min_severity: Severity = "medium"
    feedback: list[str] = Field(default_factory=list)  # test logs fed back to the patcher
    attempted_patches: list[str] = Field(default_factory=list)  # every diff that failed verification, oldest first
    run_baseline: bool = True
    baseline: Optional[TestResult] = None  # unpatched PR head
    base_baseline: Optional[TestResult] = None  # PR base branch, run only when the head baseline fails
    abort_reason: str = ""
    token_usage: list[dict] = Field(default_factory=list)  # one TokenUsage dict per agent call batch
    test_runs: list[TestResult] = Field(default_factory=list)  # every verification run, for latency metrics
    attempt_history: list[dict] = Field(default_factory=list)

    status: Status = "pending"
    report: str = ""
    published: list[str] = Field(default_factory=list)

    def actionable_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.is_actionable(self.min_severity)]
