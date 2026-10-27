"""Auth providers behind one interface.

- GitHubAppAuth (recommended): JWT signed with the App's private key -> short-lived installation token,
  scoped to the target repo with the least permissions the run needs.
- TokenAuth: personal access token, for local development.
- OAuthDeviceFlowAuth: planned, for CLI users without an App.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path

from github import Auth, Github, GithubIntegration


class GitHubAuthError(Exception):
    pass


# Least privilege: reviewing needs to read code and write PR reviews/comments. Pushing a fix branch
# additionally needs contents:write, which is requested only when --open-fix-pr is used.
REVIEW_PERMISSIONS = {"contents": "read", "pull_requests": "write", "metadata": "read"}
FIX_PR_PERMISSIONS = {**REVIEW_PERMISSIONS, "contents": "write"}

_LEVEL = {"none": 0, "read": 1, "write": 2, "admin": 3}


def required_permissions(allow_push: bool) -> dict[str, str]:
    return dict(FIX_PR_PERMISSIONS if allow_push else REVIEW_PERMISSIONS)


def missing_permissions(granted: dict[str, str], required: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in required.items() if _LEVEL.get(granted.get(k, "none"), 0) < _LEVEL[v]}


class GitHubAuthProvider(ABC):
    """Anything that can hand back an authenticated PyGithub client and a bearer token."""

    @abstractmethod
    def token(self) -> str: ...

    def client(self) -> Github:
        return Github(auth=Auth.Token(self.token()), per_page=100)


class TokenAuth(GitHubAuthProvider):
    """GITHUB_TOKEN personal access token (fine-grained: Contents + Pull requests). Dev use."""

    def __init__(self, token: str | None = None, **_ignored):
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        if not self._token:
            raise GitHubAuthError("GITHUB_TOKEN is not set. Add it to .env, use GitHub App auth, or use --dry-run.")

    def token(self) -> str:
        return self._token


def _load_private_key() -> str:
    inline = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    if inline:
        return inline.replace("\\n", "\n")
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "")
    if path and Path(path).expanduser().is_file():
        return Path(path).expanduser().read_text()
    raise GitHubAuthError("Set GITHUB_APP_PRIVATE_KEY_PATH (path to the App's .pem) or GITHUB_APP_PRIVATE_KEY.")


class GitHubAppAuth(GitHubAuthProvider):
    """GitHub App installation auth.

    1. Sign a JWT with the App's private key (PyGithub ``Auth.AppAuth``).
    2. Find the installation on the target repo (or use GITHUB_APP_INSTALLATION_ID).
    3. Exchange the JWT for a ~1 hour installation token, scoped to that one repository and down-scoped
       to exactly the permissions this run needs. Refreshed automatically before expiry.
    """

    REFRESH_MARGIN = timedelta(minutes=5)

    def __init__(self, repo: str | None = None, allow_push: bool = False, app_id: str | None = None,
                 private_key: str | None = None, installation_id: int | None = None, integration=None):
        self.app_id = app_id or os.environ.get("GITHUB_APP_ID", "")
        if not self.app_id:
            raise GitHubAuthError("GITHUB_APP_ID is not set.")
        self.repo = repo
        self.permissions = required_permissions(allow_push)
        env_inst = os.environ.get("GITHUB_APP_INSTALLATION_ID")
        self.installation_id = installation_id or (int(env_inst) if env_inst else None)
        self._integration = integration or GithubIntegration(auth=Auth.AppAuth(self.app_id, private_key or _load_private_key()))
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self.granted: dict[str, str] = {}

    def _installation(self) -> int:
        if self.installation_id is None:
            if not self.repo or "/" not in self.repo:
                raise GitHubAuthError("Need --repo owner/name (or GITHUB_APP_INSTALLATION_ID) to find the App installation.")
            owner, name = self.repo.split("/", 1)
            try:
                self.installation_id = self._integration.get_repo_installation(owner, name).id
            except Exception as e:
                raise GitHubAuthError(f"The GitHub App is not installed on {self.repo}. Install it on that repo first.") from e
        return self.installation_id

    def token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._expires_at and self._expires_at - self.REFRESH_MARGIN > now:
            return self._token
        inst = self._installation()
        try:
            access = self._integration.get_access_token(inst, permissions=self.permissions)
        except Exception as e:
            raise GitHubAuthError(
                f"Could not mint an installation token with {self.permissions}. The App must be granted at least "
                f"these permissions in its settings (and the installation must accept them): {e}"
            ) from e
        self.granted = dict(getattr(access, "permissions", None) or {})
        missing = missing_permissions(self.granted, self.permissions) if self.granted else {}
        if missing:
            raise GitHubAuthError(f"Installation token is missing permissions: {missing}")
        self._token = access.token
        expires = access.expires_at
        self._expires_at = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
        return self._token


class AnonymousAuth(GitHubAuthProvider):
    """Unauthenticated, read-only access to PUBLIC repos (60 requests/hour). Used by --dry-run with no credentials,
    so anyone can reproduce a review of a public PR without a token. Can never write."""

    def __init__(self, **_ignored):
        pass

    def token(self) -> str:
        return ""

    def client(self) -> Github:
        return Github(per_page=100)


class OAuthDeviceFlowAuth(GitHubAuthProvider):
    """Planned: GitHub OAuth device flow for CLI users who can't install an App."""

    def __init__(self, **_ignored):
        pass

    def token(self) -> str:  # pragma: no cover - planned
        raise NotImplementedError("OAuth device flow is planned; use PRBOT_GITHUB_AUTH=app or token.")


def get_auth(kind: str = "token", repo: str | None = None, allow_push: bool = False,
             read_only: bool = False) -> GitHubAuthProvider:
    """read_only=True (dry runs) falls back to anonymous access when no credentials are configured."""
    if read_only and kind == "token" and not os.environ.get("GITHUB_TOKEN"):
        return AnonymousAuth()
    if read_only and kind == "app" and not os.environ.get("GITHUB_APP_ID"):
        return AnonymousAuth()
    providers = {"token": TokenAuth, "app": GitHubAppAuth, "oauth": OAuthDeviceFlowAuth, "anonymous": AnonymousAuth}
    if kind not in providers:
        raise GitHubAuthError(f"Unknown auth kind {kind!r}; choose one of {sorted(providers)}")
    if kind == "app":
        return GitHubAppAuth(repo=repo, allow_push=allow_push)
    return providers[kind]()


