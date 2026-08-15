"""Tests for the NUL-safe Git diff planner in sourcerer.commands.index.git.

Two layers:
  1. Integration against a real temporary Git repository for the common change kinds (add,
     modify, delete, rename, type change) and awkward path bytes (spaces, tabs, Unicode).
  2. Pure-parser tests over crafted `-z` byte streams for copy/rename records and truncation,
     which are hard to elicit deterministically from git itself.
"""

# Standard packages
import os
import subprocess

# Third-party packages
import pytest

# App packages
from sourcerer.commands.index.git import (
    ChangePlan,
    _parse_name_status_z,
    base_commit_available,
    plan_changes,
)


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _commit_all(repo, message):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-m", message, env=env)
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@e")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


class TestBaseCommitAvailable:
    def test_plan_changes_base_available_for_present_commit(self, repo):
        (repo / "a.txt").write_text("hi\n")
        sha = _commit_all(repo, "init")
        assert base_commit_available(repo, sha) is True

    def test_plan_changes_base_unavailable_for_absent_commit(self, repo):
        (repo / "a.txt").write_text("hi\n")
        _commit_all(repo, "init")
        assert base_commit_available(repo, "0" * 40) is False


class TestPlanChangesIntegration:
    def test_plan_changes_base_missing_returns_flagged_plan(self, repo):
        (repo / "a.txt").write_text("hi\n")
        new = _commit_all(repo, "init")
        plan = plan_changes(repo, "0" * 40, new)
        assert plan.base_missing is True
        assert plan.delete_paths == [] and plan.index_paths == []

    def test_plan_changes_add_modify_delete(self, repo):
        (repo / "keep.txt").write_text("keep\n")
        (repo / "gone.txt").write_text("gone\n")
        (repo / "mod.txt").write_text("v1\n")
        old = _commit_all(repo, "init")
        (repo / "gone.txt").unlink()
        (repo / "mod.txt").write_text("incremental\n")
        (repo / "new.txt").write_text("new\n")
        new = _commit_all(repo, "change")
        plan = plan_changes(repo, old, new)
        assert set(plan.index_paths) == {"mod.txt", "new.txt"}
        assert set(plan.delete_paths) == {"gone.txt", "mod.txt"}

    def test_plan_changes_rename(self, repo):
        (repo / "old_name.txt").write_text("stable content here\nline two\n")
        old = _commit_all(repo, "init")
        _git(repo, "mv", "old_name.txt", "new_name.txt")
        new = _commit_all(repo, "rename")
        plan = plan_changes(repo, old, new)
        assert "old_name.txt" in plan.delete_paths
        assert "new_name.txt" in plan.index_paths
        assert "old_name.txt" not in plan.index_paths

    def test_plan_changes_type_change_file_to_symlink(self, repo):
        (repo / "target.txt").write_text("target\n")
        (repo / "thing").write_text("regular\n")
        old = _commit_all(repo, "init")
        (repo / "thing").unlink()
        os.symlink("target.txt", repo / "thing")
        new = _commit_all(repo, "typechange")
        plan = plan_changes(repo, old, new)
        # A type change replaces the whole file: delete the stale docs and re-index.
        assert "thing" in plan.delete_paths
        assert "thing" in plan.index_paths

    def test_plan_changes_awkward_paths_preserved(self, repo):
        (repo / "seed.txt").write_text("seed\n")
        old = _commit_all(repo, "init")
        weird = "dir with spaces/tab\tfile.txt"
        unicode_path = "café/naïve.txt"
        (repo / "dir with spaces").mkdir()
        (repo / weird).write_text("x\n")
        (repo / "café").mkdir()
        (repo / unicode_path).write_text("y\n")
        new = _commit_all(repo, "add weird paths")
        plan = plan_changes(repo, old, new)
        assert weird in plan.index_paths
        assert unicode_path in plan.index_paths


class TestParseNameStatusZ:
    def test_plan_changes_parser_copy_indexes_destination_only(self):
        raw = b"C100\x00src.txt\x00dst.txt\x00"
        delete, index = _parse_name_status_z(raw)
        assert delete == []
        assert index == ["dst.txt"]

    def test_plan_changes_parser_rename_deletes_source_indexes_destination(self):
        raw = b"R100\x00from.txt\x00to.txt\x00"
        delete, index = _parse_name_status_z(raw)
        assert delete == ["from.txt"]
        assert index == ["to.txt"]

    def test_plan_changes_parser_mixed_stream(self):
        raw = (
            b"A\x00added.txt\x00"
            b"M\x00mod.txt\x00"
            b"D\x00del.txt\x00"
            b"R096\x00old.txt\x00new.txt\x00"
        )
        delete, index = _parse_name_status_z(raw)
        assert delete == ["mod.txt", "del.txt", "old.txt"]
        assert index == ["added.txt", "mod.txt", "new.txt"]

    def test_plan_changes_parser_dedupe_preserves_order(self):
        raw = b"M\x00a.txt\x00M\x00a.txt\x00"
        delete, index = _parse_name_status_z(raw)
        assert delete == ["a.txt"]
        assert index == ["a.txt"]

    def test_plan_changes_parser_truncated_rename_record_ignored(self):
        raw = b"R100\x00only_one_path.txt\x00"
        delete, index = _parse_name_status_z(raw)
        assert delete == [] and index == []

    def test_plan_changes_parser_empty_stream(self):
        assert _parse_name_status_z(b"") == ([], [])


class TestChangePlanDefaults:
    def test_plan_changes_defaults(self):
        plan = ChangePlan()
        assert plan.delete_paths == [] and plan.index_paths == []
        assert plan.base_missing is False
