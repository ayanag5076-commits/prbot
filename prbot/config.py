"""Runtime settings, read from the environment (and a local .env if present)."""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

try:  # optional: load .env when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

Severity = Literal["low", "medium", "high", "critical"]


class Settings(BaseModel):
    openai_model: str = Field(default="gpt-4.1-mini")
    max_attempts: int = Field(default=3, ge=1, le=10)
    min_severity: Severity = "medium"
    test_image: str = "prbot-tester:py3.12"
    test_timeout: int = Field(default=120, ge=5)
    github_auth: Literal["token", "oauth", "app"] = "token"
    patcher_max_steps: int = Field(default=10, ge=2)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            openai_model=env.get("OPENAI_MODEL", "gpt-4.1-mini"),
            max_attempts=int(env.get("PRBOT_MAX_ATTEMPTS", 3)),
            min_severity=env.get("PRBOT_MIN_SEVERITY", "medium"),  # type: ignore[arg-type]
            test_image=env.get("PRBOT_TEST_IMAGE", "prbot-tester:py3.12"),
            test_timeout=int(env.get("PRBOT_TEST_TIMEOUT", 120)),
            github_auth=env.get("PRBOT_GITHUB_AUTH", "token"),  # type: ignore[arg-type]
        )
