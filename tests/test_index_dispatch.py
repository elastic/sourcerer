"""Tests for the ref-level concurrent dispatch in `run_config` (INDEX_REF_CONCURRENCY):
refs of one repo now check out into independent `git worktree` slots and index concurrently,
up to the configured cap, instead of strictly one-at-a-time against a single working tree.

Every git/ES side effect is mocked/patched -- these are orchestration tests of dispatch (task
grouping, slot handling, error isolation, abort), not an end-to-end index run. In particular
`index_ref_in_dir` and `index_incremental_branch_in_dir` (the actual checkout+ingest work) are
patched at the `command` module boundary, same convention as test_index_ref.py and
test_incremental_index.py."""

# Standard packages
import contextlib
import pathlib
import threading
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# App packages
from sourcerer.commands.index import command
from sourcerer.commands.index import runtime as runtimemod

_MOD = "sourcerer.commands.index.command"
_FAKE_REPO_DIR = pathlib.Path("/fake/repo")


def _write_config(tmp_path, org="myorg", repo="myrepo", ref_type="tag", match="v{major}.{minor}.{patch}"):
    """A minimal sourcerer.yml with one snapshot tag source, matching whatever ref names the
    test's patched `_resolve_entry` emits (real ref-matching runs in process_group's
    `_effective_since_floor` / `retain_doomed_ids`, so the config must be genuinely valid, not
    a mock)."""
    path = tmp_path / "sourcerer.yml"
    path.write_text(f"""\
sources:
- git:
    host: github
    org: {org}
    repo: {repo}
    ref_type: {ref_type}
  match: "{match}"
""")
    return str(path)


@contextlib.contextmanager
def _fake_prepared_repo(host, org, repo, clone_url, cache_root, ephemeral):
    yield _FAKE_REPO_DIR


def _patch_dispatch_internals(units_by_repo, rev_info_by_ref, ensure_worktree_mock=None):
    """Patch everything process_group touches EXCEPT the actual per-ref/per-unit dispatch
    logic under test. `units_by_repo` maps (host, org, repo) -> list[Unit] as Phase 1's
    `_resolve_entry` would have produced them. `rev_info_by_ref` maps ref name -> (sha, None),
    standing in for `_rev_info`'s (commit_sha, committer_date) result."""

    def fake_resolve_entry(entry, host):
        key = (entry.host, entry.org, entry.repo)
        return units_by_repo.get(key, []), []

    def fake_rev_info(repo_dir, ref):
        return rev_info_by_ref.get(ref)

    patchers = {
        "_resolve_entry": patch(f"{_MOD}._resolve_entry", side_effect=fake_resolve_entry),
        "prepared_repo": patch(f"{_MOD}.prepared_repo", _fake_prepared_repo),
        "ref_dates": patch(f"{_MOD}.ref_dates", return_value={}),
        "_rev_info": patch(f"{_MOD}._rev_info", side_effect=fake_rev_info),
        "ensure_worktree": patch(
            f"{_MOD}.ensure_worktree",
            ensure_worktree_mock or MagicMock(return_value=_FAKE_REPO_DIR),
        ),
        "_run_uniqueness_gate": patch(f"{_MOD}._run_uniqueness_gate", return_value=True),
    }
    return patchers


@contextlib.contextmanager
def _dispatch_harness(units_by_repo, rev_info_by_ref, ref_concurrency, index_ref_in_dir_mock,
                       ensure_worktree_mock=None):
    """Start every patch this module of tests needs, set INDEX_REF_CONCURRENCY, and yield
    nothing -- callers just invoke `command.run_config` inside the `with` block."""
    started = []
    try:
        runtimemod._tuning.cache_clear()
        with patch.dict("os.environ", {"INDEX_REF_CONCURRENCY": str(ref_concurrency)}):
            for p in _patch_dispatch_internals(units_by_repo, rev_info_by_ref, ensure_worktree_mock).values():
                started.append(p.start())
            started.append(patch(f"{_MOD}.index_ref_in_dir", index_ref_in_dir_mock).start())
            yield
    finally:
        patch.stopall()
        runtimemod._tuning.cache_clear()


def _unit(ref, host="github", org="myorg", repo="myrepo", kind="tag"):
    from sourcerer.progress import Unit
    return Unit(host=host, org=org, repo=repo, ref=ref, kind=kind)


def _run(config_path):
    # force=True: bypasses the batched/fallback "already indexed" pre-clone-skip path (which
    # would otherwise need markers_status_by_id/commits_with_content/fetch_markers against a
    # real Elasticsearch response shape) -- irrelevant to what these dispatch tests exercise.
    command.run_config(
        config_path, url="http://es.invalid:9200", api_key="x", username=None, password=None,
        force=True, quiet=True,
    )


