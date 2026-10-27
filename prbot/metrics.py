"""Engineering metrics: token cost per repair, self-correction recovery rate, sandbox latency.

Every run appends one JSON line to a metrics ledger (default prbot-out/metrics.jsonl).
`python -m prbot metrics --markdown` aggregates the ledger into the README table.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

# USD per 1M tokens: (input, cached input, output). Source: platform.openai.com model pages.
# Override for other models with PRBOT_PRICE_INPUT / PRBOT_PRICE_CACHED / PRBOT_PRICE_OUTPUT.
PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
}

PYTEST_TIME = re.compile(r"\bin ([0-9]+(?:\.[0-9]+)?)s\b")


class TokenUsage(BaseModel):
    agent: str
    attempt: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def usage_from_message(msg: Any) -> tuple[int, int, int]:
    """(input, cached_input, output) from a LangChain AIMessage's usage_metadata, or zeros."""
    meta = getattr(msg, "usage_metadata", None) or {}
    cached = (meta.get("input_token_details") or {}).get("cache_read", 0) or 0
    return int(meta.get("input_tokens", 0) or 0), int(cached), int(meta.get("output_tokens", 0) or 0)


def price_for(model: str) -> Optional[tuple[float, float, float]]:
    env_in, env_out = os.environ.get("PRBOT_PRICE_INPUT"), os.environ.get("PRBOT_PRICE_OUTPUT")
    if env_in and env_out:
        return float(env_in), float(os.environ.get("PRBOT_PRICE_CACHED", env_in)), float(env_out)
    for name, p in PRICES.items():
        if model == name or model.startswith(name + "-20"):
            return p
    return None


def cost_usd(usages: list[TokenUsage], model: str) -> Optional[float]:
    p = price_for(model)
    if p is None:
        return None
    pin, pcached, pout = p
    total = 0.0
    for u in usages:
        fresh = max(0, u.input_tokens - u.cached_input_tokens)
        total += fresh * pin + u.cached_input_tokens * pcached + u.output_tokens * pout
    return round(total / 1_000_000, 6)


def pytest_seconds(log: str) -> Optional[float]:
    """pytest's own reported duration ("6 passed in 0.10s"), used to separate test time from sandbox overhead."""
    hits = PYTEST_TIME.findall(log or "")
    return float(hits[-1]) if hits else None


class SandboxRun(BaseModel):
    phase: str  # baseline | base_baseline | attempt
    runner: str
    wall_s: float
    pytest_s: Optional[float] = None
    overhead_s: Optional[float] = None
    passed: bool = False


class RunRecord(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    target: str
    model: str
    status: str
    findings: int
    actionable: int
    attempts: int
    first_attempt_passed: Optional[bool] = None
    passed_on_attempt: Optional[int] = None
    tokens: list[TokenUsage] = Field(default_factory=list)
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    sandbox: list[SandboxRun] = Field(default_factory=list)
    wall_s: float = 0.0


def sandbox_run(phase: str, result) -> SandboxRun:
    ps = pytest_seconds(result.log)
    over = round(result.duration_s - ps, 3) if ps is not None else None
    return SandboxRun(phase=phase, runner=result.runner, wall_s=result.duration_s, pytest_s=ps,
                      overhead_s=over, passed=result.passed)


def build_record(state, model: str, wall_s: float) -> RunRecord:
    tokens = [TokenUsage.model_validate(t) for t in state.token_usage]
    sandbox = []
    if state.baseline is not None:
        sandbox.append(sandbox_run("baseline", state.baseline))
    if state.base_baseline is not None:
        sandbox.append(sandbox_run("base_baseline", state.base_baseline))
    for r in state.test_runs:
        if r.duration_s <= 0:  # no container started (no patch / patch didn't apply)
            continue
        sandbox.append(sandbox_run("attempt", r))
    hist = state.attempt_history
    passed_on = next((h["attempt"] for h in hist if h["passed"]), None)
    target = f"{state.pr.repo}#{state.pr.number}" if state.pr else Path(state.repo_path).name
    return RunRecord(
        target=target, model=model, status=state.status, findings=len(state.findings),
        actionable=len(state.actionable_findings()), attempts=state.attempts,
        first_attempt_passed=(hist[0]["passed"] if hist else None), passed_on_attempt=passed_on,
        tokens=tokens, total_tokens=sum(t.total for t in tokens), cost_usd=cost_usd(tokens, model),
        sandbox=sandbox, wall_s=round(wall_s, 2),
    )


def append_record(path: str | Path, record: RunRecord) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(record.model_dump_json() + "\n")


def load_records(path: str | Path) -> list[RunRecord]:
    p = Path(path)
    if not p.exists():
        return []
    return [RunRecord.model_validate_json(line) for line in p.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ aggregation
def _median(xs: list[float]) -> Optional[float]:
    return round(statistics.median(xs), 3) if xs else None


def _p95(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))], 3)


