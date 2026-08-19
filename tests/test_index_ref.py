"""Unit tests for index_ref_in_dir's dedup/reuse branch (Branch B).

All git and ES helpers are patched at the sourcerer.commands.index.command module boundary so
the tests exercise the control-flow logic without a live repo or ES cluster. The focus is the
interaction between fully_indexed_counts / content_present and whether index_repo (full ingest)
is called or bypassed, and what counts end up in write_ref_marker."""

# Standard packages
import pathlib
from unittest.mock import MagicMock, call, patch

# App packages
from sourcerer.commands.index.command import index_ref_in_dir

FULL_SHA = "bb40575aeef2cee230ddc175862866348bd3143f"
COMMIT_DATE = "2026-08-11T18:24:23Z"

# The module path where the helpers are imported (the bound names in command.py).
_MOD = "sourcerer.commands.index.command"


def _make_es() -> MagicMock:
    return MagicMock()


def _make_repo_dir() -> pathlib.Path:
    return pathlib.Path("/tmp/fake-repo")


def _patch_git(branch: str = "main", commit_sha: str = FULL_SHA, commit_date: str = COMMIT_DATE):
    """Return a context manager that patches the git helpers used by index_ref_in_dir."""
    patches = [
        patch(f"{_MOD}.checkout_branch"),
        patch(f"{_MOD}.checkout_ref"),
        patch(f"{_MOD}.default_branch", return_value=branch),
        patch(f"{_MOD}.resolve_commit", return_value=commit_sha),
        patch(f"{_MOD}.commit_date", return_value=commit_date),
        patch(f"{_MOD}.count_tracked_files", return_value=50),
    ]
    return patches


class TestBranchBReusePath:
    """A complete sibling marker exists and content is present -> reuse the marker's counts."""

    def test_reuse_writes_marker_counts_not_index_repo(self):
        es = _make_es()
        repo_dir = _make_repo_dir()

        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=None),
            patch(f"{_MOD}.fully_indexed_counts", return_value=(7, 900)) as mock_fic,
            patch(f"{_MOD}.content_present", return_value=True) as mock_cp,
            patch(f"{_MOD}.index_repo") as mock_index,
            patch(f"{_MOD}.write_indexing_marker") as mock_wim,
            patch(f"{_MOD}.write_ref_marker") as mock_wrm,
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
        ):
            index_ref_in_dir(es, "github", "elastic", "myrepo", repo_dir, branch="main")

        mock_fic.assert_called_once_with(es, "github", "elastic", "myrepo", FULL_SHA)
        # content_present is now location-aware: at_index is the repo-level default name (routing
        # unchanged), so the reuse probe targets exactly where this commit's content lives.
        mock_cp.assert_called_once_with(
            es, "github", "elastic", "myrepo", FULL_SHA,
            at_index="sourcerer-v3-files~github~elastic~myrepo",
        )
        mock_index.assert_not_called()
        mock_wim.assert_not_called()  # indexing marker only written for fresh ingest
        mock_wrm.assert_called_once()
        _, kwargs = mock_wrm.call_args
        positional = mock_wrm.call_args.args
        # write_ref_marker(es, host, org, repo, ref_type, ref, commit_sha, commit_date, fc, lc)
        assert positional[-2] == 7    # files_count
        assert positional[-1] == 900  # lines_count

    def test_reuse_for_tag_sets_tagged_status(self):
        """When a tag triggers the reuse path the reporter gets status 'tagged'."""
        es = _make_es()
        repo_dir = _make_repo_dir()
        reporter = MagicMock()
        reporter.finish = MagicMock()

        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=None),
            patch(f"{_MOD}.fully_indexed_counts", return_value=(3, 100)),
            patch(f"{_MOD}.content_present", return_value=True),
            patch(f"{_MOD}.index_repo"),
            patch(f"{_MOD}.write_indexing_marker"),
            patch(f"{_MOD}.write_ref_marker"),
            patch(f"{_MOD}.checkout_ref"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
        ):
            index_ref_in_dir(
                es, "github", "elastic", "myrepo", repo_dir, tag="v1.0.0", reporter=reporter,
            )

        status_call = reporter.finish.call_args
        assert status_call.args[1] == "tagged"

    def test_reuse_for_branch_sets_recorded_status(self):
        es = _make_es()
        repo_dir = _make_repo_dir()
        reporter = MagicMock()

        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=None),
            patch(f"{_MOD}.fully_indexed_counts", return_value=(7, 900)),
            patch(f"{_MOD}.content_present", return_value=True),
            patch(f"{_MOD}.index_repo"),
            patch(f"{_MOD}.write_indexing_marker"),
            patch(f"{_MOD}.write_ref_marker"),
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
        ):
            index_ref_in_dir(
                es, "github", "elastic", "myrepo", repo_dir, branch="main", reporter=reporter,
            )

        status_call = reporter.finish.call_args
        assert status_call.args[1] == "recorded"


class TestBranchBGCPath:
    """A complete sibling marker exists but content was GC'd -> must fall through to re-ingest."""

    def test_gcd_content_triggers_ingest_not_reuse(self):
        es = _make_es()
        repo_dir = _make_repo_dir()

        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=None),
            patch(f"{_MOD}.fully_indexed_counts", return_value=(7, 900)),
            patch(f"{_MOD}.content_present", return_value=False),  # GC'd
            patch(f"{_MOD}.index_repo", return_value=(7, 900)) as mock_index,
            patch(f"{_MOD}.write_indexing_marker"),
            patch(f"{_MOD}.write_ref_marker") as mock_wrm,
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
            patch(f"{_MOD}.count_tracked_files", return_value=50),
        ):
            index_ref_in_dir(es, "github", "elastic", "myrepo", repo_dir, branch="main")

        mock_index.assert_called_once()
        positional = mock_wrm.call_args.args
        assert positional[-2] == 7    # counts from re-ingest
        assert positional[-1] == 900


