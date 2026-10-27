from prbot.cli import build_parser, main


def test_parser_github_mode():
    a = build_parser().parse_args(["review", "--repo", "o/r", "--pr", "7"])
    assert a.repo == "o/r" and a.pr == 7 and not a.post_review and not a.open_fix_pr


def test_github_mode_requires_repo_and_pr():
    assert main(["review"]) == 2


def test_offline_dry_run_end_to_end(demo_copy, tmp_path, capsys):
    code = main(["review", "--dry-run", "--path", str(demo_copy), "--diff", str(demo_copy / "pr.diff"),
                 "--offline", "--no-docker", "--out", str(tmp_path / "out"),
                 "--metrics-file", str(tmp_path / "m.jsonl")])
    out = capsys.readouterr().out
    assert code == 0 and "status: patched" in out
    assert (tmp_path / "out" / "report.md").exists()
