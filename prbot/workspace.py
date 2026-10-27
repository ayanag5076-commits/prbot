"""Sandboxed, read-only view of a repository checkout used by the agents' tools."""
from __future__ import annotations

import re
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build"}
TEXT_SUFFIXES = {".py", ".txt", ".md", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".sql", ".js", ".ts"}


class WorkspaceError(Exception):
    pass


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"Not a directory: {root}")

    def safe_path(self, rel: str) -> Path:
        rel = rel.strip().lstrip("/")
        if rel.startswith(("a/", "b/")) and not (self.root / rel).exists():
            rel = rel[2:]
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise WorkspaceError(f"Path escapes the repository: {rel}")
        return p

    def exists(self, rel: str) -> bool:
        try:
            return self.safe_path(rel).is_file()
        except WorkspaceError:
            return False

    def read_text(self, rel: str) -> str:
        p = self.safe_path(rel)
        if not p.is_file():
            raise WorkspaceError(f"File not found: {rel}")
        return p.read_text(errors="replace")

    def iter_files(self, suffixes: set[str] | None = None):
        suffixes = suffixes or TEXT_SUFFIXES
        for p in sorted(self.root.rglob("*")):
            if any(part in SKIP_DIRS for part in p.relative_to(self.root).parts):
                continue
            if p.is_file() and p.suffix in suffixes:
                yield p.relative_to(self.root).as_posix()

    def numbered(self, rel: str, start: int = 1, end: int | None = None) -> str:
        lines = self.read_text(rel).splitlines()
        start = max(1, start)
        end = min(len(lines), end or len(lines))
        width = len(str(end))
        return "\n".join(f"{i:>{width}} | {lines[i - 1]}" for i in range(start, end + 1))

    def search(self, pattern: str, glob: str | None = None, max_results: int = 50) -> list[tuple[str, int, str]]:
        try:
            rx = re.compile(pattern)
        except re.error:
            rx = re.compile(re.escape(pattern))
        hits: list[tuple[str, int, str]] = []
        for rel in self.iter_files():
            if glob and not Path(rel).match(glob):
                continue
            for n, line in enumerate(self.read_text(rel).splitlines(), start=1):
                if rx.search(line):
                    hits.append((rel, n, line.strip()))
                    if len(hits) >= max_results:
                        return hits
        return hits
