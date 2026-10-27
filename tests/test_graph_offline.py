"""End-to-end run of the real StateGraph on the demo repo, with no LLM and no network."""
from prbot.agents.patcher import ReActPatcher
from prbot.agents.tester import LocalRunner, Tester
from prbot.context import changed_files_from_diff
from prbot.graph import Agents, build_graph, run_review
from prbot.models import Findings, ReviewState
from prbot.offline import HeuristicReviewer, ScriptedChatModel, demo_scripts
from prbot.publishers import LocalPublisher
from prbot.workspace import Workspace


def _state(root, **kw):
    changed = changed_files_from_diff((root / "pr.diff").read_text())
    return ReviewState(repo_path=str(root), changed_files=changed, **kw)


def test_self_correction_loop_fixes_on_second_attempt(demo_copy, tmp_path):
    scripts = demo_scripts(Workspace(demo_copy))
    agents = Agents(HeuristicReviewer(), ReActPatcher(ScriptedChatModel(scripts)), Tester(LocalRunner(), 60),
                    LocalPublisher(tmp_path / "out"))
    final = run_review(agents, _state(demo_copy, max_attempts=3))
    assert final.status == "patched"
    assert final.attempts == 2
    assert [h["passed"] for h in final.attempt_history] == [False, True]
    assert "OperationalError" in final.feedback[0]  # failing logs were fed back to the patcher
    assert (tmp_path / "out" / "fix.patch").exists()
    assert "Self-correction loop" in final.report


def test_gives_up_after_max_attempts(demo_copy, tmp_path):
    wrong_only = demo_scripts(Workspace(demo_copy))[:1]  # always submits the broken patch
    agents = Agents(HeuristicReviewer(), ReActPatcher(ScriptedChatModel(wrong_only)), Tester(LocalRunner(), 60),
                    LocalPublisher(tmp_path / "out"))
    final = run_review(agents, _state(demo_copy, max_attempts=2))
    assert final.status == "failed" and final.attempts == 2
    assert len(final.attempted_patches) == 1  # attempt 2's identical resubmission was rejected in-loop
    assert final.patch is None and "did not submit" in final.feedback[-1]
    assert not (tmp_path / "out" / "fix.patch").exists()


class CleanReviewer:
    def review(self, ws, changed):
        return Findings(findings=[], summary="LGTM")


class ExplodingPatcher:
    def propose(self, *a):
        raise AssertionError("patcher must not run when there are no findings")


def test_clean_pr_skips_patcher(demo_copy):
    final = run_review(Agents(CleanReviewer(), ExplodingPatcher(), Tester(LocalRunner())), _state(demo_copy))
    assert final.status == "clean" and final.attempts == 0


def test_graph_topology():
    g = build_graph(Agents(CleanReviewer(), ExplodingPatcher(), Tester(LocalRunner()))).get_graph()
    assert {"reviewer", "patcher", "tester", "finalize"} <= set(g.nodes)
    edges = {(e.source, e.target) for e in g.edges}
    assert ("tester", "patcher") in edges and ("tester", "finalize") in edges
