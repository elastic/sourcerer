"""Unit tests for the commit-selector additions in sourcerer.commands.index.markers:
commit_prefix_indexed (a cheap ES lookup that stands in for `git ls-remote`, which has no way
to resolve a SHA/prefix) and the commit branch of pre_clone_skip. Every ES call is mocked."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import commit_prefix_indexed, pre_clone_skip

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
        assert commit_prefix_indexed(es, "acme", "widgets", "cfefb3b") == FULL_SHA
        query = es.search.call_args.kwargs["query"]
        assert {"prefix": {"git.commit": "cfefb3b"}} in query["bool"]["filter"]
        assert {"term": {"status": "complete"}} in query["bool"]["filter"]
        assert {"term": {"git.org": "acme"}} in query["bool"]["filter"]
        assert {"term": {"git.repo": "widgets"}} in query["bool"]["filter"]

    def test_no_hit_returns_none(self):
        es = MagicMock()
        es.search.return_value = _search_hits(None)
        assert commit_prefix_indexed(es, "acme", "widgets", "cfefb3b") is None

    def test_missing_index_returns_none_not_raise(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert commit_prefix_indexed(es, "acme", "widgets", "cfefb3b") is None


class TestPreCloneSkipCommit:
    def test_force_bypasses_without_querying(self):
        es = MagicMock()
        skip, ref_for_id, remote_sha = pre_clone_skip(
            es, "acme", "widgets", None, None, "cfefb3b", True,
        )
        assert (skip, ref_for_id, remote_sha) == (False, None, None)
        es.search.assert_not_called()

    def test_matching_marker_skips_the_clone(self):
        es = MagicMock()
        es.search.return_value = _search_hits(FULL_SHA)
        skip, ref_for_id, remote_sha = pre_clone_skip(
            es, "acme", "widgets", None, None, "CFEFB3B", False,
        )
        assert skip is True
        assert ref_for_id == FULL_SHA
        assert remote_sha == FULL_SHA

    def test_no_matching_marker_falls_through_to_clone(self):
        es = MagicMock()
        es.search.return_value = _search_hits(None)
        skip, ref_for_id, remote_sha = pre_clone_skip(
            es, "acme", "widgets", None, None, "cfefb3b", False,
        )
        assert (skip, ref_for_id, remote_sha) == (False, None, None)
