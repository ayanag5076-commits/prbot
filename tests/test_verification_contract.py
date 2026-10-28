"""The repair loop must not be able to change what 'tests pass' means.

1. write_patch (and Tester.verify, as a backstop) refuse diffs that touch tests, conftest.py or pytest settings.
2. A patched run only passes if every test ID that passed on the baseline still passes: exit 0 is not enough.
"""
import pytest

from prbot.agents.tester import LocalRunner, Tester
from prbot.guard import parse_outcomes, protected_paths_in_diff, protected_reason
from prbot.models import PatchProposal
from prbot.offline import N1_FIX, N1_OLD, make_diff
from prbot.tools import PatchHolder, make_tools
from prbot.workspace import Workspace


@pytest.mark.parametrize("path", [
    "tests/test_users.py", "tests/__init__.py", "tests/helpers/data.py", "app/test_orders.py", "app/orders_test.py",
    "conftest.py", "tests/conftest.py", "app/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
    "./pytest.ini", ".coveragerc", "sitecustomize.py", "app/hook.pth",
])
def test_contract_files_are_protected(path):
    assert protected_reason(path)


@pytest.mark.parametrize("path", ["app/users.py", "app/reports.py", "app/testing_utils_ok.py", "contest.py", "README.md"])
def test_application_files_are_patchable(path):
    assert protected_reason(path) == ""


def _edit(old, new):
    old = old if old == "/dev/null" else f"a/{old}"
    return f"--- {old}\n+++ b/{new}\n@@ -1,1 +1,1 @@\n-x\n+y\n"


def test_diff_scan_sees_both_sides_of_renames_and_deletes():
    assert set(protected_paths_in_diff(_edit("tests/test_users.py", "app/moved.py"))) == {"tests/test_users.py"}
    assert set(protected_paths_in_diff(_edit("/dev/null", "app/conftest.py"))) == {"app/conftest.py"}
    assert protected_paths_in_diff(_edit("app/users.py", "app/users.py")) == {}


def _write_patch(ws, diff):
    holder = PatchHolder()
    tool = {t.name: t for t in make_tools(ws, holder)}["write_patch"]
    return holder, tool.invoke({"diff": diff, "explanation": "x"})


@pytest.mark.parametrize("path, old, new", [
    ("tests/test_reports.py", "def test_", "def _disabled_test_"),        # weaken / remove an assertion
    ("pytest.ini", "[pytest]", "[pytest]\naddopts = -k 'not users'"),     # deselect via collection settings
])
def test_write_patch_rejects_contract_edits(demo_copy, path, old, new):
    ws = Workspace(demo_copy)
    diff = make_diff(ws, {path: [(old, new)]})
    holder, out = _write_patch(ws, diff)
    assert out.startswith("ERROR") and "verification contract" in out and path in out
    assert holder.patch is None and holder.rejected


def test_write_patch_rejects_mixed_source_and_test_patch(demo_copy):
    ws = Workspace(demo_copy)
    diff = make_diff(ws, {"app/reports.py": [(N1_OLD, N1_FIX)], "tests/test_reports.py": [("def test_", "def _x_")]})
    holder, out = _write_patch(ws, diff)
    assert out.startswith("ERROR") and holder.patch is None


def test_write_patch_still_accepts_source_only_fix(demo_copy):
    ws = Workspace(demo_copy)
    holder, out = _write_patch(ws, make_diff(ws, {"app/reports.py": [(N1_OLD, N1_FIX)]}))
    assert out.startswith("OK") and holder.patch is not None


def test_tester_refuses_protected_patch_even_if_it_bypassed_write_patch(demo_copy):
    ws = Workspace(demo_copy)
    sneaky = PatchProposal(diff=make_diff(ws, {"app/reports.py": [(N1_OLD, N1_FIX)],
                                               "tests/test_reports.py": [("def test_", "def _x_")]}))
    r = Tester(LocalRunner(), timeout=60).verify(str(demo_copy), sneaky)
    assert not r.passed and r.exit_code == -1 and r.protected_paths and "Patch not run" in r.log


def test_baseline_records_passing_test_ids(demo_copy):
    base = Tester(LocalRunner(), timeout=60).baseline(str(demo_copy))
    assert base.passed and base.passed_ids
    assert all("::" in i for i in base.passed_ids)
    assert any(i.startswith("tests/test_users.py::") for i in base.passed_ids)


def test_verified_patch_keeps_every_baseline_id(demo_copy):
    tester = Tester(LocalRunner(), timeout=60)
    base = tester.baseline(str(demo_copy))
    good = PatchProposal(diff=make_diff(Workspace(demo_copy), {"app/reports.py": [(N1_OLD, N1_FIX)]}))
    r = tester.verify(str(demo_copy), good, base)
    assert r.passed, r.log
    assert set(base.passed_ids) <= set(r.passed_ids) and r.missing_ids == []


def test_green_exit_with_a_shrunken_suite_is_not_a_pass(demo_copy):
    """Source-only patch that makes pytest skip a whole test module at import: exit 0, fewer tests."""
    tester = Tester(LocalRunner(), timeout=60)
    base = tester.baseline(str(demo_copy))
    src = (demo_copy / "app/reports.py").read_text()
    first = src.splitlines()[0]
    diff = make_diff(Workspace(demo_copy), {"app/reports.py": [
        (first, "import pytest\npytest.skip('quietly', allow_module_level=True)\n" + first)]})
    r = tester.verify(str(demo_copy), PatchProposal(diff=diff), base)
    assert r.exit_code == 0, r.log
    assert not r.passed
    assert r.missing_ids and all(i.startswith("tests/test_reports.py::") for i in r.missing_ids)
    assert "did not pass after the patch" in r.log


def test_parse_outcomes_reads_rA_summary():
    out = ("=== short test summary info ===\n"
           "PASSED tests/test_a.py::test_one\n"
           "PASSED tests/test_a.py::test_p[a b]\n"
           "FAILED tests/test_a.py::test_two - AssertionError: boom\n"
           "SKIPPED [1] tests/test_a.py:9: no db\n")
    assert parse_outcomes(out) == {"tests/test_a.py::test_one": "PASSED", "tests/test_a.py::test_p[a b]": "PASSED",
                                   "tests/test_a.py::test_two": "FAILED"}
