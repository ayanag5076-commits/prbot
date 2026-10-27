import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo_repo"


@pytest.fixture
def demo_copy(tmp_path) -> Path:
    dst = tmp_path / "demo"
    shutil.copytree(DEMO, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dst
