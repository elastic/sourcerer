"""Integration-style unit tests for the incremental (v2) orchestration in
sourcerer.commands.index.command.index_incremental_in_dir.

Runs against a real temporary Git repository (so the diff planner, checkout, and tracked-file
walk are exercised for real) plus a small stateful fake Elasticsearch client that models the
v2 content and refs indices in memory. Covers the Definition of done: initial index, targeted
change update, deletion/rename cleanup, no-op, injected-failure pointer safety + retry
convergence, and missing-base full reconciliation.
"""

# Standard packages
import os
import subprocess
from collections import defaultdict

# Third-party packages
import pytest
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index import command, documents
from sourcerer.indices import REFS_INDEX_V2, files_index_v2, lines_index_v2
from sourcerer.utils import build_ref_key, make_doc_id

ORG, REPO, BRANCH = "acme", "widgets", "main"


# --- git helpers ----------------------------------------------------------------------------

def _git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@e")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "checkout", "-b", BRANCH)
    return tmp_path


# --- stateful fake Elasticsearch ------------------------------------------------------------

def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("not_found", meta, None)


def _get_field(source: dict, dotted: str):
    cur = source
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _matches(source: dict, query: dict) -> bool:
    for clause in query["bool"]["filter"]:
        if "term" in clause:
            (field, value), = clause["term"].items()
            if _get_field(source, field) != value:
                return False
        elif "terms" in clause:
            (field, values), = clause["terms"].items()
            if _get_field(source, field) not in values:
                return False
    return True


class _Indices:
    def refresh(self, **kwargs):
        pass


class FakeES:
    def __init__(self):
        self.store: dict[str, dict[str, dict]] = defaultdict(dict)
        self.indices = _Indices()
        self.ref_write_history: list[tuple[str, str | None]] = []
        self.fail_ready_once = False

    def index(self, *, index, id, document, refresh=False):
        if index == REFS_INDEX_V2:
            self.ref_write_history.append((document["status"], document["git"]["commit"]))
        self.store[index][id] = document

    def get(self, *, index, id):
        bucket = self.store.get(index, {})
        if id not in bucket:
            raise _not_found()
        return {"_source": bucket[id]}

    def count(self, *, index, query):
        bucket = self.store.get(index, {})
        return {"count": sum(1 for s in bucket.values() if _matches(s, query))}

    def delete_by_query(self, *, index, query, **kwargs):
        bucket = self.store.get(index, {})
        doomed = [i for i, s in bucket.items() if _matches(s, query)]
        for i in doomed:
            del bucket[i]
        return {"deleted": len(doomed)}


def _fake_parallel_bulk(es, actions, **kwargs):
    for a in actions:
        es.store[a["_index"]][a["_id"]] = a["_source"]
        yield True, {"index": {"_index": a["_index"], "_id": a["_id"]}}


@pytest.fixture(autouse=True)
def patch_bulk_and_checkout(monkeypatch):
    # Patch the bulk helper to write into the fake store, and checkout to a plain local branch
    # checkout (the test repo has no `origin` remote to reset against).
    monkeypatch.setattr(documents, "es_parallel_bulk", _fake_parallel_bulk)

    def _local_checkout(repo_dir, branch):
        subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", "--force", branch],
            check=True, capture_output=True,
        )

    monkeypatch.setattr(command, "checkout_branch", _local_checkout)


# --- helpers to inspect the fake store ------------------------------------------------------

def _files(es):
    return es.store.get(files_index_v2(ORG, REPO), {})


def _lines(es):
    return es.store.get(lines_index_v2(ORG, REPO), {})


def _ref_doc(es):
    return es.store[REFS_INDEX_V2][make_doc_id(ORG, REPO, "branch", BRANCH)]


def _run(es, repo, force=False):
    command.index_incremental_in_dir(es, ORG, REPO, repo, BRANCH, force=force)


def _file_id(path):
    return make_doc_id(ORG, REPO, "branch", BRANCH, path)


# --- tests ----------------------------------------------------------------------------------

