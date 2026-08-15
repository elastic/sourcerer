"""Unit tests for sourcerer.commands.index.markers: the per-ref and batched idempotency
helpers. Every ES call is mocked."""

# Standard packages
import datetime
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import (
    ERROR_MAX_LEN,
    build_ref_id,
    commit_prefix_indexed,
    commits_with_content,
    count_incremental_branch_docs,
    delete_incremental_branch,
    delete_incremental_paths,
    fully_indexed_counts,
    markers_status_by_id,
    _needs_index,
    _parse_marker_started,
    pre_clone_skip,
    read_incremental_ref,
    should_index,
    write_incremental_failed,
    write_incremental_indexing,
    write_incremental_ready,
    write_ref_marker,
    write_snapshot_join_doc,
)
from sourcerer.indices import FILES_ALIAS, REFS_ALIAS, REFS_INDEX
from sourcerer.utils import build_ref_key

FULL_SHA = "cfefb3b2378ccbadefa7c8f4f9e21b3a1d2e5f60"

_UTC = datetime.timezone.utc


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


def _search_hits(full_sha: str | None) -> dict:
    hits = [{"_source": {"git": {"commit": full_sha}}}] if full_sha else []
    return {"hits": {"hits": hits}}


class TestCommitPrefixIndexed:
    def test_hit_returns_full_sha(self):
        es = MagicMock()
        es.search.return_value = _search_hits(FULL_SHA)
        assert commit_prefix_indexed(es, "github", "acme", "widgets", "cfefb3b") == FULL_SHA
        query = es.search.call_args.kwargs["query"]
        assert es.search.call_args.kwargs["index"] == REFS_ALIAS
        assert {"prefix": {"git.commit": "cfefb3b"}} in query["bool"]["filter"]
        assert {"term": {"status": "complete"}} in query["bool"]["filter"]
        assert {"term": {"git.host": "github"}} in query["bool"]["filter"]
        assert {"term": {"git.org": "acme"}} in query["bool"]["filter"]
        assert {"term": {"git.repo": "widgets"}} in query["bool"]["filter"]

    def test_no_hit_returns_none(self):
        es = MagicMock()
        es.search.return_value = _search_hits(None)
        assert commit_prefix_indexed(es, "github", "acme", "widgets", "cfefb3b") is None

    def test_missing_index_returns_none_not_raise(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert commit_prefix_indexed(es, "github", "acme", "widgets", "cfefb3b") is None


class TestPreCloneSkipCommit:
    def test_force_bypasses_without_querying(self):
        es = MagicMock()
        skip, ref_for_id, remote_sha = pre_clone_skip(
            es, "github", "acme", "widgets", None, None, "cfefb3b", "https://x", True,
        )
        assert (skip, ref_for_id, remote_sha) == (False, None, None)
        es.search.assert_not_called()

    def test_matching_marker_skips_the_clone(self):
        es = MagicMock()
        es.search.return_value = _search_hits(FULL_SHA)
        skip, ref_for_id, remote_sha = pre_clone_skip(
            es, "github", "acme", "widgets", None, None, "CFEFB3B", "https://x", False,
        )
        assert skip is True
        assert ref_for_id == FULL_SHA
        assert remote_sha == FULL_SHA

    def test_no_matching_marker_falls_through_to_clone(self):
        es = MagicMock()
        es.search.return_value = _search_hits(None)
        skip, ref_for_id, remote_sha = pre_clone_skip(
            es, "github", "acme", "widgets", None, None, "cfefb3b", "https://x", False,
        )
        assert (skip, ref_for_id, remote_sha) == (False, None, None)


class TestShouldIndex:
    def test_reads_marker_by_searching_the_refs_alias(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [{"_source": {"status": "complete"}}]}}

        assert should_index(es, "github", "acme", "widgets", "branch", "main", FULL_SHA) is False

        assert es.search.call_args.kwargs == {
            "index": REFS_ALIAS,
            "size": 1,
            "query": {"ids": {"values": [build_ref_id("github", "acme", "widgets", "branch", "main", FULL_SHA)]}},
        }

    def test_host_changes_ref_id(self):
        assert build_ref_id("github", "acme", "widgets", "branch", "main", FULL_SHA) != \
            build_ref_id("gitlab", "acme", "widgets", "branch", "main", FULL_SHA)

    def _es_with_marker(self, status, started_at=None):
        """Return a mock ES that returns a marker with the given status and indexing_started_at."""
        es = MagicMock()
        source = {"status": status}
        if started_at is not None:
            source["indexing_started_at"] = started_at
        es.search.return_value = {"hits": {"hits": [{"_source": source}]}}
        return es

    def test_indexing_within_window_returns_false(self):
        # An 'indexing' marker started recently -> another run is active -> skip.
        window = datetime.timedelta(hours=1)
        now = datetime.datetime.now(_UTC)
        started = (now - datetime.timedelta(minutes=10)).isoformat()
        es = self._es_with_marker("indexing", started_at=started)
        result = should_index(es, "github", "acme", "w", "branch", "main", FULL_SHA, retry_window=window)
        assert result is False

    def test_indexing_older_than_window_returns_true(self):
        # A stale 'indexing' marker -> stuck run -> must re-index.
        window = datetime.timedelta(hours=1)
        now = datetime.datetime.now(_UTC)
        started = (now - datetime.timedelta(hours=2)).isoformat()
        es = self._es_with_marker("indexing", started_at=started)
        result = should_index(es, "github", "acme", "w", "branch", "main", FULL_SHA, retry_window=window)
        assert result is True

    def test_indexing_missing_started_at_returns_true(self):
        # No indexing_started_at -> treat as stuck -> must index.
        window = datetime.timedelta(hours=1)
        es = self._es_with_marker("indexing")   # no started_at
        result = should_index(es, "github", "acme", "w", "branch", "main", FULL_SHA, retry_window=window)
        assert result is True

    def test_indexing_no_retry_window_returns_true(self):
        # Default (no retry_window) -> any non-complete status -> must index.
        now = datetime.datetime.now(_UTC)
        started = (now - datetime.timedelta(minutes=5)).isoformat()
        es = self._es_with_marker("indexing", started_at=started)
        result = should_index(es, "github", "acme", "w", "branch", "main", FULL_SHA)
        assert result is True

    def test_es_query_shape_unchanged(self):
        # should_index must NOT change the ES query -- a test asserts exact kwargs.
        window = datetime.timedelta(hours=1)
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [{"_source": {"status": "complete"}}]}}
        should_index(es, "github", "acme", "widgets", "branch", "main", FULL_SHA, retry_window=window)
        assert es.search.call_args.kwargs == {
            "index": REFS_ALIAS,
            "size": 1,
            "query": {"ids": {"values": [build_ref_id("github", "acme", "widgets", "branch", "main", FULL_SHA)]}},
        }