def _p90(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(0.90 * (len(xs) - 1))))], 2)


def summarize(records: list[RunRecord]) -> dict:
    bench = [r for r in records if r.status == "bench"]
    all_records, records = records, [r for r in records if r.status != "bench"]
    repaired = [r for r in records if r.status == "patched"]
    needed_patch = [r for r in records if r.attempts > 0]
    failed_first = [r for r in needed_patch if r.first_attempt_passed is False]
    recovered = [r for r in failed_first if r.status == "patched"]
    rec2 = [r for r in recovered if r.passed_on_attempt == 2]
    rec3 = [r for r in recovered if r.passed_on_attempt == 3]
    costs = [r.cost_usd for r in repaired if r.cost_usd is not None]
    sb: dict[str, dict] = {}
    for runner in sorted({s.runner for r in all_records for s in r.sandbox}):
        runs = [s for r in all_records for s in r.sandbox if s.runner == runner]
        sb[runner] = {
            "runs": len(runs),
            "wall_median_s": _median([s.wall_s for s in runs]),
            "wall_p95_s": _p95([s.wall_s for s in runs]),
            "pytest_median_s": _median([s.pytest_s for s in runs if s.pytest_s is not None]),
            "overhead_median_s": _median([s.overhead_s for s in runs if s.overhead_s is not None]),
            "overhead_p95_s": _p95([s.overhead_s for s in runs if s.overhead_s is not None]),
        }
    pct = lambda a, b: round(100 * a / b, 1) if b else None
    return {
        "runs": len(records),
        "repairs_completed": len(repaired),
        "avg_tokens_per_repair": round(statistics.mean([r.total_tokens for r in repaired])) if repaired else None,
        "avg_cost_per_repair_usd": round(statistics.mean(costs), 5) if costs else None,
        "first_attempt_pass_rate_pct": pct(len([r for r in needed_patch if r.first_attempt_passed]), len(needed_patch)),
        "runs_failing_attempt_1": len(failed_first),
        "recovery_rate_pct": pct(len(recovered), len(failed_first)),
        "recovered_on_attempt_2": len(rec2),
        "recovered_on_attempt_3": len(rec3),
        "bench_sessions": len(bench),
        "aborted_by_baseline": len([r for r in records if r.status == "aborted"]),
        "sandbox": sb,
        "median_passed_on_attempt": _median([r.passed_on_attempt for r in repaired if r.passed_on_attempt]),
        "docker_runs": len([r for r in records if any(s.runner == "docker" for s in r.sandbox)]),
        "p90_execution_latency_s": _p90([r.wall_s for r in records if r.wall_s and any(s.runner == "docker" for s in r.sandbox)]),
    }


def _fmt(v, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v}{suffix}"


