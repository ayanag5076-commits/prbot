"""Per-node token telemetry: reviewer passes and patcher attempts are priced separately."""
from prbot.cli import main
from prbot.metrics import (RunRecord, TokenUsage, append_record, breakdown_summary, format_breakdown,
                           node_breakdown)


def _usages():
    return [
        TokenUsage(agent="reviewer", step="security", input_tokens=2000, output_tokens=200, calls=1),
        TokenUsage(agent="reviewer", step="performance", input_tokens=2100, output_tokens=300, calls=1),
        TokenUsage(agent="patcher", attempt=1, input_tokens=5000, output_tokens=800, calls=4),
        TokenUsage(agent="patcher", attempt=2, input_tokens=6000, output_tokens=600, calls=3),
    ]


def test_node_breakdown_splits_passes_and_attempts():
    rows = node_breakdown(_usages(), "gpt-4.1-mini")
    names = [r["node"] for r in rows]
    assert names == ["reviewer:security", "reviewer:performance", "patcher:attempt1", "patcher:attempt2"]
    assert sum(r["tokens"] for r in rows) == 17000
    assert abs(sum(r["share_pct"] for r in rows) - 100) < 0.5
    sec = rows[0]
    assert sec["cost_usd"] == round((2000 * 0.40 + 200 * 1.60) / 1e6, 6)
    assert "reviewer:security" in format_breakdown(rows)


def test_legacy_reviewer_entry_without_step_still_labels():
    rows = node_breakdown([TokenUsage(agent="reviewer", input_tokens=10, output_tokens=1, calls=2)], "gpt-4.1-mini")
    assert rows[0]["node"] == "reviewer"


def test_breakdown_summary_and_cli(tmp_path, capsys):
    ledger = tmp_path / "m.jsonl"
    for _ in range(2):
        append_record(ledger, RunRecord(target="t", model="gpt-4.1-mini", status="patched", findings=3,
                                        actionable=3, attempts=2, tokens=_usages(), total_tokens=17000))
    rows = {r["node"]: r for r in breakdown_summary(__import__("prbot.metrics", fromlist=["x"]).load_records(ledger))}
    assert rows["reviewer:performance"]["runs"] == 2 and rows["reviewer:performance"]["avg_tokens"] == 2400
    assert main(["metrics", "--file", str(ledger), "--by-node"]) == 0
    out = capsys.readouterr().out
    assert "| reviewer:security | 2 | 2200 |" in out


def test_reviewer_records_one_usage_per_pass():
    from types import SimpleNamespace

    from prbot.agents.reviewer import LLMReviewer
    from prbot.models import Findings

    class Structured:
        def invoke(self, messages):
            raw = SimpleNamespace(usage_metadata={"input_tokens": 100, "output_tokens": 10})
            return {"raw": raw, "parsed": Findings(findings=[], summary=""), "parsing_error": None}

    class Model:
        def with_structured_output(self, schema, include_raw=False):
            return Structured()

    class WS:
        def exists(self, p): return True
        def read_text(self, p): return ""

    import prbot.agents.reviewer as rv
    orig = rv.build_review_context
    rv.build_review_context = lambda ws, changed: "ctx"
    try:
        r = LLMReviewer(Model())
        r.review(WS(), [])
    finally:
        rv.build_review_context = orig
    assert [u["step"] for u in r.last_usage] == ["security", "performance"]
    assert all(u["calls"] == 1 and u["input_tokens"] == 100 for u in r.last_usage)
