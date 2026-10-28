"""ReAct tools for the patcher (Agent B): read_file, search_code, write_patch."""
from __future__ import annotations

import ast

from dataclasses import dataclass, field
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from prbot.guard import protected_message, protected_paths_in_diff
from prbot.models import PatchProposal
from prbot.patching import PatchError, apply_patch_in_memory
from prbot.workspace import Workspace, WorkspaceError


@dataclass
class PatchHolder:
    """Collects the validated patch submitted via write_patch."""

    patch: Optional[PatchProposal] = None
    rejected: list[str] = field(default_factory=list)


class ReadFileArgs(BaseModel):
    path: str = Field(description="Repo-relative file path.")
    start_line: int = Field(default=1, ge=1)
    end_line: Optional[int] = Field(default=None, description="Inclusive; omit for end of file.")


class SearchCodeArgs(BaseModel):
    pattern: str = Field(description="Python regex (falls back to literal text if invalid).")
    glob: Optional[str] = Field(default=None, description="Optional path glob, e.g. '*.py' or 'app/*.py'.")


class WritePatchArgs(BaseModel):
    diff: str = Field(
        description=(
            "A complete unified diff against the repo root, git style: '--- a/path' / '+++ b/path' headers, "
            "'@@ -l,s +l,s @@' hunks, and at least 3 lines of exact, unchanged context around each change."
        )
    )
    explanation: str = Field(description="One or two sentences on what the patch fixes.")


def normalize_diff(diff: str) -> str:
    """Compare diffs by their +/- content only, ignoring headers, hunk offsets and whitespace."""
    keep = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if line[:1] in "+-" and line[1:].strip():
            keep.append(line[0] + " ".join(line[1:].split()))
    return "\n".join(keep)


def make_tools(ws: Workspace, holder: PatchHolder, failed_patches: list[str] | None = None) -> list[StructuredTool]:
    failed_norm = [normalize_diff(d) for d in (failed_patches or [])]

    def read_file(path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        try:
            return ws.numbered(path, start_line, end_line)
        except WorkspaceError as e:
            return f"ERROR: {e}"

    def search_code(pattern: str, glob: Optional[str] = None) -> str:
        hits = ws.search(pattern, glob)
        if not hits:
            return "No matches."
        return "\n".join(f"{p}:{n}: {line}" for p, n, line in hits)

    def write_patch(diff: str, explanation: str) -> str:
        guarded = protected_paths_in_diff(diff)
        if guarded:  # tests, conftest.py and pytest settings are the contract the fix is judged against
            holder.rejected.append(f"protected paths: {', '.join(sorted(guarded))}")
            return f"ERROR: patch rejected. {protected_message(guarded)}"
        try:
            changes = apply_patch_in_memory(ws.root, diff)
        except (PatchError, WorkspaceError) as e:
            holder.rejected.append(str(e))
            return f"ERROR: patch rejected. {e}"
        norm = normalize_diff(diff)
        if norm in failed_norm:
            n = failed_norm.index(norm) + 1
            holder.rejected.append(f"repeat of failed attempt {n}")
            return (f"ERROR: this patch is identical to failed attempt {n}, which already failed the test suite. "
                    "Pivot to a structurally different solution.")
        for rel, content in changes.items():
            if content is not None and rel.endswith(".py"):
                try:
                    ast.parse(content, filename=rel)  # AST pre-validation before any container run
                except SyntaxError as e:
                    holder.rejected.append(str(e))
                    return f"ERROR: patch applies but {rel} has a SyntaxError at line {e.lineno}: {e.msg}"
        holder.patch = PatchProposal(diff=diff if diff.endswith("\n") else diff + "\n", explanation=explanation, files=list(changes))
        return f"OK: patch validated (applies cleanly, AST parses). Files: {', '.join(changes)}. You are done."

    return [
        StructuredTool.from_function(read_file, name="read_file", args_schema=ReadFileArgs,
                                     description="Read a file from the repo with line numbers."),
        StructuredTool.from_function(search_code, name="search_code", args_schema=SearchCodeArgs,
                                     description="Regex search across the repo; returns path:line: text."),
        StructuredTool.from_function(write_patch, name="write_patch", args_schema=WritePatchArgs,
                                     description="Submit the fix as a unified diff. It is validated (applies, compiles, leaves tests/conftest/pytest config untouched) and the result is returned."),
    ]
