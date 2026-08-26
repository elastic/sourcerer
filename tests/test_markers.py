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
    write_indexing_marker,
    write_ref_marker,
)
from sourcerer.indices import FILES_ALIAS, REFS_ALIAS, REFS_INDEX, files_index_pattern
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
        assert es.search.call_args.kwargs["index"] == REFS_INDEX
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
    def test_reads_marker_by_searching_the_refs_index(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [{"_source": {"status": "complete"}}]}}

        assert should_index(es, "github", "acme", "widgets", "branch", "main", FULL_SHA) is False

        assert es.search.call_args.kwargs == {
            "index": REFS_INDEX,
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
            "index": REFS_INDEX,
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
        assert call["index"] == REFS_INDEX
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
    def test_returns_commits_present_in_v3_files_index(self):
        es = MagicMock()
        es.search.return_value = {
            "aggregations": {"present": {"buckets": [{"key": FULL_SHA}]}}
        }
        result = commits_with_content(es, "github", "acme", "widgets", {FULL_SHA})
        assert result == {FULL_SHA}
        call = es.search.call_args.kwargs
        assert call["index"] == files_index_pattern("github", "acme", "widgets")
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
        assert call["index"] == REFS_INDEX
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


class TestWriteRefMarker:
    """write_ref_marker is the single refs doc per snapshot source. git.ref_key has
    been removed; snapshot refs are identified by (host, org, repo, ref, commit) on the marker."""

    def test_marker_carries_commit_no_ref_key(self):
        es = MagicMock()
        write_ref_marker(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None,
                         files_count=10, lines_count=200)
        doc = es.index.call_args.kwargs["document"]
        assert doc["git"]["commit"] == OLD
        assert "ref_key" not in doc["git"]
        assert doc["mode"] == "snapshot"

    def test_marker_id_is_hashed_not_the_commit(self):
        # _id is build_ref_id (BLAKE2b hash) -- one per (ref, commit), NOT the bare commit SHA.
        es = MagicMock()
        write_ref_marker(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None,
                         files_count=10, lines_count=200)
        call = es.index.call_args.kwargs
        assert call["index"] == REFS_INDEX
        assert call["id"] != OLD  # hashed, not the bare commit
        assert call["id"] == build_ref_id("github", "acme", "widgets", "tag", "v1.0.0", OLD)

    def test_default_write_does_not_refresh(self):
        es = MagicMock()
        write_ref_marker(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None,
                         files_count=1, lines_count=1)
        assert es.index.call_args.kwargs.get("refresh") is False

    def test_refresh_true_is_propagated(self):
        # write_ref_marker accepts refresh=True for callers that need the doc visible before
        # the next gate runs.
        es = MagicMock()
        write_ref_marker(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None,
                         files_count=1, lines_count=1, refresh=True)
        assert es.index.call_args.kwargs["refresh"] is True

    def test_marker_status_complete(self):
        es = MagicMock()
        write_ref_marker(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None,
                         files_count=5, lines_count=100)
        assert es.index.call_args.kwargs["document"]["status"] == "complete"

    def test_complete_marker_carries_ref_pattern(self):
        # write_ref_marker (status:complete) must populate git.ref_pattern. When no ref_pattern
        # arg is given (branch == pattern), the fallback is git.ref.
        es = MagicMock()
        write_ref_marker(es, "github", "acme", "widgets", "branch", "main", OLD, None,
                         files_count=1, lines_count=10)
        doc = es.index.call_args.kwargs["document"]
        assert doc["git"]["ref_pattern"] == "main"

    def test_complete_marker_explicit_ref_pattern_differs_from_ref(self):
        # When a versioned snapshot tag has a pattern like "zentity-{major}.{minor}.{patch}",
        # git.ref holds the concrete tag and git.ref_pattern holds the raw match pattern.
        es = MagicMock()
        write_ref_marker(es, "github", "zentity-io", "zentity", "tag", "zentity-1.7.0", OLD, None,
                         files_count=10, lines_count=200,
                         ref_pattern="zentity-{major}.{minor}.{patch}")
        doc = es.index.call_args.kwargs["document"]
        assert doc["git"]["ref"] == "zentity-1.7.0"
        assert doc["git"]["ref_pattern"] == "zentity-{major}.{minor}.{patch}"
        # _id still keys on build_ref_id(concrete ref + commit), not the pattern,
        # so sibling tags sharing a ref_pattern get distinct docs.
        from sourcerer.commands.index.markers import build_ref_id
        expected_id = build_ref_id("github", "zentity-io", "zentity", "tag", "zentity-1.7.0", OLD)
        assert es.index.call_args.kwargs["id"] == expected_id


class TestWriteIndexingMarker:
    """write_indexing_marker is the in-progress (status:'indexing') snapshot writer. It must
    populate git.ref_pattern from the very first write, so the field is never NULL in a snapshot
    refs doc -- even while the ref is mid-index (before write_ref_marker's 'complete' overwrite)."""

    def test_indexing_marker_carries_ref_pattern(self):
        # Fallback: when no ref_pattern arg is passed, git.ref_pattern == git.ref.
        es = MagicMock()
        write_indexing_marker(es, "github", "acme", "widgets", "branch", "main", OLD, None)
        doc = es.index.call_args.kwargs["document"]
        assert doc["git"]["ref_pattern"] == "main"
        assert doc["git"]["ref"] == "main"
        assert doc["git"]["ref_pattern"] == doc["git"]["ref"]

    def test_indexing_marker_explicit_ref_pattern_differs_from_ref(self):
        # When a versioned snapshot tag has a pattern, the in-progress marker also carries it.
        es = MagicMock()
        write_indexing_marker(es, "github", "zentity-io", "zentity", "tag", "zentity-1.7.0", OLD, None,
                              ref_pattern="zentity-{major}.{minor}.{patch}")
        doc = es.index.call_args.kwargs["document"]
        assert doc["git"]["ref"] == "zentity-1.7.0"
        assert doc["git"]["ref_pattern"] == "zentity-{major}.{minor}.{patch}"

    def test_indexing_marker_status_is_indexing(self):
        es = MagicMock()
        write_indexing_marker(es, "github", "acme", "widgets", "tag", "v1.0.0", OLD, None)
        doc = es.index.call_args.kwargs["document"]
        assert doc["status"] == "indexing"
        assert doc["mode"] == "snapshot"

    def test_indexing_marker_id_matches_write_ref_marker_id(self):
        # Both writers must use the same ref_id so write_ref_marker overwrites in place.
        es = MagicMock()
        write_indexing_marker(es, "github", "acme", "widgets", "tag", "v2.0.0", OLD, None)
        indexing_id = es.index.call_args.kwargs["id"]
        write_ref_marker(es, "github", "acme", "widgets", "tag", "v2.0.0", OLD, None,
                         files_count=1, lines_count=1)
        complete_id = es.index.call_args.kwargs["id"]
        assert indexing_id == complete_id


class TestIncrementalRefKeyIdentity:
    def test_id_is_ref_key_not_a_hash(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "branch", "main", OLD, NEW)
        assert es.index.call_args.kwargs["id"] == build_ref_key("github", "acme", "widgets", "branch", "main")

    def test_stable_across_calls_commit_independent(self):
        a = build_ref_key("github", "acme", "widgets", "branch", "main")
        b = build_ref_key("github", "acme", "widgets", "branch", "main")
        assert a == b  # one document per ref, no commit folded in

    def test_branch_and_tag_same_name_have_distinct_keys(self):
        branch_key = build_ref_key("github", "acme", "widgets", "branch", "deploy")
        tag_key = build_ref_key("github", "acme", "widgets", "tag", "deploy")
        assert branch_key != tag_key  # ref_type distinguishes them


class TestWriteIncrementalIndexing:
    def test_incremental_marker_preserves_completed_commit_and_exposes_target(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "branch", "main",
                                    completed_commit=OLD, commit_target=NEW)
        doc = _indexed_doc(es)
        assert doc["status"] == "indexing"
        assert doc["git"]["commit"] == OLD  # completed pointer unchanged
        assert doc["git"]["commit_target"] == NEW
        assert doc["mode"] == "delta"
        assert doc["git"]["ref_type"] == "branch"
        assert es.index.call_args.kwargs["index"] == REFS_INDEX

    def test_incremental_marker_first_index_has_no_completed_commit(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "branch", "main",
                                    completed_commit=None, commit_target=NEW)
        assert _indexed_doc(es)["git"]["commit"] is None

    def test_incremental_marker_carries_prior_counts(self):
        es = MagicMock()
        prior = {"files_count": 12, "lines_count": 340, "git": {"commit_date": "2026-01-01T00:00:00+00:00"}}
        write_incremental_indexing(es, "github", "acme", "widgets", "branch", "main", OLD, NEW, prior=prior)
        doc = _indexed_doc(es)
        assert doc["files_count"] == 12 and doc["lines_count"] == 340
        assert doc["git"]["commit_date"] == "2026-01-01T00:00:00+00:00"

    def test_incremental_indexing_carries_routing(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "branch", "main", OLD, NEW,
                                   index_level="commit", index_suffix="s1")
        doc = _indexed_doc(es)
        assert doc["index_level"] == "commit"
        assert doc["index_suffix"] == "s1"

    def test_incremental_indexing_default_routing(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "branch", "main", OLD, NEW)
        doc = _indexed_doc(es)
        assert doc["index_level"] == "repo"
        assert doc["index_suffix"] is None

    def test_tag_ref_type_propagates_to_doc(self):
        es = MagicMock()
        write_incremental_indexing(es, "github", "acme", "widgets", "tag", "deploy@1",
                                    completed_commit=None, commit_target=NEW)
        doc = _indexed_doc(es)
        assert doc["git"]["ref_type"] == "tag"
        assert doc["git"]["ref"] == "deploy@1"
        # Join doc id must differ from a same-named branch's
        branch_id = build_ref_key("github", "acme", "widgets", "branch", "deploy@1")
        tag_id = build_ref_key("github", "acme", "widgets", "tag", "deploy@1")
        assert es.index.call_args.kwargs["id"] == tag_id
        assert tag_id != branch_id


class TestWriteIncrementalReady:
    def test_incremental_marker_advances_commit_and_clears_target_and_error(self):
        es = MagicMock()
        write_incremental_ready(es, "github", "acme", "widgets", "branch", "main", commit=NEW,
                                 commit_date_iso="2026-02-02T00:00:00+00:00",
                                 files_count=5, lines_count=99)
        doc = _indexed_doc(es)
        assert doc["status"] == "complete"
        assert doc["git"]["commit"] == NEW  # advances only after a successful run
        assert doc["git"]["commit_target"] is None
        assert doc["files_count"] == 5 and doc["lines_count"] == 99
        assert es.index.call_args.kwargs["refresh"] is True  # publication boundary

    def test_incremental_ready_carries_routing(self):
        es = MagicMock()
        write_incremental_ready(es, "github", "acme", "widgets", "branch", "main", commit=NEW,
                                commit_date_iso=None, files_count=1, lines_count=1,
                                index_level="commit", index_suffix="s1")
        doc = _indexed_doc(es)
        assert doc["index_level"] == "commit"
        assert doc["index_suffix"] == "s1"


class TestWriteIncrementalFailed:
    def test_incremental_failed_sets_status_failed_and_retains_old_pointer(self):
        es = MagicMock()
        write_incremental_failed(es, "github", "acme", "widgets", "branch", "main",
                                  completed_commit=OLD, commit_target=NEW, error="boom")
        doc = _indexed_doc(es)
        assert doc["status"] == "failed"
        assert doc["git"]["commit"] == OLD   # completed pointer unchanged
        assert doc["git"]["commit_target"] == NEW
        assert "error" not in doc
        assert "failed_at" not in doc

    def test_incremental_failed_carries_routing(self):
        es = MagicMock()
        write_incremental_failed(es, "github", "acme", "widgets", "branch", "main", OLD, NEW, error="boom",
                                 index_level="commit", index_suffix="s1")
        doc = _indexed_doc(es)
        assert doc["index_level"] == "commit"
        assert doc["index_suffix"] == "s1"


class TestReadIncrementalRef:
    def test_returns_source(self):
        es = MagicMock()
        es.get.return_value = {"_source": {"status": "ready", "git": {"commit": NEW}}}
        assert read_incremental_ref(es, "github", "acme", "widgets", "branch", "main") == (
            {"status": "ready", "git": {"commit": NEW}}
        )
        assert es.get.call_args.kwargs["id"] == build_ref_key("github", "acme", "widgets", "branch", "main")

    def test_missing_returns_none(self):
        es = MagicMock()
        es.get.side_effect = _not_found()
        assert read_incremental_ref(es, "github", "acme", "widgets", "branch", "main") is None


class TestDeleteIncrementalPaths:
    def test_empty_paths_is_noop(self):
        es = MagicMock()
        delete_incremental_paths(es, "github", "acme", "widgets", "branch", "main", [])
        es.delete_by_query.assert_not_called()

    def test_scoped_to_exact_ref_key_and_paths(self):
        es = MagicMock()
        delete_incremental_paths(es, "github", "acme", "widgets", "branch", "main", ["a.txt", "b.txt"])
        assert es.delete_by_query.call_count == 2  # files + lines indices
        for call in es.delete_by_query.call_args_list:
            query = call.kwargs["query"]
            filt = query["bool"]["filter"]
            assert {"term": {"git.host": "github"}} in filt
            assert {"term": {"git.org": "acme"}} in filt
            assert {"term": {"git.repo": "widgets"}} in filt
            assert {"term": {"git.ref_type": "branch"}} in filt
            assert {"term": {"git.ref_pattern": "main"}} in filt
            assert {"terms": {"file.path": ["a.txt", "b.txt"]}} in filt

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.delete_by_query.side_effect = _not_found()
        delete_incremental_paths(es, "github", "acme", "widgets", "branch", "main", ["a.txt"])  # no raise


class TestDeleteIncrementalBranch:
    def test_scoped_to_exact_ref_key_only(self):
        es = MagicMock()
        delete_incremental_branch(es, "github", "acme", "widgets", "main")
        assert es.delete_by_query.call_count == 2
        for call in es.delete_by_query.call_args_list:
            query = call.kwargs["query"]
            filt = query["bool"]["filter"]
            assert {"term": {"git.host": "github"}} in filt
            assert {"term": {"git.org": "acme"}} in filt
            assert {"term": {"git.repo": "widgets"}} in filt
            assert {"term": {"git.ref_type": "branch"}} in filt
            assert {"term": {"git.ref_pattern": "main"}} in filt
            assert not any("ref_key" in str(f) for f in filt)

    def test_isolated_from_another_branch(self):
        # Two incremental branches indexed; deleting one's docs must never scope to the other's
        # (host,org,repo,ref) quadruple -- asserted here at the query-construction level.
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
