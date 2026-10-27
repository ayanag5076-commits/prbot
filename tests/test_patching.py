import pytest

from prbot.patching import (PatchError, added_lines_from_patch, apply_patch_in_memory, apply_patch_to_dir,
                            commentable_lines_from_patch, parse_unified_diff)

ORIG = "a\nb\nc\nd\ne\n"


def test_apply_simple(tmp_path):
    (tmp_path / "f.txt").write_text(ORIG)
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -2,3 +2,3 @@\n b\n-c\n+C\n d\n"
    apply_patch_to_dir(tmp_path, diff)
    assert (tmp_path / "f.txt").read_text() == "a\nb\nC\nd\ne\n"


def test_wrong_hunk_header_still_applies(tmp_path):
    (tmp_path / "f.txt").write_text(ORIG)
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -40,9 +40,9 @@\n c\n-d\n+D\n e\n"
    assert apply_patch_in_memory(tmp_path, diff)["f.txt"] == "a\nb\nc\nD\ne\n"


def test_context_mismatch_raises_helpful_error(tmp_path):
    (tmp_path / "f.txt").write_text(ORIG)
    with pytest.raises(PatchError, match="hunk #1 does not match"):
        apply_patch_in_memory(tmp_path, "--- a/f.txt\n+++ b/f.txt\n@@ -1,2 +1,2 @@\n zzz\n-a\n+A\n")


def test_new_and_deleted_files(tmp_path):
    (tmp_path / "old.txt").write_text("bye\n")
    diff = ("--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
            "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n")
    apply_patch_to_dir(tmp_path, diff)
    assert (tmp_path / "new.txt").read_text() == "hello\nworld\n"
    assert not (tmp_path / "old.txt").exists()


def test_path_traversal_blocked(tmp_path):
    with pytest.raises(PatchError, match="escapes"):
        apply_patch_in_memory(tmp_path, "--- /dev/null\n+++ b/../evil.txt\n@@ -0,0 +1 @@\n+x\n")


def test_removed_line_starting_with_dashes_is_not_a_header(tmp_path):
    (tmp_path / "f.sql").write_text("select 1;\n-- comment\nselect 2;\n")
    diff = "--- a/f.sql\n+++ b/f.sql\n@@ -1,3 +1,2 @@\n select 1;\n--- comment\n select 2;\n"
    assert len(parse_unified_diff(diff)) == 1
    assert apply_patch_in_memory(tmp_path, diff)["f.sql"] == "select 1;\nselect 2;\n"


def test_no_headers():
    with pytest.raises(PatchError):
        parse_unified_diff("just text")


def test_line_maps_for_github_comments():
    patch = "@@ -10,4 +10,5 @@ def f():\n ctx\n-old\n+new1\n+new2\n ctx2\n"
    assert added_lines_from_patch(patch) == [11, 12]
    assert commentable_lines_from_patch(patch) == {10, 11, 12, 13}