class TestInitialIndex:
    def test_full_branch_view_and_ready_marker(self, repo):
        (repo / "a.txt").write_text("one\ntwo\n")
        (repo / "b.txt").write_text("hello\n")
        sha = _commit(repo, "init")
        es = FakeES()
        _run(es, repo)

        assert _file_id("a.txt") in _files(es)
        assert _file_id("b.txt") in _files(es)
        ref = _ref_doc(es)
        assert ref["status"] == "ready"
        assert ref["git"]["commit"] == sha
        assert ref["git"]["target_commit"] is None
        assert ref["git"]["ref_key"] == build_ref_key(ORG, REPO, BRANCH)
        assert ref["files_count"] == 2
        assert ref["lines_count"] == 3


class TestTargetedUpdate:
    def test_only_changed_paths_touched(self, repo):
        (repo / "keep.txt").write_text("unchanged\n")
        (repo / "mod.txt").write_text("v1\n")
        (repo / "gone.txt").write_text("bye\n")
        _commit(repo, "init")
        es = FakeES()
        _run(es, repo)
        keep_line_id_before = make_doc_id(ORG, REPO, "branch", BRANCH, "keep.txt", "1")
        assert keep_line_id_before in _lines(es)

        (repo / "mod.txt").write_text("v1\nv2\n")
        (repo / "gone.txt").unlink()
        (repo / "new.txt").write_text("fresh\n")
        sha2 = _commit(repo, "change")
        _run(es, repo)

        # Deleted file leaves no docs.
        assert _file_id("gone.txt") not in _files(es)
        assert make_doc_id(ORG, REPO, "branch", BRANCH, "gone.txt", "1") not in _lines(es)
        # New + modified files present; modified file has both lines now.
        assert _file_id("new.txt") in _files(es)
        assert make_doc_id(ORG, REPO, "branch", BRANCH, "mod.txt", "2") in _lines(es)
        # Unchanged file's line doc id is stable across the update.
        assert keep_line_id_before in _lines(es)
        ref = _ref_doc(es)
        assert ref["status"] == "ready" and ref["git"]["commit"] == sha2

    def test_rename_leaves_no_source_docs(self, repo):
        (repo / "old.txt").write_text("stable content\nsecond line\n")
        _commit(repo, "init")
        es = FakeES()
        _run(es, repo)
        assert _file_id("old.txt") in _files(es)

        _git(repo, "mv", "old.txt", "new.txt")
        _commit(repo, "rename")
        _run(es, repo)

        assert _file_id("old.txt") not in _files(es)
        assert make_doc_id(ORG, REPO, "branch", BRANCH, "old.txt", "1") not in _lines(es)
        assert _file_id("new.txt") in _files(es)


class TestNoOp:
    def test_unchanged_head_writes_no_content(self, repo):
        (repo / "a.txt").write_text("one\n")
        _commit(repo, "init")
        es = FakeES()
        _run(es, repo)
        files_snapshot = dict(_files(es))
        history_len = len(es.ref_write_history)

        _run(es, repo)  # nothing changed

        assert dict(_files(es)) == files_snapshot  # no content writes
        # No new refs writes beyond the first run's indexing+ready pair.
        assert len(es.ref_write_history) == history_len


class TestFailureAndRetry:
    def test_failure_never_advances_pointer_and_retry_converges(self, repo, monkeypatch):
        (repo / "a.txt").write_text("one\n")
        sha1 = _commit(repo, "init")
        es = FakeES()
        _run(es, repo)  # clean initial index at sha1

        (repo / "a.txt").write_text("one\ntwo\n")
        (repo / "b.txt").write_text("new file\n")
        sha2 = _commit(repo, "change")

        # Inject an indexing failure for the update run.
        def boom(*a, **k):
            raise RuntimeError("bulk exploded")

        monkeypatch.setattr(command, "index_paths_v2", boom)
        with pytest.raises(RuntimeError):
            _run(es, repo)

        ref = _ref_doc(es)
        assert ref["status"] == "indexing"        # not advanced past the failure
        assert ref["git"]["commit"] == sha1        # completed pointer held at old SHA
        assert ref["git"]["target_commit"] == sha2
        assert ref["error"] == "bulk exploded"
        assert ref["failed_at"] is not None
        # No refs write during the whole failed run set git.commit to the candidate SHA.
        assert all(commit != sha2 for _status, commit in es.ref_write_history)

        # Retry (failure cleared): restore the real ingest and converge to the same view a
        # clean run would produce. Only the boom patch is reverted -- the autouse bulk/checkout
        # patches must stay in place.
        monkeypatch.setattr(command, "index_paths_v2", documents.index_paths_v2)
        _run(es, repo)
        ref = _ref_doc(es)
        assert ref["status"] == "ready"
        assert ref["git"]["commit"] == sha2
        assert ref["error"] is None and ref["failed_at"] is None
        assert make_doc_id(ORG, REPO, "branch", BRANCH, "a.txt", "2") in _lines(es)
        assert _file_id("b.txt") in _files(es)


