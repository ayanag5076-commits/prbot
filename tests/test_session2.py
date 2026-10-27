"""Session 2: baseline gate, GitHub App least-privilege auth, metrics, and a secret-leak guard."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from prbot.agents.patcher import ReActPatcher
from prbot.agents.tester import LocalRunner, Tester
from prbot.context import changed_files_from_diff
from prbot.github.auth import (AnonymousAuth, GitHubAppAuth, GitHubAuthError, get_auth, missing_permissions,
                               required_permissions)
from prbot.github.client import GitHubPublisher
from prbot.graph import ABORT_MAIN_FAILING, ABORT_PR_FAILING, Agents, abort_reason_for, route_after_baseline, run_review
from prbot.metrics import (RunRecord, SandboxRun, TokenUsage, cost_usd, pytest_seconds, summarize, to_markdown,
                           usage_from_message)
from prbot.models import ReviewState, TestResult
from prbot.offline import HeuristicReviewer, ScriptedChatModel, demo_scripts
from prbot.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ baseline gate
def test_abort_reason_attribution():
    assert abort_reason_for(True, None) == ""
    assert abort_reason_for(False, False) == ABORT_MAIN_FAILING
    assert abort_reason_for(False, True) == ABORT_PR_FAILING
    assert route_after_baseline(ReviewState(repo_path=".", abort_reason="x")) == "finalize"
    assert route_after_baseline(ReviewState(repo_path=".")) == "reviewer"


class Exploding:
    def review(self, *a):
        raise AssertionError("must not run after a failed baseline")

    def propose(self, *a):
        raise AssertionError("must not run after a failed baseline")


def test_failing_main_aborts_before_review(demo_copy, tmp_path):
    (demo_copy / "tests" / "test_broken.py").write_text("def test_already_broken():\n    assert 1 == 2\n")
    base = tmp_path / "base"
    import shutil
    shutil.copytree(demo_copy, base)
    changed = changed_files_from_diff((demo_copy / "pr.diff").read_text())
    final = run_review(Agents(Exploding(), Exploding(), Tester(LocalRunner(), 60)),
                       ReviewState(repo_path=str(demo_copy), base_path=str(base), changed_files=changed))
    assert final.status == "aborted" and final.abort_reason == ABORT_MAIN_FAILING
    assert final.attempts == 0 and final.findings == []
    assert "Aborted" in final.report


class FakePR:
    def __init__(self):
        self.calls = []

    def post_abort_comment(self, state):
        self.calls.append(("abort", state.abort_reason))
        return "comment-url"

    def post_review(self, state):
        self.calls.append(("review",))

    def open_fix_pr(self, state, path):
        self.calls.append(("fix",))


def test_abort_posts_top_level_comment_only():
    fake = FakePR()
    st = ReviewState(repo_path=".", status="aborted", abort_reason=ABORT_MAIN_FAILING)
    assert GitHubPublisher(fake, post_review=True, open_fix_pr=True).publish(st) == ["comment-url"]
    assert fake.calls == [("abort", ABORT_MAIN_FAILING)]


# ------------------------------------------------------------------ GitHub App auth
class FakeAccess:
    def __init__(self, perms):
        self.token = "ghs_installation_token"
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        self.permissions = perms


class FakeIntegration:
    def __init__(self, granted):
        self.granted, self.requests = granted, []

    def get_repo_installation(self, owner, repo):
        return type("I", (), {"id": 42})()

    def get_access_token(self, installation_id, permissions=None):
        self.requests.append((installation_id, permissions))
        return FakeAccess({k: v for k, v in self.granted.items() if k in (permissions or {})} or self.granted)


def test_app_auth_requests_least_privilege_for_review():
    integ = FakeIntegration({"contents": "read", "pull_requests": "write", "metadata": "read"})
    auth = GitHubAppAuth(repo="o/r", app_id="1", integration=integ)
    assert auth.token() == "ghs_installation_token"
    inst, perms = integ.requests[0]
    assert inst == 42 and perms == {"contents": "read", "pull_requests": "write", "metadata": "read"}
    auth.token()
    assert len(integ.requests) == 1  # cached until near expiry


def test_app_auth_fix_pr_needs_contents_write():
    integ = FakeIntegration({"contents": "read", "pull_requests": "write", "metadata": "read"})
    auth = GitHubAppAuth(repo="o/r", app_id="1", allow_push=True, integration=integ)
    with pytest.raises(GitHubAuthError, match="contents"):
        auth.token()


def test_permission_math():
    assert required_permissions(False)["contents"] == "read"
    assert required_permissions(True)["contents"] == "write"
    assert missing_permissions({"contents": "write", "pull_requests": "write", "metadata": "read"}, required_permissions(True)) == {}
    assert missing_permissions({"contents": "read"}, {"contents": "write"}) == {"contents": "write"}


def test_dry_run_without_credentials_is_anonymous(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert isinstance(get_auth("token", read_only=True), AnonymousAuth)
    with pytest.raises(GitHubAuthError):
        get_auth("token")


# ------------------------------------------------------------------ metrics
def test_usage_and_cost():
    msg = AIMessage(content="", usage_metadata={"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
                                                "input_token_details": {"cache_read": 400}})
    assert usage_from_message(msg) == (1000, 400, 200)
    u = TokenUsage(agent="patcher", input_tokens=1000, cached_input_tokens=400, output_tokens=200)
    # 600*0.40 + 400*0.10 + 200*1.60 = 600 per 1M
    assert cost_usd([u], "gpt-4.1-mini") == pytest.approx(0.0006)
    assert cost_usd([u], "some-unknown-model") is None


def test_patcher_accumulates_tokens_per_attempt(demo_copy):
    ws = Workspace(demo_copy)
    script = demo_scripts(ws)[1]
    for m in script:
        m.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    p = ReActPatcher(ScriptedChatModel([script]))
    p.propose(ws, [], [], [])
    assert p.last_usage["input_tokens"] == 200 and p.last_usage["output_tokens"] == 20 and p.last_usage["calls"] == 2


def test_pytest_time_parsing():
    assert pytest_seconds("....\n6 passed in 0.10s\n") == 0.10
    assert pytest_seconds("1 failed, 5 passed in 1.25s") == 1.25
    assert pytest_seconds("no summary") is None


def _rec(status, attempts, first, passed_on, tokens=0, cost=None):
    return RunRecord(target="t", model="m", status=status, findings=1, actionable=1, attempts=attempts,
                     first_attempt_passed=first, passed_on_attempt=passed_on, total_tokens=tokens, cost_usd=cost,
                     sandbox=[SandboxRun(phase="attempt", runner="docker", wall_s=2.0, pytest_s=0.5, overhead_s=1.5, passed=True)])


def test_summary_recovery_rate_and_markdown():
    recs = [_rec("patched", 1, True, 1, 1000, 0.001), _rec("patched", 2, False, 2, 3000, 0.003),
            _rec("patched", 3, False, 3, 5000, 0.005), _rec("failed", 3, False, None, 6000, 0.006),
            RunRecord(target="bench", model="none", status="bench", findings=0, actionable=0, attempts=0)]
    s = summarize(recs)
    assert s["runs"] == 4 and s["repairs_completed"] == 3
    assert s["recovery_rate_pct"] == pytest.approx(66.7)
    assert (s["recovered_on_attempt_2"], s["recovered_on_attempt_3"]) == (1, 1)
    assert s["avg_tokens_per_repair"] == 3000 and s["avg_cost_per_repair_usd"] == pytest.approx(0.003)
    assert s["sandbox"]["docker"]["overhead_median_s"] == 1.5
    md = to_markdown(s)
    assert "Token efficiency" in md and "recovery rate" in md and "Sandbox latency (Docker)" in md


def test_metrics_record_written_by_cli(demo_copy, tmp_path):
    from prbot.cli import main
    ledger = tmp_path / "m.jsonl"
    main(["review", "--dry-run", "--path", str(demo_copy), "--diff", str(demo_copy / "pr.diff"), "--offline",
          "--no-docker", "--out", str(tmp_path / "o"), "--metrics-file", str(ledger)])
    rec = json.loads(ledger.read_text().splitlines()[0])
    assert rec["status"] == "patched" and rec["passed_on_attempt"] == 2 and rec["first_attempt_passed"] is False
    assert any(s["phase"] == "baseline" for s in rec["sandbox"])


# ------------------------------------------------------------------ secret-leak guard
SECRET_PATTERNS = [
    re.compile(r"sk-(proj-)?[A-Za-z0-9_\-]{20,}"),           # OpenAI keys
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),                # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
]


def test_no_secrets_committed():
    offenders = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if any(part in {".git", ".venv", "venv", "__pycache__", ".pytest_cache"} for part in rel.parts):
            continue
        if not p.is_file() or p.suffix in {".pyc", ".zip"} or p.name == "test_session2.py":
            continue
        assert p.name != ".env", ".env must never be in the repo"
        assert p.suffix != ".pem", f"private key file in repo: {rel}"
        text = p.read_text(errors="ignore")
        offenders += [f"{rel}: {rx.pattern}" for rx in SECRET_PATTERNS if rx.search(text)]
    assert not offenders, offenders


def test_gitignore_blocks_secrets():
    gi = (ROOT / ".gitignore").read_text().splitlines()
    for needed in (".env", "*.pem"):
        assert needed in gi


def test_telemetry_table_pending_then_filled(tmp_path):
    from prbot.metrics import to_telemetry_table, update_readme
    host_only = summarize([RunRecord(target="t", model="m", status="patched", findings=3, actionable=3, attempts=2,
                                     first_attempt_passed=False, passed_on_attempt=2, wall_s=3.0,
                                     sandbox=[SandboxRun(phase="attempt", runner="local", wall_s=1.0, passed=True)])])
    t = to_telemetry_table(host_only)
    assert "**2 Attempts**" in t and "[Pending Runtime Log] | Awaiting Local Docker Run" in t
    docker = summarize([_rec("patched", 2, False, 2).model_copy(update={"wall_s": w}) for w in (10, 20, 30, 40, 50)])
    t = to_telemetry_table(docker)
    assert "**46.0s** (5 Docker runs)" in t or "**50" in t or "Over target" in t or "Met" in t
    readme = tmp_path / "R.md"
    readme.write_text("x\n<!-- METRICS:START -->\nold\n<!-- METRICS:END -->\ny\n")
    assert update_readme(readme, t)
    body = readme.read_text()
    assert "old" not in body and "P90 Execution Latency" in body and body.endswith("y\n")
