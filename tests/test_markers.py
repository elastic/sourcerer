"""Unit tests for sourcerer.commands.index.markers: the per-ref and batched idempotency
helpers. Every ES call is mocked."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import (
    build_ref_id, commit_prefix_indexed, commits_with_content,
    markers_status_by_id, _needs_index, pre_clone_skip, should_index,
)
from sourcerer.indices import FILES_ALIAS, REFS_ALIAS

FULL_SHA = "cfefb3b2378ccbadefa7c8f4f9e21b3a1d2e5f60"


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
        assert result == {ref_id: {"status": "complete", "commit": FULL_SHA}}
        call = es.search.call_args.kwargs
        assert call["index"] == REFS_ALIAS
        assert call["query"] == {"ids": {"values": [ref_id]}}
        assert "status" in call["source_includes"]
        assert "git.commit" in call["source_includes"]

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

    def test_incomplete_status_needs_indexing(self):
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "indexing", "commit": self.SHA}}, {self.SHA}) is True

    def test_complete_with_content_present_is_skipped(self):
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete", "commit": self.SHA}}, {self.SHA}) is False

    def test_complete_but_content_gcd_out_needs_indexing(self):
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete", "commit": self.SHA}}, set()) is True

    def test_falls_back_to_remote_sha_when_commit_missing_from_marker(self):
        # A marker without a 'commit' field falls back to remote_sha for the content check.
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete"}}, {self.SHA}) is False
        assert _needs_index(self.ID, self.SHA, {self.ID: {"status": "complete"}}, set()) is True
