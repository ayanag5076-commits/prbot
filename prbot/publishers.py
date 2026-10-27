"""Where finalized results go. The finalize node calls publisher.publish(state)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from prbot.models import ReviewState


class Publisher(Protocol):
    def publish(self, state: ReviewState) -> list[str]: ...


class LocalPublisher:
    """Writes report.md, findings.json and (if verified) fix.patch to an output folder."""

    def __init__(self, out_dir: str | Path):
        self.out_dir = Path(out_dir)

    def publish(self, state: ReviewState) -> list[str]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        (self.out_dir / "report.md").write_text(state.report)
        written.append(str(self.out_dir / "report.md"))
        (self.out_dir / "findings.json").write_text(json.dumps([f.model_dump() for f in state.findings], indent=2))
        written.append(str(self.out_dir / "findings.json"))
        if state.patch is not None:
            name = "fix.patch" if state.status == "patched" else "unverified.patch"
            (self.out_dir / name).write_text(state.patch.diff)
            written.append(str(self.out_dir / name))
        return written


class CompositePublisher:
    def __init__(self, *publishers: Publisher):
        self.publishers = publishers

    def publish(self, state: ReviewState) -> list[str]:
        out: list[str] = []
        for p in self.publishers:
            out += p.publish(state)
        return out
