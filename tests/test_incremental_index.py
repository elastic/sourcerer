"""Tests for the incremental (ref-addressed) branch orchestration in
sourcerer.commands.index.command.index_incremental_branch_in_dir: the two-phase
indexing -> complete publication, full rebuild vs delta update selection, and failure handling.
Every ES call and every git/documents side effect is mocked/patched -- these are orchestration
tests, not an end-to-end index run (see specs/incremental-indexing.md Task 16 for that)."""

# Standard packages
from unittest.mock import MagicMock, patch

# App packages
from sourcerer.commands.index.command import index_incremental_branch_in_dir
from sourcerer.commands.index.git import ChangePlan
from sourcerer.progress import ProgressReporter, Unit

OLD = "1111111111111111111111111111111111111111"
NEW = "2222222222222222222222222222222222222222"


def _patch_common(prior=None, plan=None):
    """Patch every git/documents/markers side effect index_incremental_branch_in_dir calls,
    returning the patcher context managers as a dict of MagicMocks keyed by name."""
    patchers = {
        "checkout_branch": patch("sourcerer.commands.index.command.checkout_branch"),
        "resolve_commit": patch("sourcerer.commands.index.command.resolve_commit", return_value=NEW),
        "commit_date": patch("sourcerer.commands.index.command.commit_date", return_value="2026-01-01T00:00:00+00:00"),
        "read_incremental_ref": patch("sourcerer.commands.index.command.read_incremental_ref", return_value=prior),
        "plan_changes": patch("sourcerer.commands.index.command.plan_changes", return_value=plan or ChangePlan()),
        "delete_incremental_branch": patch("sourcerer.commands.index.command.delete_incremental_branch"),
        "delete_incremental_paths": patch("sourcerer.commands.index.command.delete_incremental_paths"),
        "index_incremental_paths": patch("sourcerer.commands.index.command.index_incremental_paths", return_value=(3, 30)),
        "count_tracked_files": patch("sourcerer.commands.index.command.count_tracked_files", return_value=3),
        "refresh_incremental_content": patch("sourcerer.commands.index.command.refresh_incremental_content"),
        "count_incremental_branch_docs": patch(
            "sourcerer.commands.index.command.count_incremental_branch_docs", return_value=(3, 30)
        ),
        "write_incremental_indexing": patch("sourcerer.commands.index.command.write_incremental_indexing"),
        "write_incremental_ready": patch("sourcerer.commands.index.command.write_incremental_ready"),
        "write_incremental_failed": patch("sourcerer.commands.index.command.write_incremental_failed"),
    }
    mocks = {name: p.start() for name, p in patchers.items()}
    return patchers, mocks


def _stop(patchers):
    for p in patchers.values():
        p.stop()


class TestIncrementalIndexFirstRun:
    def test_first_index_does_full_rebuild(self):
        patchers, mocks = _patch_common(prior=None)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental")
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            mocks["delete_incremental_branch"].assert_called_once()
            mocks["index_incremental_paths"].assert_called_once()
            # rel_paths (4th positional after repo_dir/branch) is None -> full tree walk.
            call_args = mocks["index_incremental_paths"].call_args
            assert call_args[0][6] is None
            mocks["delete_incremental_paths"].assert_not_called()
            mocks["write_incremental_ready"].assert_called_once()
            assert mocks["write_incremental_ready"].call_args[0][5] == NEW
        finally:
            _stop(patchers)


class TestIncrementalIndexDeltaRun:
    def test_second_run_indexes_only_changed_paths(self):
        prior = {"git": {"commit": OLD}}
        plan = ChangePlan(delete_paths=["gone.txt"], index_paths=["new.txt"])
        patchers, mocks = _patch_common(prior=prior, plan=plan)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental")
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            mocks["delete_incremental_branch"].assert_not_called()
            mocks["delete_incremental_paths"].assert_called_once()
            assert mocks["delete_incremental_paths"].call_args[0][5] == ["gone.txt"]
            mocks["index_incremental_paths"].assert_called_once()
            call_args = mocks["index_incremental_paths"].call_args
            assert call_args[0][6] == ["new.txt"]
            mocks["write_incremental_ready"].assert_called_once()
        finally:
            _stop(patchers)

    def test_missing_diff_base_triggers_full_rebuild(self):
        prior = {"git": {"commit": OLD}}
        plan = ChangePlan(base_missing=True)
        patchers, mocks = _patch_common(prior=prior, plan=plan)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental")
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            mocks["delete_incremental_branch"].assert_called_once()
            call_args = mocks["index_incremental_paths"].call_args
            assert call_args[0][6] is None
        finally:
            _stop(patchers)

    def test_no_change_skips_entirely(self):
        prior = {"git": {"commit": NEW}}  # already at the new (checked-out) commit
        patchers, mocks = _patch_common(prior=prior)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental")
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            mocks["index_incremental_paths"].assert_not_called()
            mocks["write_incremental_indexing"].assert_not_called()
            mocks["write_incremental_ready"].assert_not_called()
            assert unit.status == "no-changes"
        finally:
            _stop(patchers)


