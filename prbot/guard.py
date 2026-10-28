"""Protects the verification contract from the repair loop.

A green test run only proves the patch is correct if the tests, fixtures and collection settings are the
ones the PR author wrote. So an automatic fix may never touch them, and the patched run must still pass
every test that passed on the unpatched baseline (not just exit 0).
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from prbot.patching import PatchError, parse_unified_diff

# Files that decide WHAT pytest collects and HOW it runs (plugins, markers, addopts, testpaths, rootdir).
COLLECTION_FILES = {
    "conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py",
    ".coveragerc", "sitecustomize.py", "usercustomize.py",
}
TEST_DIRS = {"test", "tests", "testing"}


def protected_reason(path: str) -> str:
    """Why a repo-relative path is part of the verification contract, or "" if a patch may edit it."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    p = PurePosixPath(norm.lstrip("/"))
    name = p.name.lower()
    if name in COLLECTION_FILES:
        return "pytest configuration / collection setting"
    if name.endswith(".pth"):
        return "Python startup hook (.pth)"
    if any(part.lower() in TEST_DIRS for part in p.parts[:-1]):
        return "file in a test directory"
    if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
        return "test module"
    return ""


def protected_paths_in_diff(diff: str) -> dict[str, str]:
    """Every protected path a diff creates, edits, deletes or renames (old AND new side), with the reason."""
    try:
        files = parse_unified_diff(diff)
    except PatchError:
        return {}
    hits: dict[str, str] = {}
    for fp in files:
        for path in {fp.old_path, fp.new_path} - {None}:
            reason = protected_reason(path)  # type: ignore[arg-type]
            if reason:
                hits[path] = reason  # type: ignore[index]
    return hits


def protected_message(hits: dict[str, str]) -> str:
    listed = "; ".join(f"{p} ({r})" for p, r in sorted(hits.items()))
    return (f"patch touches the verification contract: {listed}. Automatic fixes may only change application "
            "code. Tests, conftest.py and pytest/collection settings are read-only, so fix the source instead.")


# ------------------------------------------------------------------ test-ID bookkeeping (pytest -rA summary)
_SUMMARY_RE = re.compile(r"^(PASSED|XPASS|FAILED|ERROR|XFAIL) (.+?)(?: - .*)?$")


def parse_outcomes(output: str) -> dict[str, str]:
    """{node_id: outcome} from the 'short test summary info' section that `pytest -rA` prints."""
    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        m = _SUMMARY_RE.match(line.strip())
        if m and "::" in m.group(2):
            outcomes[m.group(2).strip()] = m.group(1)
    return outcomes


def passed_ids(output: str) -> list[str]:
    return sorted(i for i, o in parse_outcomes(output).items() if o == "PASSED")
