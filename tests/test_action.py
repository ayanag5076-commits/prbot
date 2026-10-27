"""The composite GitHub Action and the copy-paste example workflow stay valid."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_action_yml_is_a_valid_composite_action():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["openai-api-key"]["required"] is True
    script = next(s["run"] for s in action["runs"]["steps"] if s.get("id") == "review")
    assert "--post-review" in script and "--yes" in script
    assert "prbot-tester:py3.12" in "".join(s.get("run", "") for s in action["runs"]["steps"])


def test_example_workflow_uses_least_privilege():
    wf = yaml.safe_load((ROOT / "examples/prbot-review.yml").read_text())
    assert wf["permissions"] == {"contents": "read", "pull-requests": "write"}
    step = wf["jobs"]["prbot"]["steps"][0]
    assert step["uses"].startswith("ayanag5076-commits/prbot@")
    assert step["with"]["openai-api-key"] == "${{ secrets.OPENAI_API_KEY }}"