class TestMarkersStatusById:
    def test_returns_status_and_commit_for_found_ids(self):
        es = MagicMock()
        ref_id = build_ref_id("github", "acme", "widgets", "branch", "main", FULL_SHA)
        es.search.return_value = {
            "hits": {"hits": [
                {"_id": ref_id, "_source": {"status": "complete", "git": {"commit": FULL_SHA}}}
            ]}
        }
        result = markers_status_by_id(es, [ref_id])
        assert result == {ref_id: {"status": "complete", "commit": FULL_SHA, "indexing_started_at": None,
                                   "index_level": None, "index_suffix": None}}
        call = es.search.call_args.kwargs
        assert call["index"] == REFS_ALIAS
        assert call["query"] == {"ids": {"values": [ref_id]}}
        assert "status" in call["source_includes"]
        assert "git.commit" in call["source_includes"]
        assert "indexing_started_at" in call["source_includes"]

    def test_returns_indexing_started_at_for_indexing_marker(self):
        es = MagicMock()
        ref_id = build_ref_id("github", "acme", "widgets", "branch", "main", FULL_SHA)
        started = "2026-08-09T17:14:03.000000+00:00"
        es.search.return_value = {
            "hits": {"hits": [
                {"_id": ref_id, "_source": {
                    "status": "indexing", "git": {"commit": FULL_SHA},
                    "indexing_started_at": started,
                }}
            ]}
        }
        result = markers_status_by_id(es, [ref_id])
        assert result == {ref_id: {"status": "indexing", "commit": FULL_SHA, "indexing_started_at": started,
                                   "index_level": None, "index_suffix": None}}

    def test_missing_id_is_absent_from_result(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": []}}
        result = markers_status_by_id(es, ["no-such-id"])
        assert result == {}

    def test_missing_index_returns_empty_dict(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        result = markers_status_by_id(es, ["some-id"])
        assert result == {}

    def test_empty_input_returns_empty_without_querying(self):
        es = MagicMock()
        result = markers_status_by_id(es, [])
        assert result == {}
        es.search.assert_not_called()


class TestCommitsWithContent:
    def test_returns_commits_present_in_files_alias(self):
        es = MagicMock()
        es.search.return_value = {
            "aggregations": {"present": {"buckets": [{"key": FULL_SHA}]}}
        }
        result = commits_with_content(es, "github", "acme", "widgets", {FULL_SHA})
        assert result == {FULL_SHA}
        call = es.search.call_args.kwargs
        assert call["index"] == FILES_ALIAS
        assert call["size"] == 0
        assert {"terms": {"git.commit": [FULL_SHA]}} in call["query"]["bool"]["filter"]

    def test_commit_absent_from_content_not_in_result(self):
        es = MagicMock()
        es.search.return_value = {"aggregations": {"present": {"buckets": []}}}
        result = commits_with_content(es, "github", "acme", "widgets", {FULL_SHA})
        assert result == set()

    def test_missing_index_returns_empty_set(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        result = commits_with_content(es, "github", "acme", "widgets", {FULL_SHA})
        assert result == set()

    def test_empty_input_returns_empty_without_querying(self):
        es = MagicMock()
        result = commits_with_content(es, "github", "acme", "widgets", set())
        assert result == set()
        es.search.assert_not_called()


class TestNeedsIndex:
    """Tests for the pure per-ref skip decision (_needs_index)."""

    SHA = FULL_SHA
    ID = build_ref_id("github", "acme", "repo", "branch", "main", FULL_SHA)

    def test_missing_from_status_map_needs_indexing(self):
        assert _needs_index(self.ID, self.SHA, {}, set()) is True

    def test_incomplete_status_needs_indexing_no_cutoff(self):
        # Without a cutoff (default None), any non-complete status -> must index (back-compat).
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "indexing", "commit": self.SHA}}, {self.SHA}) is True

    def test_incomplete_status_needs_indexing(self):
        # Alias retained for back-compat: same as above with explicit args.
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "indexing", "commit": self.SHA}}, {self.SHA}) is True

    def test_complete_with_content_present_is_skipped(self):
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete", "commit": self.SHA}}, {self.SHA}) is False

    def test_complete_but_content_gcd_out_needs_indexing(self):
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete", "commit": self.SHA}}, set()) is True

    def test_falls_back_to_remote_sha_when_commit_missing_from_marker(self):
        # A marker without a 'commit' field falls back to remote_sha for the content check.
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete"}}, {self.SHA}) is False
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete"}}, set()) is True

    # --- retry-window aware tests ---

    def _marker(self, started_at):
        """Helper: build a status:indexing marker dict with the given indexing_started_at."""
        return {self.ID: {"status": "indexing", "commit": self.SHA, "indexing_started_at": started_at}}

    def test_indexing_within_window_is_skipped(self):
        # A fresh 'indexing' marker within the window -> skip (another run is active).
        cutoff = datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)
        started = "2026-08-09T17:10:00+00:00"   # 10 min after cutoff -> within window
        assert _needs_index(self.ID, self.SHA, self._marker(started), set(), cutoff) is False

    def test_indexing_older_than_window_needs_reindex(self):
        # A stale 'indexing' marker (older than cutoff) -> must re-index.
        cutoff = datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)
        started = "2026-08-09T16:55:00+00:00"   # 5 min before cutoff -> stuck
        assert _needs_index(self.ID, self.SHA, self._marker(started), set(), cutoff) is True

    def test_indexing_at_exact_cutoff_boundary_is_skipped(self):
        # Boundary is inclusive (>= cutoff): started == cutoff -> skip.
        cutoff = datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)
        started = "2026-08-09T17:00:00+00:00"
        assert _needs_index(self.ID, self.SHA, self._marker(started), set(), cutoff) is False

    def test_indexing_missing_started_at_with_cutoff_needs_reindex(self):
        # No indexing_started_at in marker -> treat as unknown/stuck -> must index.
        cutoff = datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)
        marker = {self.ID: {"status": "indexing", "commit": self.SHA, "indexing_started_at": None}}
        assert _needs_index(self.ID, self.SHA, marker, set(), cutoff) is True

    def test_indexing_malformed_started_at_needs_reindex(self):
        # Malformed started_at -> treat as unknown/stuck -> must index.
        cutoff = datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)
        marker = {self.ID: {"status": "indexing", "commit": self.SHA, "indexing_started_at": "not-a-date"}}
        assert _needs_index(self.ID, self.SHA, marker, set(), cutoff) is True

    def test_indexing_naive_iso_within_window_is_skipped(self):
        # A naive (tz-unaware) ISO string is coerced to UTC before comparison -- no TypeError.
        cutoff = datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)
        started = "2026-08-09T17:10:00"   # no tz suffix -> naive
        assert _needs_index(self.ID, self.SHA, self._marker(started), set(), cutoff) is False


