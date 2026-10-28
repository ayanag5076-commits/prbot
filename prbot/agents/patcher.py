"""Agent B: a ReAct loop (reason -> call tool -> observe) that writes a unified-diff patch."""
from __future__ import annotations

import json
from typing import Protocol

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from prbot.models import Finding, PatchProposal
from prbot.tools import PatchHolder, make_tools
from prbot.workspace import Workspace

PATCHER_SYSTEM = """You are an expert engineer who fixes security and performance bugs with minimal, safe patches.
Work in a ReAct loop: think, call a tool, read the observation, repeat.
1. Use read_file (and search_code for callers/usages) to see the exact current code. Never guess file contents.
2. Before editing a function, search_code for its name under tests/ and read those tests: they are the behaviour
   contract. Preserve it exactly: return types, ordering ("first seen" order means order of FIRST appearance),
   rows with no matches (use LEFT JOIN + COALESCE so they still appear with 0), empty inputs, None results.
3. Fix every listed finding with the smallest change that preserves the public API and existing behaviour.
4. Submit ONE unified diff covering all files via write_patch. Context lines must match the file exactly.
5. If write_patch returns ERROR, read the file again and resubmit a corrected diff.
Never edit tests to make them pass. Test files, conftest.py and pytest/collection settings (pytest.ini,
pyproject.toml, setup.cfg, tox.ini) are read-only: write_patch rejects any diff that touches them, and every
test that passed before your patch must still pass (and still be collected) after it."""

FAILED_APPROACHES = """

## Approaches that already FAILED the test suite
The following {n} patch(es) were applied and the tests failed. You MUST pivot to a structurally different
solution. Do not resubmit any of them, and do not submit small variations of them (renamed variables,
reordered lines, different formatting). First explain in one sentence why the last approach failed, based on
the test output, then choose a different approach. write_patch rejects exact repeats.
{patches}"""


class Patcher(Protocol):
    def propose(self, ws: Workspace, findings: list[Finding], feedback: list[str],
                attempted_patches: list[str]) -> PatchProposal | None: ...


def build_system(attempted_patches: list[str], feedback: list[str]) -> str:
    """System prompt plus the full history of failed diffs (counters recency bias / correction loops)."""
    if not attempted_patches:
        return PATCHER_SYSTEM
    blocks = []
    for i, diff in enumerate(attempted_patches, start=1):
        log = feedback[i - 1] if i - 1 < len(feedback) else ""
        log_tail = "\n".join(log.strip().splitlines()[-25:])
        blocks.append(f"\n### Failed attempt {i}\n```diff\n{diff.strip()}\n```\nTest output (tail):\n```\n{log_tail}\n```")
    return PATCHER_SYSTEM + FAILED_APPROACHES.format(n=len(attempted_patches), patches="".join(blocks))


def build_task(findings: list[Finding], feedback: list[str], attempted_patches: list[str]) -> str:
    items = [f.model_dump(exclude_none=True) for f in findings]
    task = "Fix these findings:\n```json\n" + json.dumps(items, indent=2) + "\n```"
    if attempted_patches:
        task += (f"\n\nThis is attempt {len(attempted_patches) + 1}. {len(attempted_patches)} earlier approach(es) failed "
                 "(see system prompt). Write a structurally different patch against the ORIGINAL files.")
    elif feedback:  # patcher produced no valid patch last time
        task += f"\n\nThe previous attempt did not produce a valid patch:\n```\n{feedback[-1]}\n```"
    return task


class ReActPatcher:
    def __init__(self, chat_model, max_steps: int = 10):
        self.chat_model = chat_model
        self.max_steps = max_steps
        self.last_usage: dict | None = None

    def propose(self, ws, findings, feedback, attempted_patches):
        holder = PatchHolder()
        tools = make_tools(ws, holder, failed_patches=attempted_patches)
        by_name = {t.name: t for t in tools}
        model = self.chat_model.bind_tools(tools) if hasattr(self.chat_model, "bind_tools") else self.chat_model
        messages = [SystemMessage(build_system(attempted_patches, feedback)),
                    HumanMessage(build_task(findings, feedback, attempted_patches))]
        trace: list[str] = []
        from prbot.metrics import usage_from_message

        usage = {"agent": "patcher", "attempt": len(attempted_patches) + 1, "input_tokens": 0,
                 "cached_input_tokens": 0, "output_tokens": 0, "calls": 0}
        self.last_usage = usage
        for _ in range(self.max_steps):
            ai: AIMessage = model.invoke(messages)
            i, c, o = usage_from_message(ai)
            usage["input_tokens"] += i
            usage["cached_input_tokens"] += c
            usage["output_tokens"] += o
            usage["calls"] += 1
            messages.append(ai)
            calls = getattr(ai, "tool_calls", None) or []
            if not calls:
                break
            for call in calls:
                tool = by_name.get(call["name"])
                trace.append(call["name"])
                if tool is None:
                    obs = f"ERROR: unknown tool {call['name']}"
                else:
                    try:
                        obs = tool.invoke(call.get("args", {}))
                    except Exception as e:  # tool arg validation errors are fed back, not raised
                        obs = f"ERROR: {type(e).__name__}: {e}"
                messages.append(ToolMessage(content=str(obs), tool_call_id=call.get("id") or call["name"]))
            if holder.patch is not None:
                break
        if holder.patch is not None:
            holder.patch.tool_trace = trace
        return holder.patch
