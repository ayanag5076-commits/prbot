"""Reviewer grounding, ReAct patcher self-correction inside one attempt, tester behaviour."""
from pathlib import Path

from langchain_core.messages import AIMessage

from prbot.agents.patcher import ReActPatcher, build_task
from prbot.agents.reviewer import LLMReviewer
from prbot.agents.tester import DockerRunner, LocalRunner, Tester
from prbot.context import build_review_context, changed_files_from_diff
from prbot.models import Finding, Findings, PatchProposal
from prbot.offline import HeuristicReviewer, ScriptedChatModel, _call, make_diff, N1_FIX, N1_OLD
from prbot.workspace import Workspace


def test_heuristic_reviewer_finds_seeded_bugs(demo_copy):
    ws = Workspace(demo_copy)
    changed = changed_files_from_diff((demo_copy / "pr.diff").read_text())
    titles = {(f.file, f.category) for f in HeuristicReviewer().review(ws, changed).findings}
    assert ("app/users.py", "security") in titles
    assert ("app/reports.py", "performance") in titles
    assert ("app/users.py", "performance") in titles


def test_review_context_has_numbered_changed_lines(demo_copy):
    ws = Workspace(demo_copy)
    ctx = build_review_context(ws, changed_files_from_diff((demo_copy / "pr.diff").read_text()))
    assert ">   7 |     query = f\"SELECT" in ctx
    assert "function find_user_by_name" in ctx


class FakeStructured:
    def __init__(self, result):
        self.result = result

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        return self.result


def test_llm_reviewer_drops_hallucinated_findings(demo_copy):
    ws = Workspace(demo_copy)
    changed = changed_files_from_diff((demo_copy / "pr.diff").read_text())
    good = Finding(file="app/users.py", line=7, severity="critical", category="security", title="sqli", rationale="r")
    ghost = Finding(file="nope.py", line=1, severity="high", category="security", title="x", rationale="r")
    far = Finding(file="app/users.py", line=999, severity="high", category="security", title="x", rationale="r")
    out = LLMReviewer(FakeStructured(Findings(findings=[good, ghost, far]))).review(ws, changed)
    assert out.findings == [good]


def test_react_patcher_recovers_from_bad_diff_within_one_attempt(demo_copy):
    ws = Workspace(demo_copy)
    good = make_diff(ws, {"app/reports.py": [(N1_OLD, N1_FIX)]})
    bad = "--- a/app/reports.py\n+++ b/app/reports.py\n@@ -1,2 +1,2 @@\n not in file\n-x\n+y\n"
    model = ScriptedChatModel([[
        _call("read_file", path="app/reports.py"),
        _call("write_patch", diff=bad, explanation="first try"),
        AIMessage(content="", tool_calls=[{"name": "nonexistent", "args": {}, "id": "c1", "type": "tool_call"}]),
        _call("write_patch", diff=good, explanation="fixed"),
    ]])
    patch = ReActPatcher(model).propose(ws, [], [], [])
    assert patch is not None and patch.explanation == "fixed"
    assert patch.tool_trace == ["read_file", "write_patch", "nonexistent", "write_patch"]


def test_write_patch_rejects_syntax_errors(demo_copy):
    ws = Workspace(demo_copy)
    broken = make_diff(ws, {"app/reports.py": [("    return totals", "    return totals)")]})
    model = ScriptedChatModel([[_call("write_patch", diff=broken, explanation="oops")]])
    assert ReActPatcher(model, max_steps=2).propose(ws, [], [], []) is None


def test_failed_patch_history_goes_into_system_prompt():
    from prbot.agents.patcher import build_system
    f = Finding(file="a.py", line=1, severity="high", category="security", title="t", rationale="r")
    sys_prompt = build_system(["--- a/a.py\n+++ b/a.py\n-v1", "--- a/a.py\n+++ b/a.py\n-v2"],
                              ["E   sqlite3.OperationalError", "E   AssertionError"])
    assert "Failed attempt 1" in sys_prompt and "Failed attempt 2" in sys_prompt
    assert "structurally different" in sys_prompt and "OperationalError" in sys_prompt
    assert "attempt 3" in build_task([f], ["log1", "log2"], ["d1", "d2"])


