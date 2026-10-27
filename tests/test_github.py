"""GitHub layer without network: comment placement, auth interface, publish safety."""
import pytest

from prbot.github.auth import GitHubAuthError, TokenAuth, get_auth
from prbot.github.client import GitHubPublisher, split_comments
from prbot.models import ChangedFile, Finding, ReviewState


def F(line, file="app/x.py"):
    return Finding(file=file, line=line, severity="high", category="security", title="t", rationale="r")


def test_split_comments_inline_only_on_diff_lines():
    cf = ChangedFile(path="app/x.py", patch="@@ -1,3 +1,4 @@\n a\n+b\n c\n d\n")
    inline, body = split_comments([F(2), F(50), F(1, file="other.py")], [cf])
    assert [c["line"] for c in inline] == [2]
    assert inline[0]["side"] == "RIGHT" and inline[0]["path"] == "app/x.py"
    assert [f.line for f in body] == [50, 1]


def test_token_auth_requires_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(GitHubAuthError):
        TokenAuth()
    assert TokenAuth("abc").token() == "abc"


def test_unknown_auth_kind():
    with pytest.raises(GitHubAuthError):
        get_auth("magic")


class FakePR:
    def __init__(self):
        self.calls = []

    def post_review(self, state):
        self.calls.append("review")
        return "review-url"

    def open_fix_pr(self, state, repo_path):
        self.calls.append("fix")
        return "pr-url"


def test_publisher_writes_nothing_without_opt_in():
    fake = FakePR()
    GitHubPublisher(fake).publish(ReviewState(repo_path=".", findings=[F(1)], status="patched"))
    assert fake.calls == []


def test_publisher_never_pushes_unverified_fix():
    fake = FakePR()
    GitHubPublisher(fake, post_review=True, open_fix_pr=True).publish(
        ReviewState(repo_path=".", findings=[F(1)], status="failed"))
    assert fake.calls == ["review"]
