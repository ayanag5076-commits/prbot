"""A forgiving unified-diff parser/applier.

LLM-written diffs often get hunk headers (line counts, offsets) slightly wrong. This
applier ignores the counts and locates each hunk by its context lines, searching
outward from the expected position, so a diff with correct *content* still applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


@dataclass
class Hunk:
    old_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (tag, text) tag in ' ', '-', '+'

    @property
    def old_block(self) -> list[str]:
        return [t for tag, t in self.lines if tag in " -"]

    @property
    def new_block(self) -> list[str]:
        return [t for tag, t in self.lines if tag in " +"]


@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return (self.new_path or self.old_path)  # type: ignore[return-value]

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_delete(self) -> bool:
        return self.new_path is None


def _clean_path(raw: str) -> str | None:
    raw = raw.strip().split("\t")[0]
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    return raw


def parse_unified_diff(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        # A file header is a '--- ' line immediately followed by a '+++ ' line.
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            current = FilePatch(old_path=_clean_path(line[4:]), new_path=_clean_path(lines[i + 1][4:]))
            files.append(current)
            hunk = None
            i += 2
            continue
        if line.startswith(("diff --git", "index ", "new file mode", "deleted file mode", "similarity index", "rename ")):
            hunk = None
            i += 1
            continue
        m = HUNK_RE.match(line)
        if m and current is not None:
            hunk = Hunk(old_start=int(m.group(1)))
            current.hunks.append(hunk)
        elif hunk is not None:
            if line.startswith("\\"):  # "\ No newline at end of file"
                pass
            elif line == "":
                hunk.lines.append((" ", ""))  # LLMs often drop the leading space on blank context
            elif line[0] in " +-":
                hunk.lines.append((line[0], line[1:]))
            else:
                hunk = None
        i += 1
    for fp in files:  # drop trailing blank context produced by the diff's final newline
        for h in fp.hunks:
            while h.lines and h.lines[-1] == (" ", ""):
                h.lines.pop()
    if not files:
        raise PatchError("No file headers ('--- a/...' followed by '+++ b/...') found in diff.")
    return files


def _find_block(haystack: list[str], needle: list[str], expected: int) -> int:
    if not needle:
        return max(0, min(expected, len(haystack)))
    n = len(needle)
    candidates = range(0, len(haystack) - n + 1)
    for norm in (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip()):
        nd = [norm(x) for x in needle]
        best = None
        for pos in sorted(candidates, key=lambda p: abs(p - expected)):
            if [norm(x) for x in haystack[pos:pos + n]] == nd:
                best = pos
                break
        if best is not None:
            return best
    return -1


def apply_file_patch(original: str, fp: FilePatch) -> str:
    trailing_nl = original.endswith("\n") or original == ""
    lines = original.split("\n")
    if original.endswith("\n"):
        lines = lines[:-1]
    if original == "":
        lines = []
    shift = 0
    for i, h in enumerate(fp.hunks, start=1):
        expected = max(0, h.old_start - 1 + shift)
        pos = _find_block(lines, h.old_block, expected)
        if pos < 0:
            preview = "\n".join(h.old_block[:6])
            raise PatchError(
                f"{fp.path}: hunk #{i} does not match the file. Context lines expected near line "
                f"{h.old_start}:\n{preview}\nRe-read the file with read_file and copy context lines exactly."
            )
        lines[pos:pos + len(h.old_block)] = h.new_block
        shift = pos - (h.old_start - 1) + len(h.new_block) - len(h.old_block)
    out = "\n".join(lines)
    if trailing_nl and lines:
        out += "\n"
    return out


def _safe(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if root.resolve() not in p.parents and p != root.resolve():
        raise PatchError(f"Path escapes repository root: {rel}")
    return p


def apply_patch_in_memory(root: str | Path, diff: str) -> dict[str, str | None]:
    """Return {path: new_content or None (deleted)} without touching disk."""
    root = Path(root)
    result: dict[str, str | None] = {}
    for fp in parse_unified_diff(diff):
        target = _safe(root, fp.path)
        if fp.is_delete:
            result[fp.path] = None
            continue
        if fp.is_new:
            original = ""
        else:
            src = _safe(root, fp.old_path or fp.path)
            if not src.exists():
                raise PatchError(f"{fp.old_path}: file does not exist in the repository.")
            original = result.get(fp.path) or src.read_text()
        if not fp.hunks:
            raise PatchError(f"{fp.path}: no hunks ('@@ ... @@') in diff.")
        result[fp.path] = apply_file_patch(original, fp)
        _ = target
    return result


def apply_patch_to_dir(root: str | Path, diff: str) -> list[str]:
    root = Path(root)
    changes = apply_patch_in_memory(root, diff)
    for rel, content in changes.items():
        p = _safe(root, rel)
        if content is None:
            if p.exists():
                p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return list(changes)


def added_lines_from_patch(patch: str | None) -> list[int]:
    """New-side line numbers that a GitHub-style hunk patch adds or modifies."""
    if not patch:
        return []
    out: list[int] = []
    new_line = 0
    in_hunk = False
    for line in patch.split("\n"):
        m = HUNK_RE.match(line)
        if m:
            new_line = int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk or line == "" or line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.append(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return out


def commentable_lines_from_patch(patch: str | None) -> set[int]:
    """New-side lines visible in the diff (added or context): GitHub accepts review comments only there."""
    if not patch:
        return set()
    out: set[int] = set()
    new_line = 0
    in_hunk = False
    for line in patch.split("\n"):
        m = HUNK_RE.match(line)
        if m:
            new_line = int(m.group(3))
            in_hunk = True
            continue
        if not in_hunk or line == "" or line.startswith("\\"):
            continue
        if line.startswith("-") and not line.startswith("---"):
            continue
        out.add(new_line)
        new_line += 1
    return out
