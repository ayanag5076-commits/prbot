"""PyGithub layer: fetch a PR and its code, post review comments, push a verified fix as a follow-up PR."""
from __future__ import annotations

import io
import tarfile
import tempfile
from pathlib import Path

import requests

from prbot.github.auth import GitHubAuthProvider
from prbot.models import ChangedFile, Finding, PRMeta, ReviewState
from prbot.patching import added_lines_from_patch, apply_patch_in_memory, commentable_lines_from_patch
from prbot.report import finding_comment


def split_comments(findings: list[Finding], changed: list[ChangedFile]) -> tuple[list[dict], list[Finding]]:
    """Inline comments GitHub will accept (line visible in the diff) vs findings that go in the review body."""
    visible = {c.path: commentable_lines_from_patch(c.patch) for c in changed}
    inline, body_only = [], []
    for f in findings:
        if f.line in visible.get(f.file, set()):
            c = {"path": f.file, "line": f.line, "side": "RIGHT", "body": finding_comment(f)}
            if f.end_line and f.end_line > f.line and f.end_line in visible[f.file]:
                c = {**c, "start_line": f.line, "line": f.end_line, "start_side": "RIGHT"}
            inline.append(c)
        else:
            body_only.append(f)
    return inline, body_only


class GitHubPR:
    def __init__(self, auth: GitHubAuthProvider, repo: str, number: int):
        self.auth = auth
        self.gh = auth.client()
        self.repo = self.gh.get_repo(repo)
        self.pr = self.repo.get_pull(number)

    # --------------------------------------------------------------- read
    def meta(self) -> PRMeta:
        pr = self.pr
        return PRMeta(
            repo=self.repo.full_name, number=pr.number, title=pr.title, head_ref=pr.head.ref,
            head_sha=pr.head.sha, base_ref=pr.base.ref, url=pr.html_url,
            same_repo=(pr.head.repo is not None and pr.head.repo.full_name == self.repo.full_name),
        )

    def changed_files(self) -> list[ChangedFile]:
        out = []
        for f in self.pr.get_files():
            out.append(ChangedFile(path=f.filename, status=f.status, patch=f.patch,
                                   changed_lines=added_lines_from_patch(f.patch)))
        return out

    def checkout(self, dest: str | Path | None = None, ref: str | None = None) -> Path:
        """Download a tarball of the PR head (or any ref, e.g. the base sha) and extract it. No git needed."""
        dest = Path(dest or tempfile.mkdtemp(prefix="prbot-pr-"))
        url = self.repo.get_archive_link("tarball", ref=ref or self.pr.head.sha)
        token = self.auth.token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get(url, headers=headers, timeout=60)
        resp.raise_for_status()
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
            members = tar.getmembers()
            prefix = members[0].name.split("/")[0] + "/" if members else ""
            for m in members:
                if not m.name.startswith(prefix) or m.name == prefix.rstrip("/"):
                    continue
                m.name = m.name[len(prefix):]
                if m.name:
                    tar.extract(m, dest, filter="data")
        return dest

    # --------------------------------------------------------------- write
    def checkout_base(self) -> Path:
        return self.checkout(ref=self.pr.base.sha)

    def post_abort_comment(self, state: ReviewState) -> str:
        """Top-level PR comment (issue comment), not a review: nothing was reviewed."""
        body = f"### prbot\n\n⛔ **{state.abort_reason}**\n\nNo review or patch was attempted. Fix the failing tests and re-run."
        tail_log = (state.base_baseline or state.baseline)
        if tail_log is not None and tail_log.log:
            body += "\n\n<details><summary>Test output (tail)</summary>\n\n```\n" + tail_log.log.strip()[-1500:] + "\n```\n</details>"
        comment = self.pr.create_issue_comment(body)
        return getattr(comment, "html_url", None) or "abort comment"

    def post_review(self, state: ReviewState) -> str:
        inline, body_only = split_comments(state.findings, state.changed_files)
        body = ["### prbot automated review", "", state.review_summary or ""]
        if body_only:
            body += ["", "Findings outside the diff:"] + [f"- `{f.file}:{f.line}` **{f.severity}** {f.title}" for f in body_only]
        if state.status == "patched":
            body += ["", "✅ A verified fix (tests pass in an isolated container) is available as a follow-up PR."]
        review = self.pr.create_review(
            commit=self.repo.get_commit(self.pr.head.sha), body="\n".join(body).strip(),
            event="COMMENT", comments=inline,
        )
        return getattr(review, "html_url", None) or f"review {review.id}"

    def open_fix_pr(self, state: ReviewState, repo_path: str) -> str:
        """Commit the verified patch to prbot/fix-pr-N and open a PR targeting the PR's head branch."""
        if state.status != "patched" or state.patch is None:
            raise RuntimeError("Refusing to push: patch was not verified by the tester.")
        branch = f"prbot/fix-pr-{self.pr.number}"
        same_repo = self.meta().same_repo
        base = self.pr.head.ref if same_repo else self.pr.base.ref
        self.repo.create_git_ref(ref=f"refs/heads/{branch}", sha=self.pr.head.sha)
        changes = apply_patch_in_memory(repo_path, state.patch.diff)
        for path, content in changes.items():
            msg = f"prbot: fix {state.findings[0].category if state.findings else 'issues'} in {path}"
            try:
                existing = self.repo.get_contents(path, ref=branch)
                if content is None:
                    self.repo.delete_file(path, msg, existing.sha, branch=branch)
                else:
                    self.repo.update_file(path, msg, content, existing.sha, branch=branch)
            except Exception as e:  # 404 -> new file
                if getattr(e, "status", None) != 404 or content is None:
                    raise
                self.repo.create_file(path, msg, content, branch=branch)
        titles = "; ".join(f.title for f in state.actionable_findings())[:200]
        pr = self.repo.create_pull(
            title=f"prbot: fixes for #{self.pr.number}", head=branch, base=base,
            body=f"Automated fix for {titles}.\n\nVerified: tests passed after {state.attempts} attempt(s) "
                 f"in a network-isolated container.\n\n{state.patch.explanation}",
        )
        return pr.html_url


class GitHubPublisher:
    """Publishes to GitHub only what the user opted into on the CLI."""

    def __init__(self, gh_pr: GitHubPR, post_review: bool = False, open_fix_pr: bool = False):
        self.gh_pr, self.post, self.fix = gh_pr, post_review, open_fix_pr

    def publish(self, state: ReviewState) -> list[str]:
        out = []
        if state.status == "aborted":
            if self.post:
                out.append(self.gh_pr.post_abort_comment(state))
            return out
        if self.post and state.findings:
            out.append(self.gh_pr.post_review(state))
        if self.fix and state.status == "patched":
            out.append(self.gh_pr.open_fix_pr(state, state.repo_path))
        return out
