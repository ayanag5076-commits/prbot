"""Command line: `python -m prbot review ...`."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prbot.config import Settings
from prbot.models import ReviewState


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prbot", description="Autonomous PR review & patching agent.")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("review", help="Review a PR (GitHub) or a local folder (--dry-run).")
    r.add_argument("--repo", help="owner/name on GitHub")
    r.add_argument("--pr", type=int, help="pull request number")
    r.add_argument("--dry-run", action="store_true",
                   help="never write to GitHub. With --repo/--pr: read the PR (anonymously if no token). Without: review --path")
    r.add_argument("--path", default="demo_repo", help="local repo folder for --dry-run")
    r.add_argument("--diff", help="unified diff file describing the change (--dry-run); default: review all source files")
    r.add_argument("--base-path", help="(--dry-run) checkout of the base branch, to attribute baseline failures")
    r.add_argument("--skip-baseline", action="store_true", help="skip the pre-patch baseline test run")
    r.add_argument("--offline", action="store_true", help="no LLM: heuristic reviewer + scripted patcher (demo repo)")
    r.add_argument("--no-docker", action="store_true", help="run tests on the host instead of Docker (unsafe)")
    r.add_argument("--model", help="OpenAI model (default from OPENAI_MODEL or gpt-4.1-mini)")
    r.add_argument("--max-attempts", type=int, help="patch/test iterations before giving up (default 3)")
    r.add_argument("--min-severity", choices=["low", "medium", "high", "critical"])
    r.add_argument("--out", default="prbot-out", help="folder for report.md, findings.json, fix.patch")
    r.add_argument("--post-review", action="store_true", help="post review comments on the PR")
    r.add_argument("--open-fix-pr", action="store_true", help="push a verified fix branch and open a follow-up PR")
    r.add_argument("--yes", action="store_true", help="skip the confirmation prompt before writing to GitHub")
    r.add_argument("--metrics-file", default="prbot-out/metrics.jsonl", help="append one JSON metrics record per run")
    r.add_argument("--repeat", type=int, default=1, help="(--dry-run) repeat the run N times to build metrics")

    m = sub.add_parser("metrics", help="Aggregate the metrics ledger (token cost, recovery rate, sandbox latency).")
    m.add_argument("--file", default="prbot-out/metrics.jsonl")
    m.add_argument("--markdown", action="store_true", help="print the detailed metrics table")
    m.add_argument("--by-node", action="store_true",
                   help="average tokens and cost per node (reviewer pass, patcher attempt) across runs")
    m.add_argument("--telemetry", action="store_true", help="print the README 'System Benchmarks & Telemetry' table")
    m.add_argument("--update-readme", metavar="README", nargs="?", const="README.md",
                   help="write the telemetry table into README.md between the METRICS markers")

    b = sub.add_parser("bench-sandbox", help="Measure Docker sandbox overhead vs running pytest on the host.")
    b.add_argument("--path", default="demo_repo")
    b.add_argument("--runs", type=int, default=5)
    b.add_argument("--metrics-file", default="prbot-out/metrics.jsonl")
    b.add_argument("--no-docker", action="store_true", help="host runner only (for machines without Docker)")
    return p


def _agents(args, settings: Settings, ws_root: str, publisher):
    from prbot.agents.patcher import ReActPatcher
    from prbot.agents.reviewer import LLMReviewer
    from prbot.agents.tester import DockerRunner, LocalRunner, Tester
    from prbot.graph import Agents

    runner = LocalRunner() if args.no_docker else DockerRunner(settings.test_image)
    tester = Tester(runner, timeout=settings.test_timeout)
    if args.offline:
        from prbot.offline import HeuristicReviewer, ScriptedChatModel, demo_scripts
        from prbot.workspace import Workspace

        try:
            scripts = demo_scripts(Workspace(ws_root))
        except (ValueError, Exception) as e:
            raise SystemExit(f"--offline only knows how to patch the bundled demo_repo ({e}).")
        return Agents(HeuristicReviewer(), ReActPatcher(ScriptedChatModel(scripts)), tester, publisher)
    from prbot.llm import make_chat_model

    chat = make_chat_model(settings.openai_model)
    return Agents(LLMReviewer(chat), ReActPatcher(chat, settings.patcher_max_steps), tester, publisher)


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def cmd_review(args) -> int:
    from prbot.context import changed_files_from_diff, changed_files_from_tree
    from prbot.graph import run_review
    from prbot.publishers import CompositePublisher, LocalPublisher
    from prbot.workspace import Workspace

    settings = Settings.from_env()
    if args.model:
        settings.openai_model = args.model
    if args.max_attempts:
        settings.max_attempts = args.max_attempts
    if args.min_severity:
        settings.min_severity = args.min_severity
    local = LocalPublisher(args.out)

    if args.dry_run and not (args.repo and args.pr):
        root = str(Path(args.path).resolve())
        ws = Workspace(root)
        changed = changed_files_from_diff(Path(args.diff).read_text()) if args.diff else changed_files_from_tree(ws)
        base = str(Path(args.base_path).resolve()) if args.base_path else None
        state = ReviewState(repo_path=root, base_path=base, changed_files=changed, max_attempts=settings.max_attempts,
                            min_severity=settings.min_severity, run_baseline=not args.skip_baseline)
        agents = _agents(args, settings, root, local)
    else:
        if not (args.repo and args.pr):
            print("error: --repo and --pr are required (or use --dry-run)", file=sys.stderr)
            return 2
        from prbot.github.auth import get_auth
        from prbot.github.client import GitHubPR, GitHubPublisher

        if args.dry_run and (args.post_review or args.open_fix_pr):
            print("note: --dry-run never writes to GitHub; ignoring --post-review/--open-fix-pr")
            args.post_review = args.open_fix_pr = False
        auth = get_auth(settings.github_auth, repo=args.repo, allow_push=args.open_fix_pr, read_only=args.dry_run)
        gh_pr = GitHubPR(auth, args.repo, args.pr)
        meta = gh_pr.meta()
        print(f"Fetching {meta.repo}#{meta.number}: {meta.title}")
        root = str(gh_pr.checkout())
        base = None if args.skip_baseline else str(gh_pr.checkout_base())
        state = ReviewState(repo_path=root, base_path=base, changed_files=gh_pr.changed_files(), pr=meta,
                            max_attempts=settings.max_attempts, min_severity=settings.min_severity,
                            run_baseline=not args.skip_baseline)
        wants_write = args.post_review or args.open_fix_pr
        if wants_write and not args.yes and not _confirm(f"prbot will write to GitHub ({meta.repo}#{meta.number}). Continue?"):
            wants_write = False
        publisher = CompositePublisher(local, GitHubPublisher(gh_pr, args.post_review and wants_write,
                                                              args.open_fix_pr and wants_write))
        agents = _agents(args, settings, root, publisher)

    from prbot.metrics import Stopwatch, append_record, build_record

    repeat = max(1, args.repeat) if (args.dry_run and not args.repo) else 1
    model_name = "offline" if args.offline else settings.openai_model
    for i in range(repeat):
        if i:
            agents = _agents(args, settings, state.repo_path, agents.publisher)  # fresh scripted/LLM state
        with Stopwatch() as sw:
            final = run_review(agents, state)
        record = build_record(final, model_name, sw.elapsed)
        if args.metrics_file:
            append_record(args.metrics_file, record)
    print(f"\nstatus: {final.status} | findings: {len(final.findings)} | attempts: {final.attempts}/{final.max_attempts}")
    if final.abort_reason:
        print(f"  {final.abort_reason}")
    for f in final.findings:
        print(f"  [{f.severity:<8}] {f.category:<11} {f.file}:{f.line}  {f.title}")
    for h in final.attempt_history:
        print(f"  attempt {h['attempt']}: tests {'PASS' if h['passed'] else 'FAIL'} (exit {h['exit_code']}, {h['runner']})")
    for item in final.published:
        print(f"  -> {item}")
    cost = "n/a" if record.cost_usd is None else f"${record.cost_usd:.5f}"
    docker = [s for s in record.sandbox if s.overhead_s is not None]
    over = f", sandbox overhead {sum(s.overhead_s for s in docker) / len(docker):.2f}s/run" if docker else ""
    print(f"  metrics: {record.total_tokens} tokens, {cost}, {record.wall_s}s total{over}")
    if record.total_tokens:
        from prbot.metrics import format_breakdown, node_breakdown
        print("  tokens by node:")
        print(format_breakdown(node_breakdown(record.tokens, record.model)))
    if final.status == "aborted":
        return 3
    return 0 if final.status in {"clean", "patched", "review_only"} else 1


def cmd_metrics(args) -> int:
    import json

    from prbot.metrics import load_records, summarize, to_markdown, to_telemetry_table, update_readme

    records = load_records(args.file)
    if not records:
        print(f"No runs recorded in {args.file} yet.")
        return 1
    s = summarize(records)
    if getattr(args, "by_node", False):
        from prbot.metrics import breakdown_summary
        rows = breakdown_summary(records)
        if not rows:
            print("No token usage recorded yet.")
            return 1
        print("| Node | Runs | Avg tokens | Avg cost |\n|---|---|---|---|")
        for r in rows:
            cost = "n/a" if r["avg_cost_usd"] is None else f"${r['avg_cost_usd']:.5f}"
            print(f"| {r['node']} | {r['runs']} | {r['avg_tokens']} | {cost} |")
        return 0
    if args.update_readme:
        ok = update_readme(args.update_readme, to_telemetry_table(s))
        print(f"updated {args.update_readme}" if ok else f"METRICS markers not found in {args.update_readme}")
        return 0 if ok else 2
    if args.telemetry:
        print(to_telemetry_table(s))
    else:
        print(to_markdown(s) if args.markdown else json.dumps(s, indent=2))
    return 0


def cmd_bench(args) -> int:
    from prbot.agents.tester import DockerRunner, LocalRunner, Tester
    from prbot.metrics import RunRecord, append_record, sandbox_run

    settings = Settings.from_env()
    runners = [LocalRunner()] + ([] if args.no_docker else [DockerRunner(settings.test_image)])
    for runner in runners:
        tester = Tester(runner, timeout=settings.test_timeout)
        runs = [sandbox_run("bench", tester.baseline(args.path)) for _ in range(args.runs)]
        bad = [r for r in runs if not r.passed]
        if bad:
            print(f"{runner.name}: {len(bad)} run(s) failed; check the image/tests first.")
        walls = sorted(r.wall_s for r in runs)
        overs = sorted(r.overhead_s for r in runs if r.overhead_s is not None)
        print(f"{runner.name:>6}: wall median {walls[len(walls) // 2]:.3f}s, "
              f"overhead median {overs[len(overs) // 2] if overs else float('nan'):.3f}s over {len(runs)} runs")
        append_record(args.metrics_file, RunRecord(target=f"bench:{args.path}", model="none", status="bench",
                                                   findings=0, actionable=0, attempts=0, sandbox=runs))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "review":
        return cmd_review(args)
    if args.command == "metrics":
        return cmd_metrics(args)
    if args.command == "bench-sandbox":
        return cmd_bench(args)
    return 2
