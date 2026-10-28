"""Agent C: applies the patch in a throwaway checkout and runs pytest in a locked-down Docker container."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Protocol

from prbot.models import PatchProposal, TestResult
from prbot.guard import passed_ids, protected_message, protected_paths_in_diff
from prbot.patching import PatchError, apply_patch_to_dir
from prbot.workspace import SKIP_DIRS

LOG_TAIL_CHARS = 6000


def tail(text: str, n: int = LOG_TAIL_CHARS) -> str:
    return text if len(text) <= n else "...[truncated]...\n" + text[-n:]


class Runner(Protocol):
    name: str

    def run(self, workdir: Path, timeout: int) -> tuple[int, str, bool]: ...


class DockerRunner:
    """docker run --network none, read-only rootfs, dropped caps, CPU/mem/pids limits and a hard timeout."""

    name = "docker"

    def __init__(self, image: str, command: list[str] | None = None):
        self.image = image
        self.command = command or ["python", "-m", "pytest", "-p", "no:cacheprovider", "-rA"]

    def build_cmd(self, workdir: Path, container: str) -> list[str]:
        return [
            "docker", "run", "--rm", "--name", container,
            "--network", "none",
            "--cpus", "1", "--memory", "512m", "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{workdir}:/workspace:ro", "-w", "/workspace",
            self.image, *self.command,
        ]

    def run(self, workdir: Path, timeout: int) -> tuple[int, str, bool]:
        if shutil.which("docker") is None:
            return 127, "docker CLI not found. Install Docker or run with --no-docker (unsafe: runs tests on host).", False
        container = f"prbot-test-{uuid.uuid4().hex[:10]}"
        try:
            proc = subprocess.run(self.build_cmd(workdir, container), capture_output=True, text=True, timeout=timeout)
            return proc.returncode, proc.stdout + proc.stderr, False
        except subprocess.TimeoutExpired as e:
            subprocess.run(["docker", "kill", container], capture_output=True)
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 124, out + f"\nTIMEOUT: tests exceeded {timeout}s and the container was killed.", True


class LocalRunner:
    """Runs pytest on the host. No isolation: for CI/sandboxes without Docker and for offline tests."""

    name = "local"

    def run(self, workdir: Path, timeout: int) -> tuple[int, str, bool]:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA"],
                                  cwd=workdir, capture_output=True, text=True, timeout=timeout, env=env)
            return proc.returncode, proc.stdout + proc.stderr, False
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return 124, out + f"\nTIMEOUT after {timeout}s.", True


def _copy_repo(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    for root, dirs, files in os.walk(dst):  # readable by the container user even with --cap-drop ALL
        os.chmod(root, 0o755)
        for f in files:
            os.chmod(os.path.join(root, f), 0o644)


class Tester:
    __test__ = False  # not a pytest test class

    def __init__(self, runner: Runner, timeout: int = 120):
        self.runner = runner
        self.timeout = timeout

    def baseline(self, repo_path: str) -> TestResult:
        """Run the suite on unpatched code. A failure here means the patch loop can't succeed."""
        with tempfile.TemporaryDirectory(prefix="prbot-base-") as tmp:
            work = Path(tmp) / "repo"
            _copy_repo(Path(repo_path), work)
            t0 = time.monotonic()
            code, out, timed_out = self.runner.run(work, self.timeout)
            # pytest exit 5 = no tests collected: nothing to protect, but nothing broken either
            return TestResult(passed=code in (0, 5), exit_code=code, log=tail(out), runner=self.runner.name,
                              duration_s=round(time.monotonic() - t0, 2), timed_out=timed_out,
                              passed_ids=passed_ids(out))

    def verify(self, repo_path: str, patch: PatchProposal | None, baseline: TestResult | None = None) -> TestResult:
        """Apply the patch in a throwaway copy and run the suite. Passing means: the patch leaves tests and
        collection settings untouched, pytest exits 0, AND every test that passed on the baseline still passes."""
        if patch is None:
            return TestResult(passed=False, exit_code=-1, runner=self.runner.name,
                              log="The patcher did not submit a valid patch (write_patch never succeeded).")
        guarded = protected_paths_in_diff(patch.diff)
        if guarded:  # defence in depth: write_patch already refuses these, but never run a rewritten contract
            return TestResult(passed=False, exit_code=-1, runner=self.runner.name, protected_paths=sorted(guarded),
                              log=f"Patch not run: {protected_message(guarded)}")
        with tempfile.TemporaryDirectory(prefix="prbot-") as tmp:
            work = Path(tmp) / "repo"
            _copy_repo(Path(repo_path), work)
            try:
                apply_patch_to_dir(work, patch.diff)
            except PatchError as e:
                return TestResult(passed=False, exit_code=-1, runner=self.runner.name, log=f"Patch failed to apply: {e}")
            t0 = time.monotonic()
            code, out, timed_out = self.runner.run(work, self.timeout)
            ids = passed_ids(out)
            missing = sorted(set(baseline.passed_ids) - set(ids)) if baseline is not None else []
            log = tail(out)
            if code == 0 and missing:
                shown = "\n".join(f"  - {i}" for i in missing[:20]) + ("\n  ..." if len(missing) > 20 else "")
                log += (f"\n\nprbot: pytest exited 0 but {len(missing)} test(s) that passed on the unpatched baseline "
                        f"did not pass after the patch (deselected, skipped, removed or no longer collected):\n{shown}")
            return TestResult(passed=(code == 0 and not missing), exit_code=code, log=log, runner=self.runner.name,
                              duration_s=round(time.monotonic() - t0, 2), timed_out=timed_out,
                              passed_ids=ids, missing_ids=missing)