def test_write_patch_rejects_repeat_of_failed_patch(demo_copy):
    ws = Workspace(demo_copy)
    diff = make_diff(ws, {"app/reports.py": [(N1_OLD, N1_FIX)]})
    reformatted = diff.replace("@@ -5,", "@@ -6,")  # same content, different hunk header
    model = ScriptedChatModel([[_call("write_patch", diff=reformatted, explanation="again")]])
    assert ReActPatcher(model, max_steps=1).propose(ws, [], ["log"], [diff]) is None


def test_tester_reports_missing_patch(demo_copy):
    r = Tester(LocalRunner()).verify(str(demo_copy), None)
    assert not r.passed and "did not submit" in r.log


def test_tester_reports_unappliable_patch(demo_copy):
    bad = PatchProposal(diff="--- a/app/users.py\n+++ b/app/users.py\n@@ -1 +1 @@\n-nope\n+x\n")
    r = Tester(LocalRunner()).verify(str(demo_copy), bad)
    assert not r.passed and "failed to apply" in r.log


def test_tester_runs_tests_without_touching_original(demo_copy):
    ws = Workspace(demo_copy)
    good = PatchProposal(diff=make_diff(ws, {"app/reports.py": [(N1_OLD, N1_FIX)]}))
    before = (demo_copy / "app/reports.py").read_text()
    r = Tester(LocalRunner(), timeout=60).verify(str(demo_copy), good)
    assert r.passed, r.log
    assert (demo_copy / "app/reports.py").read_text() == before


def test_docker_command_is_locked_down():
    cmd = DockerRunner("img:tag").build_cmd(Path("/tmp/x"), "c1")
    joined = " ".join(cmd)
    for flag in ["--network none", "--read-only", "--cap-drop ALL", "--pids-limit", "--memory", "/tmp/x:/workspace:ro"]:
        assert flag in joined
    assert cmd[cmd.index("img:tag") + 1:] == ["python", "-m", "pytest", "-p", "no:cacheprovider", "-rA"]


def test_hotspot_hints_flag_n_plus_one_and_quadratic(demo_copy):
    from prbot.context import performance_hotspots

    reports = performance_hotspots((demo_copy / "app/reports.py").read_text())
    users = performance_hotspots((demo_copy / "app/users.py").read_text())
    assert any("execute" in h and "N+1" in h for h in reports)
    assert any("O(n^2)" in h and "emails" in h for h in users)
    ctx = build_review_context(Workspace(demo_copy), changed_files_from_diff((demo_copy / "pr.diff").read_text()))
    assert "Static hotspot hints" in ctx


def test_llm_reviewer_runs_focused_passes_and_merges(demo_copy):
    ws = Workspace(demo_copy)
    changed = changed_files_from_diff((demo_copy / "pr.diff").read_text())
    sqli = Finding(file="app/users.py", line=7, severity="critical", category="security", title="sqli", rationale="r")
    n1 = Finding(file="app/reports.py", line=10, severity="high", category="performance", title="n+1", rationale="r")
    quad = Finding(file="app/users.py", line=15, severity="high", category="performance", title="o(n^2)", rationale="r")

    class TwoPass:
        def __init__(self):
            self.calls = []

        def with_structured_output(self, schema, **kw):
            return self

        def invoke(self, messages):
            self.calls.append(messages[0].content)
            # each pass "sees" everything; the reviewer keeps only the pass's own category, deduped
            return Findings(findings=[sqli, n1, quad, sqli])

    model = TwoPass()
    out = LLMReviewer(model).review(ws, changed)
    assert len(model.calls) == 2
    assert "ONLY security" in model.calls[0] and "ONLY performance" in model.calls[1]
    assert out.findings == [sqli, n1, quad]
