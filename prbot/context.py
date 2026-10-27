"""Parse a dynamic codebase into review context: diffs, changed regions and surrounding code."""
from __future__ import annotations

import ast
from pathlib import Path

from prbot.models import ChangedFile
from prbot.patching import added_lines_from_patch, parse_unified_diff
from prbot.workspace import Workspace

MAX_CONTEXT_CHARS = 60_000


def changed_files_from_diff(diff_text: str) -> list[ChangedFile]:
    """Build ChangedFile entries (with per-file hunk text) from a full unified diff."""
    out: list[ChangedFile] = []
    chunks = _split_diff_by_file(diff_text)
    for fp, chunk in zip(parse_unified_diff(diff_text), chunks):
        status = "added" if fp.is_new else "removed" if fp.is_delete else "modified"
        hunk_text = "\n".join(l for l in chunk.split("\n") if not l.startswith(("--- ", "+++ ", "diff --git", "index ")))
        out.append(ChangedFile(path=fp.path, status=status, patch=hunk_text, changed_lines=added_lines_from_patch(hunk_text)))
    return out


def _split_diff_by_file(diff_text: str) -> list[str]:
    lines = diff_text.split("\n")
    starts = [i for i in range(len(lines) - 1) if lines[i].startswith("--- ") and lines[i + 1].startswith("+++ ")]
    chunks = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        chunks.append("\n".join(lines[s:e]))
    return chunks


def changed_files_from_tree(ws: Workspace, include_tests: bool = False) -> list[ChangedFile]:
    """Dry-run fallback with no diff: treat every source file as fully changed."""
    files = []
    for rel in ws.iter_files({".py"}):
        if not include_tests and (rel.startswith("tests/") or Path(rel).name.startswith("test_") or Path(rel).name == "conftest.py"):
            continue
        n = len(ws.read_text(rel).splitlines())
        files.append(ChangedFile(path=rel, status="added", patch=None, changed_lines=list(range(1, n + 1))))
    return files


def _enclosing_symbols(source: str, lines: set[int]) -> list[str]:
    """Names of functions/classes that contain changed lines (helps the model reason about scope)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if any(node.lineno <= ln <= end for ln in lines):
                names.append(f"{type(node).__name__.replace('Def', '').lower()} {node.name} (L{node.lineno}-{end})")
    return names


def _regions(lines: list[int], window: int, total: int) -> list[tuple[int, int]]:
    if not lines:
        return []
    regions: list[list[int]] = []
    for ln in sorted(set(lines)):
        s, e = max(1, ln - window), min(total, ln + window)
        if regions and s <= regions[-1][1] + 1:
            regions[-1][1] = max(regions[-1][1], e)
        else:
            regions.append([s, e])
    return [(s, e) for s, e in regions]


_QUERY_CALLS = {"execute", "executemany", "fetchone", "fetchall", "query", "get", "filter", "all", "first", "scalar", "read", "open", "request", "urlopen"}


def performance_hotspots(source: str, lines: set[int] | None = None) -> list[str]:
    """Deterministic AST hints for the reviewer: I/O or queries inside loops (N+1), and nested loops
    over the same iterable (accidental O(n^2)). Hints are leads, not verdicts; the model decides."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hints: list[str] = []

    def touched(node) -> bool:
        if lines is None:
            return True
        end = getattr(node, "end_lineno", node.lineno)
        return any(node.lineno <= ln <= end for ln in lines)

    def iter_name(loop) -> str | None:
        it = loop.iter
        if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id in {"enumerate", "range", "len", "list", "sorted"} and it.args:
            it = it.args[0]
            if isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == "len" and it.args:
                it = it.args[0]
        return ast.unparse(it) if isinstance(it, (ast.Name, ast.Attribute)) else None

    for loop in ast.walk(tree):
        if not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)) or not touched(loop):
            continue
        for node in ast.walk(loop):
            if node is loop:
                continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "executemany", "query", "urlopen", "request"}:
                hints.append(f"L{node.lineno}: `{node.func.attr}(...)` call inside the loop at L{loop.lineno} (possible N+1 / repeated I/O)")
            if isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(loop, (ast.For, ast.AsyncFor)):
                a, b = iter_name(loop), iter_name(node)
                if a and a == b:
                    hints.append(f"L{node.lineno}: nested loop over `{b}` inside the loop over `{a}` at L{loop.lineno} (possible O(n^2))")
            if isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                for c in node.comparators:
                    if isinstance(c, ast.Name):
                        hints.append(f"L{node.lineno}: `in {c.id}` membership test inside the loop at L{loop.lineno} (O(n) per check if `{c.id}` is a list)")
    seen, out = set(), []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def build_review_context(ws: Workspace, changed: list[ChangedFile], window: int = 12) -> str:
    """Markdown context for the reviewer: per file, the diff hunks plus numbered surrounding code."""
    parts: list[str] = []
    for cf in changed:
        parts.append(f"## {cf.path} ({cf.status})")
        if cf.status == "removed" or not ws.exists(cf.path):
            parts.append("(file removed or unavailable)")
            continue
        source = ws.read_text(cf.path)
        total = len(source.splitlines())
        syms = _enclosing_symbols(source, set(cf.changed_lines))
        if syms:
            parts.append("Touched symbols: " + ", ".join(syms))
        hot = performance_hotspots(source, set(cf.changed_lines)) if cf.path.endswith(".py") else []
        if hot:
            parts.append("Static hotspot hints (verify each):\n" + "\n".join(f"- {h}" for h in hot))
        if cf.patch:
            parts.append("### Diff\n```diff\n" + cf.patch.strip() + "\n```")
        parts.append("### Code (new version, numbered; changed lines marked with >)")
        changed_set = set(cf.changed_lines)
        for s, e in _regions(cf.changed_lines, window, total) or [(1, min(total, 2 * window))]:
            src_lines = source.splitlines()
            block = "\n".join(
                f"{'>' if i in changed_set else ' '}{i:>4} | {src_lines[i - 1]}" for i in range(s, e + 1)
            )
            parts.append(f"```python\n{block}\n```")
    text = "\n\n".join(parts)
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n\n[context truncated]"
    return text