class TestSameRepoRefsRunConcurrently:
    """The whole point of the change: two refs of ONE repo, at different commits, must be
    able to run at the same time -- not strictly one after another."""

    def test_two_refs_at_different_commits_overlap_when_cap_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(command, "make_client", lambda *a, **k: MagicMock())
        config_path = _write_config(tmp_path)
        units = [_unit("v1.0.0"), _unit("v2.0.0")]
        rev_info = {"v1.0.0": ("a" * 40, None), "v2.0.0": ("b" * 40, None)}

        barrier = threading.Barrier(2, timeout=5)

        def side_effect(*args, **kwargs):
            barrier.wait()  # both calls must arrive concurrently, or this times out

        mock = MagicMock(side_effect=side_effect)
        with _dispatch_harness({("github", "myorg", "myrepo"): units}, rev_info,
                                ref_concurrency=2, index_ref_in_dir_mock=mock):
            _run(config_path)

        assert mock.call_count == 2

    def test_two_refs_at_different_commits_never_overlap_when_cap_is_one(self, tmp_path, monkeypatch):
        """A cap of 1 must reproduce today's strictly-sequential behavior: forcing both calls
        to rendezvous on a 2-party barrier deadlocks (and times out) when only one can ever be
        in flight at a time -- the mirror image of the concurrent test above."""
        monkeypatch.setattr(command, "make_client", lambda *a, **k: MagicMock())
        config_path = _write_config(tmp_path)
        units = [_unit("v1.0.0"), _unit("v2.0.0")]
        rev_info = {"v1.0.0": ("a" * 40, None), "v2.0.0": ("b" * 40, None)}

        barrier = threading.Barrier(2, timeout=0.3)

        def side_effect(*args, **kwargs):
            barrier.wait()

        mock = MagicMock(side_effect=side_effect)
        with _dispatch_harness({("github", "myorg", "myrepo"): units}, rev_info,
                                ref_concurrency=1, index_ref_in_dir_mock=mock):
            with pytest.raises(threading.BrokenBarrierError):
                _run(config_path)


class TestSameCommitCoalescing:
    """Two refs resolving to the SAME commit must run one after another in the SAME task
    (sharing one worktree), preserving index_ref_in_dir's own sibling-reuse optimization --
    they must never be dispatched to two different worktree slots concurrently."""

    def test_same_commit_units_share_one_worktree_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(command, "make_client", lambda *a, **k: MagicMock())
        config_path = _write_config(tmp_path)
        units = [_unit("v1.0.0"), _unit("v1.0.1")]  # both resolve to the same commit
        same_sha = "c" * 40
        rev_info = {"v1.0.0": (same_sha, None), "v1.0.1": (same_sha, None)}

        seen_threads = []
        lock = threading.Lock()

        def side_effect(*args, **kwargs):
            with lock:
                seen_threads.append(threading.current_thread().ident)

        mock = MagicMock(side_effect=side_effect)
        ensure_worktree_mock = MagicMock(return_value=_FAKE_REPO_DIR)
        with _dispatch_harness({("github", "myorg", "myrepo"): units}, rev_info,
                                ref_concurrency=2, index_ref_in_dir_mock=mock,
                                ensure_worktree_mock=ensure_worktree_mock):
            _run(config_path)

        assert mock.call_count == 2
        # Coalesced into one task -> one worktree slot claimed for the whole commit group.
        assert ensure_worktree_mock.call_count == 1
        # Coalesced into one task -> both ran sequentially in the SAME worker thread.
        assert seen_threads[0] == seen_threads[1]


class TestErrorIsolation:
    """One ref's failure must be reported once and must not stop its siblings, matching the
    original per-unit try/except in the sequential loop."""

    def test_one_failing_ref_does_not_prevent_its_sibling_from_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr(command, "make_client", lambda *a, **k: MagicMock())
        config_path = _write_config(tmp_path)
        units = [_unit("v1.0.0"), _unit("v2.0.0")]
        rev_info = {"v1.0.0": ("a" * 40, None), "v2.0.0": ("b" * 40, None)}

        def side_effect(es, host, org, repo, worktree_dir, branch, tag, commit, *a, **k):
            if tag == "v1.0.0":
                raise ValueError("boom")

        mock = MagicMock(side_effect=side_effect)
        with _dispatch_harness({("github", "myorg", "myrepo"): units}, rev_info,
                                ref_concurrency=2, index_ref_in_dir_mock=mock):
            with pytest.raises(SystemExit) as exc:
                _run(config_path)

        assert exc.value.code == 1
        called_tags = {c.args[6] for c in mock.call_args_list}
        assert called_tags == {"v1.0.0", "v2.0.0"}


class TestAbort:
    """A mid-run abort must cancel refs that haven't started yet rather than waiting for the
    whole backlog to drain (see command._drain_futures)."""

    def test_abort_lets_in_flight_finish_and_cancels_the_rest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(command, "make_client", lambda *a, **k: MagicMock())
        config_path = _write_config(tmp_path)
        # A single-worker cap so the second/third refs are still queued (not yet running) when
        # the first one sets the abort flag -- exercising _drain_futures' cancel-pending path.
        units = [_unit("v1.0.0"), _unit("v2.0.0"), _unit("v3.0.0")]
        rev_info = {
            "v1.0.0": ("a" * 40, None), "v2.0.0": ("b" * 40, None), "v3.0.0": ("c" * 40, None),
        }
        started_first = threading.Event()

        def side_effect(es, host, org, repo, worktree_dir, branch, tag, commit, *a, **k):
            if tag == "v1.0.0":
                runtimemod._aborted.set()
                started_first.set()

        mock = MagicMock(side_effect=side_effect)
        try:
            with _dispatch_harness({("github", "myorg", "myrepo"): units}, rev_info,
                                    ref_concurrency=1, index_ref_in_dir_mock=mock):
                _run(config_path)
        finally:
            runtimemod._aborted.clear()

        assert started_first.is_set()
        # Only the ref that was already running when the abort fired got a chance to run;
        # the rest were cancelled before they ever reached index_ref_in_dir.
        assert mock.call_count == 1