class TestIncrementalIndexFailure:
    def test_failed_run_does_not_advance_commit(self):
        prior = {"git": {"commit": OLD}}
        plan = ChangePlan(delete_paths=[], index_paths=["a.txt"])
        patchers, mocks = _patch_common(prior=prior, plan=plan)
        mocks["index_incremental_paths"].side_effect = RuntimeError("bulk failed")
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental")
            try:
                index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                                reporter=ProgressReporter(), unit=unit)
                assert False, "expected RuntimeError to propagate"
            except RuntimeError:
                pass
            mocks["write_incremental_ready"].assert_not_called()
            mocks["write_incremental_failed"].assert_called_once()
            # The completed pointer stays at OLD -- a failed run must not advance it (INV-006).
            assert mocks["write_incremental_failed"].call_args.kwargs["completed_commit"] == OLD
        finally:
            _stop(patchers)


class TestIncrementalIndexRoutingMigration:
    """When index.level or index.suffix changes on an already-indexed branch the run must:
    1. NOT return no-changes even when the commit hasn't advanced.
    2. Do a full rebuild into the NEW routing.
    3. After the ready marker flips, delete the old-routing copy (write-new -> flip -> delete-old).
    """

    def _prior_at_routing(self, level="repo", suffix=None):
        """A completed incremental join doc recorded at the given routing."""
        doc = {"git": {"commit": NEW}, "index_level": level}
        if suffix is not None:
            doc["index_suffix"] = suffix
        return doc

    def test_suffix_change_forces_full_rebuild_and_old_copy_delete(self):
        """repo -> repo^deploy: full rebuild at new routing, then delete at old routing."""
        prior = self._prior_at_routing(level="repo", suffix=None)
        patchers, mocks = _patch_common(prior=prior)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental", index_level="repo", index_suffix="deploy")
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            # Full rebuild path: delete_incremental_branch called at new routing, full tree indexed.
            assert mocks["delete_incremental_branch"].call_count == 2, (
                "Expected 2 calls to delete_incremental_branch: one for new routing (rebuild), "
                "one for old routing (migration cleanup)"
            )
            call_kwargs_list = [c.kwargs for c in mocks["delete_incremental_branch"].call_args_list]
            # First call: full rebuild at new (repo^deploy) routing.
            assert call_kwargs_list[0].get("index_level") == "repo"
            assert call_kwargs_list[0].get("index_suffix") == "deploy"
            # Second call: delete old (repo, no suffix) routing.
            assert call_kwargs_list[1].get("index_level") == "repo"
            assert call_kwargs_list[1].get("index_suffix") is None
            # Ready marker was published.
            mocks["write_incremental_ready"].assert_called_once()
        finally:
            _stop(patchers)

    def test_level_change_forces_full_rebuild_and_old_copy_delete(self):
        """repo -> org level: full rebuild at org routing, then delete at repo routing."""
        prior = self._prior_at_routing(level="repo", suffix=None)
        patchers, mocks = _patch_common(prior=prior)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental", index_level="org", index_suffix=None)
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            assert mocks["delete_incremental_branch"].call_count == 2
            call_kwargs_list = [c.kwargs for c in mocks["delete_incremental_branch"].call_args_list]
            assert call_kwargs_list[0].get("index_level") == "org"
            assert call_kwargs_list[1].get("index_level") == "repo"
            mocks["write_incremental_ready"].assert_called_once()
        finally:
            _stop(patchers)

    def test_no_changes_with_routing_change_still_migrates(self):
        """Commit unchanged but routing changed: must NOT return no-changes; must migrate."""
        # prior already at NEW sha, but at old routing
        prior = self._prior_at_routing(level="repo", suffix=None)
        prior["git"]["commit"] = NEW  # same commit as what resolve_commit returns
        patchers, mocks = _patch_common(prior=prior)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental", index_level="repo", index_suffix="v2")
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            # Must NOT skip even though old_sha == new_sha.
            mocks["write_incremental_indexing"].assert_called_once()
            mocks["write_incremental_ready"].assert_called_once()
            # Old routing must be cleaned up.
            assert mocks["delete_incremental_branch"].call_count == 2
        finally:
            _stop(patchers)

    def test_same_routing_no_old_copy_delete(self):
        """When routing is unchanged a delta run must not call delete_incremental_branch at all."""
        prior = self._prior_at_routing(level="repo", suffix=None)
        plan = ChangePlan(delete_paths=[], index_paths=["changed.txt"])
        patchers, mocks = _patch_common(prior=prior, plan=plan)
        try:
            es = MagicMock()
            unit = Unit(host="github", org="acme", repo="widgets", ref="main", kind="branch",
                       index_strategy="incremental", index_level="repo", index_suffix=None)
            index_incremental_branch_in_dir(es, "github", "acme", "widgets", "/repo", "main",
                                            reporter=ProgressReporter(), unit=unit)
            # Delta run: no full rebuild (delete_incremental_branch not called), no extra delete.
            mocks["delete_incremental_branch"].assert_not_called()
        finally:
            _stop(patchers)