class TestMissingBaseReconciliation:
    def test_unavailable_old_commit_triggers_full_rebuild(self, repo, monkeypatch):
        (repo / "a.txt").write_text("one\n")
        _commit(repo, "init")
        es = FakeES()
        _run(es, repo)

        # Pretend the completed base commit is gone: force a full-namespace delete + rebuild.
        monkeypatch.setattr(command, "base_commit_available", lambda repo_dir, sha: False, raising=False)
        # base_commit_available is used inside git.plan_changes; patch there.
        import sourcerer.commands.index.git as gitmod
        monkeypatch.setattr(gitmod, "base_commit_available", lambda repo_dir, sha: False)

        (repo / "a.txt").write_text("one\ntwo\n")
        (repo / "c.txt").write_text("added\n")
        sha2 = _commit(repo, "change")
        _run(es, repo)

        ref = _ref_doc(es)
        assert ref["status"] == "ready" and ref["git"]["commit"] == sha2
        # Full rebuild indexed every current file with current content.
        assert _file_id("a.txt") in _files(es)
        assert _file_id("c.txt") in _files(es)
        assert make_doc_id(ORG, REPO, "branch", BRANCH, "a.txt", "2") in _lines(es)


class TestRunConfigRouting:
    def test_snapshot_units_use_v1_path_incremental_units_use_v2_path(self, tmp_path, monkeypatch):
        import contextlib
        from unittest.mock import MagicMock
        from sourcerer.config import RepoConfig
        from sourcerer.progress import Unit

        snap = Unit(org=ORG, repo=REPO, ref="release", kind="branch", update_mode="snapshot")
        incr = Unit(org=ORG, repo=REPO, ref="main", kind="branch", update_mode="incremental")

        v1_calls: list[str] = []
        v2_calls: list[str] = []

        def fake_v1(es, org, repo, repo_dir, branch, tag, commit, force, reporter, unit):
            v1_calls.append(unit.ref)
            unit.status = "indexed"

        def fake_v2(es, org, repo, repo_dir, branch, force=False, reporter=None, unit=None):
            v2_calls.append(branch)
            unit.status = "indexed"

        @contextlib.contextmanager
        def fake_prepared_repo(org, repo, cache_root, ephemeral):
            yield tmp_path

        monkeypatch.setattr(command, "make_client", lambda *a, **k: MagicMock())
        monkeypatch.setattr(command, "_load_config",
                            lambda p: [RepoConfig(org=ORG, repo=REPO, selectors=[])])
        monkeypatch.setattr(command, "_resolve_entry", lambda entry: [snap, incr])
        monkeypatch.setattr(command, "prepared_repo", fake_prepared_repo)
        monkeypatch.setattr(command, "pre_clone_skip", lambda *a, **k: (False, "release", "sha"))
        monkeypatch.setattr(command, "_rev_info", lambda repo_dir, ref: ("sha", None))
        monkeypatch.setattr(command, "_effective_since_floor", lambda *a, **k: None)
        monkeypatch.setattr(command, "plan_repo", lambda *a, **k: [])
        monkeypatch.setattr(command, "ref_dates", lambda repo_dir: {})
        monkeypatch.setattr(command, "index_ref_in_dir", fake_v1)
        monkeypatch.setattr(command, "index_incremental_in_dir", fake_v2)

        command.run_config("unused.yml", "http://es", None, None, None, quiet=True)

        assert v1_calls == ["release"]  # snapshot ref went through the v1 path
        assert v2_calls == ["main"]     # incremental ref went through the v2 path


class TestForceFullReconciliation:
    def test_force_rebuilds_even_when_head_matches(self, repo):
        (repo / "a.txt").write_text("one\n")
        _commit(repo, "init")
        es = FakeES()
        _run(es, repo)
        history_len = len(es.ref_write_history)

        _run(es, repo, force=True)  # HEAD unchanged, but --force rebuilds anyway

        # A rebuild happened (indexing + ready), so history grew rather than a no-op skip.
        assert len(es.ref_write_history) > history_len
        assert _ref_doc(es)["status"] == "ready"
