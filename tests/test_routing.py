"""Unit tests for the graph's conditional edges and final status."""
from prbot.graph import final_status, route_after_review, route_after_test
from prbot.models import Finding, PatchProposal, ReviewState, TestResult


def F(sev="high"):
    return Finding(file="a.py", line=1, severity=sev, category="security", title="t", rationale="r")


def S(**kw):
    return ReviewState(repo_path=".", **kw)


def test_review_routes_to_patcher_when_actionable():
    assert route_after_review(S(findings=[F("high")])) == "patcher"


def test_review_skips_patcher_when_clean():
    assert route_after_review(S()) == "finalize"


def test_review_skips_patcher_below_min_severity():
    assert route_after_review(S(findings=[F("low")], min_severity="medium")) == "finalize"


def test_passing_tests_finalize():
    st = S(findings=[F()], attempts=1, test_result=TestResult(passed=True, exit_code=0))
    assert route_after_test(st) == "finalize"


def test_failing_tests_loop_back_to_patcher():
    st = S(findings=[F()], attempts=1, max_attempts=3, test_result=TestResult(passed=False, exit_code=1))
    assert route_after_test(st) == "patcher"


def test_failing_tests_stop_at_attempt_cap():
    st = S(findings=[F()], attempts=3, max_attempts=3, test_result=TestResult(passed=False, exit_code=1))
    assert route_after_test(st) == "finalize"


def test_missing_patch_counts_as_failure():
    st = S(findings=[F()], attempts=1, max_attempts=2, test_result=None)
    assert route_after_test(st) == "patcher"


def test_final_status_values():
    p = PatchProposal(diff="x")
    assert final_status(S()) == "clean"
    assert final_status(S(findings=[F("low")])) == "review_only"
    assert final_status(S(findings=[F()], patch=p, test_result=TestResult(passed=True, exit_code=0))) == "patched"
    assert final_status(S(findings=[F()], patch=p, test_result=TestResult(passed=False, exit_code=1))) == "failed"