class TestBranchBNoSiblingMarker:
    """No complete sibling marker -> always ingests (Branch A)."""

    def test_no_sibling_marker_triggers_ingest(self):
        es = _make_es()
        repo_dir = _make_repo_dir()

        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=None),
            patch(f"{_MOD}.fully_indexed_counts", return_value=None),
            patch(f"{_MOD}.content_present") as mock_cp,
            patch(f"{_MOD}.index_repo", return_value=(12, 1500)) as mock_index,
            patch(f"{_MOD}.write_indexing_marker") as mock_wim,
            patch(f"{_MOD}.write_ref_marker") as mock_wrm,
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
            patch(f"{_MOD}.count_tracked_files", return_value=50),
        ):
            index_ref_in_dir(es, "github", "elastic", "myrepo", repo_dir, branch="main")

        mock_cp.assert_not_called()  # no need to probe content when marker absent
        mock_index.assert_called_once()
        mock_wim.assert_called_once()  # indexing marker written before ingest
        positional = mock_wrm.call_args.args
        assert positional[-2] == 12
        assert positional[-1] == 1500


class TestBranchBForce:
    """--force bypasses the reuse check entirely."""

    def test_force_skips_fully_indexed_counts_and_ingests(self):
        es = _make_es()
        repo_dir = _make_repo_dir()

        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=None),
            patch(f"{_MOD}.fully_indexed_counts") as mock_fic,
            patch(f"{_MOD}.content_present") as mock_cp,
            patch(f"{_MOD}.index_repo", return_value=(5, 600)) as mock_index,
            patch(f"{_MOD}.write_indexing_marker"),
            patch(f"{_MOD}.write_ref_marker"),
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
            patch(f"{_MOD}.count_tracked_files", return_value=50),
        ):
            index_ref_in_dir(
                es, "github", "elastic", "myrepo", repo_dir, branch="main", force=True,
            )

        mock_fic.assert_not_called()
        mock_cp.assert_not_called()
        mock_index.assert_called_once()


class TestIndexLevelSuffixMigration:
    """index.level/suffix change: a prior complete marker recorded a different routing, so the ref
    is re-ingested at the new index, the marker is flipped, then the old copy is deleted (in that
    order)."""

    def test_suffix_added_migrates_and_cleans_old_copy(self):
        es = _make_es()
        repo_dir = _make_repo_dir()
        call_order = []

        def track_wrm(*a, **k):
            call_order.append("flip-marker")

        def track_delete(es_, host, org, repo, sha, index_names):
            call_order.append(("delete-old", tuple(index_names)))

        with (
            # Prior marker recorded the default repo-level routing; the run now wants suffix "deploy".
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=("repo", None)),
            patch(f"{_MOD}.fully_indexed_counts", return_value=None),
            patch(f"{_MOD}.content_present", return_value=False),
            patch(f"{_MOD}.index_repo", return_value=(4, 40)) as mock_index,
            patch(f"{_MOD}.write_indexing_marker"),
            patch(f"{_MOD}.write_ref_marker", side_effect=track_wrm) as mock_wrm,
            patch(f"{_MOD}.delete_commit_from_indices", side_effect=track_delete) as mock_del,
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
            patch(f"{_MOD}.count_tracked_files", return_value=50),
        ):
            index_ref_in_dir(
                es, "github", "elastic", "myrepo", repo_dir, branch="main",
                index_level="repo", index_suffix="deploy",
            )

        # Re-ingest happened at the new location.
        mock_index.assert_called_once()
        _, ikwargs = mock_index.call_args
        assert ikwargs["index_level"] == "repo" and ikwargs["index_suffix"] == "deploy"

        # Marker flipped to the new routing.
        _, wkwargs = mock_wrm.call_args
        assert wkwargs["index_level"] == "repo" and wkwargs["index_suffix"] == "deploy"

        # Old (unsuffixed) copy deleted, AFTER the marker flip.
        mock_del.assert_called_once()
        assert call_order.index("flip-marker") < next(
            i for i, c in enumerate(call_order) if isinstance(c, tuple)
        )
        deleted_indices = mock_del.call_args.args[5]
        assert "sourcerer-v3-files~github~elastic~myrepo" in deleted_indices
        assert "sourcerer-v3-lines~github~elastic~myrepo" in deleted_indices

    def test_unchanged_routing_does_not_delete(self):
        """Same routing as recorded -> normal ingest, no migration delete."""
        es = _make_es()
        repo_dir = _make_repo_dir()
        with (
            patch(f"{_MOD}.should_index", return_value=True),
            patch(f"{_MOD}.recorded_routing", return_value=("repo", None)),
            patch(f"{_MOD}.fully_indexed_counts", return_value=None),
            patch(f"{_MOD}.content_present", return_value=False),
            patch(f"{_MOD}.index_repo", return_value=(4, 40)),
            patch(f"{_MOD}.write_indexing_marker"),
            patch(f"{_MOD}.write_ref_marker"),
            patch(f"{_MOD}.delete_commit_from_indices") as mock_del,
            patch(f"{_MOD}.checkout_branch"),
            patch(f"{_MOD}.resolve_commit", return_value=FULL_SHA),
            patch(f"{_MOD}.commit_date", return_value=COMMIT_DATE),
            patch(f"{_MOD}.count_tracked_files", return_value=50),
        ):
            index_ref_in_dir(es, "github", "elastic", "myrepo", repo_dir, branch="main")
        mock_del.assert_not_called()