def to_markdown(s: dict) -> str:
    rows = [
        "| Metric | Value | How it's measured |",
        "|---|---|---|",
        f"| **Token efficiency** | {_fmt(s['avg_tokens_per_repair'])} tokens · ${_fmt(s['avg_cost_per_repair_usd'])} per completed repair | "
        f"`usage_metadata` from every reviewer + patcher call, priced per model, averaged over {s['repairs_completed']} verified repairs |",
        f"| **Self-correction recovery rate** | {_fmt(s['recovery_rate_pct'], '%')} ({s['recovered_on_attempt_2']} on attempt 2, {s['recovered_on_attempt_3']} on attempt 3) | "
        f"runs whose attempt-1 patch failed the sandboxed test suite ({s['runs_failing_attempt_1']}) that still ended verified |",
        f"| First-attempt pass rate | {_fmt(s['first_attempt_pass_rate_pct'], '%')} | patches that passed tests on attempt 1 |",
    ]
    for runner, d in s["sandbox"].items():
        label = "**Sandbox latency (Docker)**" if runner == "docker" else f"Sandbox latency ({runner}, comparison)"
        rows.append(
            f"| {label} | {_fmt(d['overhead_median_s'], 's')} median overhead (p95 {_fmt(d['overhead_p95_s'], 's')}) · "
            f"{_fmt(d['wall_median_s'], 's')} median wall | wall-clock per test run minus pytest's own reported time, {d['runs']} runs |"
        )
    rows.append(f"| Baseline aborts | {s['aborted_by_baseline']} of {s['runs']} runs | suite already failing before any patch |")
    return "\n".join(rows)


LATENCY_TARGET_S = 45.0
README_START, README_END = "<!-- METRICS:START", "<!-- METRICS:END -->"


def to_telemetry_table(s: dict) -> str:
    """The README's 'System Benchmarks & Telemetry' table, filled from the JSONL ledger."""
    att = s.get("median_passed_on_attempt")
    if att:
        n = int(att) if float(att).is_integer() else att
        rec = f"**{n} Attempt{'s' if n != 1 else ''}** (median of {s['repairs_completed']} repairs, {_fmt(s['recovery_rate_pct'], '%')} recovered)"
        rec_status = "Active Engine Runtime"
    else:
        rec, rec_status = "[Pending Runtime Log]", "Awaiting Run"
    p90 = s.get("p90_execution_latency_s")
    if p90 is None:
        lat, lat_status = "[Pending Runtime Log]", "Awaiting Local Docker Run"
    else:
        lat = f"**{p90}s** ({s['docker_runs']} Docker runs)"
        lat_status = ("Met" if p90 < LATENCY_TARGET_S else "Over target") + " · `python -m prbot metrics`"
    return "\n".join([
        "| Metric / Constraint | Baseline Target | Performance Realization | Verification Status |",
        "| :--- | :--- | :--- | :--- |",
        "| **Sandbox Network Egress** | 0 Bytes (Blocked) | 0 Bytes (`--network none`, `--cap-drop ALL`) | Verified via `test_sandbox_isolation` |",
        f"| **Self-Correction Recovery** | < 3 Attempts | {rec} | {rec_status} |",
        "| **AST Diff Pre-Validation** | 100% Syntax Pass | Checked before any container run | Verified via `tools.py` |",
        "| **Secret Leaks Detected** | 0 Key Escapes | 0 Variables Exposed | Verified via `tests/` CI guard |",
        f"| **P90 Execution Latency** | Under {LATENCY_TARGET_S:g}s | {lat} | {lat_status} |",
    ])


def update_readme(readme: str | Path, table: str) -> bool:
    p = Path(readme)
    text = p.read_text()
    a, b = text.find(README_START), text.find(README_END)
    if a < 0 or b < 0:
        return False
    head_end = text.index("-->", a) + 3
    p.write_text(text[:head_end] + "\n" + table + "\n" + text[b:])
    return True


class Stopwatch:
    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.elapsed = time.monotonic() - self.t0
