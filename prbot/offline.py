"""Offline mode: no API key, no network. Used by --offline, the test suite and demo recordings.

- HeuristicReviewer: a small AST/regex static pass that finds the same bug classes the LLM reviewer targets.
- ScriptedChatModel: a stand-in chat model that emits pre-written tool calls, so the real ReAct loop,
  tools, patch validation, tester and self-correction edge all run for real.
- demo_scripts(): attempt 1 submits a plausible-but-wrong fix (MySQL '%s' placeholder in SQLite) so the
  tests fail and the logs are fed back; attempt 2 submits the correct fix.
"""
from __future__ import annotations

import ast
import difflib
import re
import uuid

from langchain_core.messages import AIMessage

from prbot.models import ChangedFile, Finding, Findings
from prbot.workspace import Workspace

SQL_FSTRING = re.compile(r"""f["'].*\b(SELECT|INSERT|UPDATE|DELETE)\b.*\{""", re.IGNORECASE)


class HeuristicReviewer:
    def review(self, ws: Workspace, changed: list[ChangedFile]) -> Findings:
        findings: list[Finding] = []
        for cf in changed:
            if not cf.path.endswith(".py") or not ws.exists(cf.path):
                continue
            src = ws.read_text(cf.path)
            changed_set = set(cf.changed_lines) or set(range(1, len(src.splitlines()) + 1))
            for n, line in enumerate(src.splitlines(), start=1):
                if n in changed_set and SQL_FSTRING.search(line):
                    findings.append(Finding(
                        file=cf.path, line=n, severity="critical", category="security",
                        title="SQL injection via f-string query",
                        rationale="User input is interpolated straight into SQL; a name like \"x' OR '1'='1\" rewrites the query.",
                        suggestion="Use a parameterized query: conn.execute('... WHERE name = ?', (name,)).",
                    ))
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.AsyncFor))]:
                for inner in ast.walk(loop):
                    if inner is loop:
                        continue
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                            and inner.func.attr in {"execute", "query", "get"} and inner.lineno in changed_set
                            and isinstance(inner.func.value, ast.Name) and inner.func.value.id in {"conn", "cursor", "db", "session"}):
                        findings.append(Finding(
                            file=cf.path, line=inner.lineno, severity="high", category="performance",
                            title="N+1 query inside a loop",
                            rationale="One database round-trip per row of the outer query; cost grows linearly with users.",
                            suggestion="Fetch everything in one query with a JOIN + GROUP BY.",
                        ))
                        break
                    if (isinstance(inner, (ast.For, ast.AsyncFor)) and loop.lineno in changed_set
                            and ast.dump(_iter_root(inner.iter)) == ast.dump(_iter_root(loop.iter))):
                        findings.append(Finding(
                            file=cf.path, line=loop.lineno, end_line=inner.lineno, severity="medium", category="performance",
                            title="Quadratic nested loop over the same sequence",
                            rationale="Every element is compared with every other element (O(n²)), plus an O(n) list membership test.",
                            suggestion="Count once with collections.Counter and keep order with dict.fromkeys.",
                        ))
                        break
        uniq = {(f.file, f.line, f.title): f for f in findings}
        out = sorted(uniq.values(), key=lambda f: (f.file, f.line))
        summary = (f"Found {len(out)} issue(s): " + ", ".join(f.title for f in out)) if out else "No issues found."
        return Findings(findings=out, summary=summary)


def _iter_root(node: ast.AST) -> ast.AST:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "enumerate" and node.args:
        return node.args[0]
    return node


# ------------------------------------------------------------------ scripted model
def _call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}])


class ScriptedChatModel:
    """Each new conversation (a fresh patcher attempt) consumes the next script of AIMessages."""

    def __init__(self, scripts: list[list[AIMessage]]):
        self.scripts = scripts
        self.attempt = -1
        self.step = 0

    def bind_tools(self, _tools):
        return self

    def invoke(self, messages):
        if len(messages) <= 2:  # system + task => new attempt
            self.attempt += 1
            self.step = 0
        script = self.scripts[min(self.attempt, len(self.scripts) - 1)]
        msg = script[self.step] if self.step < len(script) else AIMessage(content="Done.")
        self.step += 1
        return msg


def make_diff(ws: Workspace, edits: dict[str, list[tuple[str, str]]]) -> str:
    """Build a unified diff by applying exact string replacements to files in the workspace."""
    chunks = []
    for rel, reps in edits.items():
        old = ws.read_text(rel)
        new = old
        for a, b in reps:
            if a not in new:
                raise ValueError(f"snippet not found in {rel}: {a[:60]!r}")
            new = new.replace(a, b)
        chunks.append("".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}")))
    return "".join(chunks)


SQLI_OLD = """    query = f"SELECT id, name, email FROM users WHERE name = '{name}'"
    return conn.execute(query).fetchone()"""
SQLI_WRONG = """    query = "SELECT id, name, email FROM users WHERE name = %s"
    return conn.execute(query, (name,)).fetchone()"""
SQLI_FIX = """    query = "SELECT id, name, email FROM users WHERE name = ?"
    return conn.execute(query, (name,)).fetchone()"""
DUP_OLD = """    duplicates = []
    for i, email in enumerate(emails):
        for j, other in enumerate(emails):
            if i != j and email == other and email not in duplicates:
                duplicates.append(email)
    return duplicates"""
DUP_FIX = """    counts = Counter(emails)
    return list(dict.fromkeys(e for e in emails if counts[e] > 1))"""
N1_OLD = """    totals = {}
    users = conn.execute("SELECT id, name FROM users ORDER BY id").fetchall()
    for user_id, name in users:
        rows = conn.execute(
            "SELECT amount FROM orders WHERE user_id = ?", (user_id,)
        ).fetchall()
        totals[name] = sum(amount for (amount,) in rows)
    return totals"""
N1_FIX = """    rows = conn.execute(
        \"\"\"
        SELECT u.name, COALESCE(SUM(o.amount), 0)
        FROM users u LEFT JOIN orders o ON o.user_id = u.id
        GROUP BY u.id, u.name
        ORDER BY u.id
        \"\"\"
    ).fetchall()
    return {name: total for name, total in rows}"""


def demo_scripts(ws: Workspace) -> list[list[AIMessage]]:
    users_fix = [(SQLI_OLD, SQLI_FIX), (DUP_OLD, DUP_FIX), ("import sqlite3\n", "import sqlite3\nfrom collections import Counter\n")]
    users_wrong = [(SQLI_OLD, SQLI_WRONG), (DUP_OLD, DUP_FIX), ("import sqlite3\n", "import sqlite3\nfrom collections import Counter\n")]
    wrong = make_diff(ws, {"app/users.py": users_wrong, "app/reports.py": [(N1_OLD, N1_FIX)]})
    right = make_diff(ws, {"app/users.py": users_fix, "app/reports.py": [(N1_OLD, N1_FIX)]})
    return [
        [_call("read_file", path="app/users.py"), _call("search_code", pattern=r"\.execute\("),
         _call("write_patch", diff=wrong, explanation="Parameterize the user lookup, replace O(n²) duplicate scan with Counter, collapse N+1 into one JOIN.")],
        [_call("read_file", path="app/users.py", start_line=5, end_line=8),
         _call("write_patch", diff=right, explanation="Use SQLite's '?' placeholder (the '%s' style raised OperationalError), Counter-based duplicate scan, single JOIN query for totals.")],
    ]