class TestParseMarkerStarted:
    """Tests for the _parse_marker_started helper."""

    def test_none_returns_none(self):
        assert _parse_marker_started(None) is None

    def test_malformed_string_returns_none(self):
        assert _parse_marker_started("not-a-date") is None

    def test_aware_iso_returns_datetime(self):
        result = _parse_marker_started("2026-08-09T17:00:00+00:00")
        assert result == datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)

    def test_naive_iso_is_coerced_to_utc(self):
        result = _parse_marker_started("2026-08-09T17:00:00")
        assert result is not None
        assert result.tzinfo == _UTC
        assert result == datetime.datetime(2026, 8, 9, 17, 0, 0, tzinfo=_UTC)

    def test_empty_string_returns_none(self):
        assert _parse_marker_started("") is None


class TestFullyIndexedCounts:
    def test_hit_returns_files_and_lines_counts(self):
        es = MagicMock()
        es.search.return_value = {
            "hits": {"hits": [{"_source": {"files_count": 42, "lines_count": 3400}}]}
        }
        result = fully_indexed_counts(es, "github", "acme", "widgets", FULL_SHA)
        assert result == (42, 3400)
        call = es.search.call_args.kwargs
        assert call["index"] == REFS_ALIAS
        assert call["size"] == 1
        assert {"term": {"git.host": "github"}} in call["query"]["bool"]["filter"]
        assert {"term": {"git.org": "acme"}} in call["query"]["bool"]["filter"]
        assert {"term": {"git.repo": "widgets"}} in call["query"]["bool"]["filter"]
        assert {"term": {"git.commit": FULL_SHA}} in call["query"]["bool"]["filter"]
        assert {"term": {"status": "complete"}} in call["query"]["bool"]["filter"]
        assert call["source_includes"] == ["files_count", "lines_count"]

    def test_no_hit_returns_none(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": []}}
        assert fully_indexed_counts(es, "github", "acme", "widgets", FULL_SHA) is None

    def test_missing_index_returns_none_not_raise(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert fully_indexed_counts(es, "github", "acme", "widgets", FULL_SHA) is None

    def test_missing_count_fields_default_to_zero(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [{"_source": {}}]}}
        assert fully_indexed_counts(es, "github", "acme", "widgets", FULL_SHA) == (0, 0)

    def test_host_changes_result(self):
        es = MagicMock()
        es.search.return_value = {
            "hits": {"hits": [{"_source": {"files_count": 7, "lines_count": 900}}]}
        }
        assert fully_indexed_counts(es, "gitlab", "acme", "widgets", FULL_SHA) == (7, 900)
        call = es.search.call_args.kwargs
        assert {"term": {"git.host": "gitlab"}} in call["query"]["bool"]["filter"]


OLD = "1111111111111111111111111111111111111111"
NEW = "2222222222222222222222222222222222222222"


def _indexed_doc(es):
    return es.index.call_args.kwargs["document"]


class TestSnapshotJoinDoc:
    def test_join_doc_id_and_ref_key_are_the_commit(self):
        es = MagicMock()
        write_snapshot_join_doc(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None)
        call = es.index.call_args.kwargs
        assert call["id"] == OLD
        assert call["index"] == REFS_INDEX
        doc = call["document"]
        assert doc["git"]["ref_key"] == OLD
        assert doc["git"]["commit"] == OLD
        assert doc["update_mode"] == "snapshot"
        assert doc["status"] == "complete"

    def test_join_doc_idempotent_rewrite_same_id(self):
        first = MagicMock()
        second = MagicMock()
        write_snapshot_join_doc(first, "github", "acme", "widgets", "branch", "main", OLD, None)
        write_snapshot_join_doc(second, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None)
        assert first.index.call_args.kwargs["id"] == second.index.call_args.kwargs["id"]


class TestIncrementalRefKeyIdentity:
    def test_id_is_ref_key_not_a_hash(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "main", OLD, NEW)
        assert es.index.call_args.kwargs["id"] == build_ref_key("github", "acme", "widgets", "main")

    def test_stable_across_calls_commit_independent(self):
        a = build_ref_key("github", "acme", "widgets", "main")
        b = build_ref_key("github", "acme", "widgets", "main")
        assert a == b  # one document per branch, no commit folded in


class TestWriteIncrementalIndexing:
    def test_incremental_marker_preserves_completed_commit_and_exposes_target(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "main",
                                    completed_commit=OLD, target_commit=NEW)
        doc = _indexed_doc(es)
        assert doc["status"] == "indexing"
        assert doc["git"]["commit"] == OLD  # completed pointer unchanged (INV-006)
        assert doc["git"]["target_commit"] == NEW
        assert doc["update_mode"] == "incremental"
        assert es.index.call_args.kwargs["index"] == REFS_INDEX

    def test_incremental_marker_first_index_has_no_completed_commit(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "main",
                                    completed_commit=None, target_commit=NEW)
        assert _indexed_doc(es)["git"]["commit"] is None

    def test_incremental_marker_carries_prior_counts(self):
        es = MagicMock()
        prior = {"files_count": 12, "lines_count": 340, "git": {"commit_date": "2026-01-01T00:00:00+00:00"}}
        write_incremental_indexing(es, "github", "acme", "widgets", "main", OLD, NEW, prior=prior)
        doc = _indexed_doc(es)
        assert doc["files_count"] == 12 and doc["lines_count"] == 340
        assert doc["git"]["commit_date"] == "2026-01-01T00:00:00+00:00"


class TestWriteIncrementalReady:
    def test_incremental_marker_advances_commit_and_clears_target_and_error(self):
        es = MagicMock()
        write_incremental_ready(es, "github", "acme", "widgets", "main", commit=NEW,
                                 commit_date_iso="2026-02-02T00:00:00+00:00",
                                 files_count=5, lines_count=99)
        doc = _indexed_doc(es)
        assert doc["status"] == "ready"
        assert doc["git"]["commit"] == NEW  # advances only after a successful run (INV-006)
        assert doc["git"]["target_commit"] is None
        assert doc["error"] is None and doc["failed_at"] is None
        assert doc["files_count"] == 5 and doc["lines_count"] == 99
        assert es.index.call_args.kwargs["refresh"] is True  # publication boundary


class TestWriteIncrementalFailed:
    def test_incremental_marker_keeps_status_indexing_and_retains_old_pointer(self):
        es = MagicMock()
        write_incremental_failed(es, "github", "acme", "widgets", "main", completed_commit=OLD,
                                  target_commit=NEW, error="boom")
        doc = _indexed_doc(es)
        assert doc["status"] == "indexing"  # not advanced -- a failed run leaves the prior state
        assert doc["git"]["commit"] == OLD
        assert doc["git"]["target_commit"] == NEW
        assert doc["error"] == "boom"
        assert doc["failed_at"] is not None

    def test_incremental_marker_error_text_is_bounded(self):
        es = MagicMock()
        write_incremental_failed(es, "github", "acme", "widgets", "main", OLD, NEW, error="x" * 5000)
        assert len(_indexed_doc(es)["error"]) == ERROR_MAX_LEN


class TestReadIncrementalRef:
    def test_returns_source(self):
        es = MagicMock()
        es.get.return_value = {"_source": {"status": "ready", "git": {"commit": NEW}}}
        assert read_incremental_ref(es, "github", "acme", "widgets", "main") == (
            {"status": "ready", "git": {"commit": NEW}}
        )
        assert es.get.call_args.kwargs["id"] == build_ref_key("github", "acme", "widgets", "main")

    def test_missing_returns_none(self):
        es = MagicMock()
        es.get.side_effect = _not_found()
        assert read_incremental_ref(es, "github", "acme", "widgets", "main") is None


class TestDeleteIncrementalPaths:
    def test_empty_paths_is_noop(self):
        es = MagicMock()
        delete_incremental_paths(es, "github", "acme", "widgets", "main", [])
        es.delete_by_query.assert_not_called()

    def test_scoped_to_exact_ref_key_and_paths(self):
        es = MagicMock()
        delete_incremental_paths(es, "github", "acme", "widgets", "main", ["a.txt", "b.txt"])
        assert es.delete_by_query.call_count == 2  # files + lines indices
        for call in es.delete_by_query.call_args_list:
            query = call.kwargs["query"]
            assert {"term": {"git.ref_key": build_ref_key("github", "acme", "widgets", "main")}} in (
                query["bool"]["filter"]
            )
            assert {"terms": {"file.path": ["a.txt", "b.txt"]}} in query["bool"]["filter"]

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.delete_by_query.side_effect = _not_found()
        delete_incremental_paths(es, "github", "acme", "widgets", "main", ["a.txt"])  # no raise


class TestDeleteIncrementalBranch:
    def test_scoped_to_exact_ref_key_only(self):
        es = MagicMock()
        delete_incremental_branch(es, "github", "acme", "widgets", "main")
        assert es.delete_by_query.call_count == 2
        for call in es.delete_by_query.call_args_list:
            query = call.kwargs["query"]
            assert query["bool"]["filter"] == [
                {"term": {"git.ref_key": build_ref_key("github", "acme", "widgets", "main")}}
            ]

    def test_isolated_from_another_branch(self):
        # Two incremental branches indexed; deleting one's docs must never scope to the other's
        # ref_key (INV-008) -- asserted here at the query-construction level.
        es_a = MagicMock()
        es_b = MagicMock()
        delete_incremental_branch(es_a, "github", "acme", "widgets", "main")
        delete_incremental_branch(es_b, "github", "acme", "widgets", "dev")
        query_a = es_a.delete_by_query.call_args_list[0].kwargs["query"]
        query_b = es_b.delete_by_query.call_args_list[0].kwargs["query"]
        assert query_a != query_b


class TestCountIncrementalBranchDocs:
    def test_missing_index_returns_zero(self):
        es = MagicMock()
        es.count.side_effect = _not_found()
        assert count_incremental_branch_docs(es, "github", "acme", "widgets", "main") == (0, 0)

    def test_returns_counts(self):
        es = MagicMock()
        es.count.return_value = {"count": 5}
        assert count_incremental_branch_docs(es, "github", "acme", "widgets", "main") == (5, 5)
