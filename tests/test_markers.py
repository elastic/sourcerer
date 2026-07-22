"""Unit tests for the commit-selector additions in sourcerer.commands.index.markers:
commit_prefix_indexed (a cheap ES lookup that stands in for `git ls-remote`, which has no way
to resolve a SHA/prefix) and the commit branch of pre_clone_skip. Every ES call is mocked."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import (
    ERROR_MAX_LEN,
    build_v2_ref_id,
    commit_prefix_indexed,
    pre_clone_skip,
    read_v2_ref,
    write_v2_failed,
    write_v2_indexing,
    write_v2_ready,
)
from sourcerer.indices import REFS_INDEX_V2

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


OLD = "1111111111111111111111111111111111111111"
NEW = "2222222222222222222222222222222222222222"


class TestV2RefId:
    def test_stable_across_calls_and_commit_independent(self):
        a = build_v2_ref_id("acme", "widgets", "main")
        b = build_v2_ref_id("acme", "widgets", "main")
        assert a == b  # one document per branch, no commit folded in

    def test_org_repo_case_insensitive_but_ref_case_sensitive(self):
        assert build_v2_ref_id("Acme", "Widgets", "main") == build_v2_ref_id("acme", "widgets", "main")
        assert build_v2_ref_id("acme", "widgets", "Main") != build_v2_ref_id("acme", "widgets", "main")


def _indexed_doc(es):
    return es.index.call_args.kwargs["document"]


class TestWriteV2Indexing:
    def test_preserves_completed_commit_and_exposes_target(self):
        es = MagicMock()
        write_v2_indexing(es, "acme", "widgets", "main", completed_commit=OLD, target_commit=NEW)
        doc = _indexed_doc(es)
        assert doc["status"] == "indexing"
        assert doc["git"]["commit"] == OLD  # completed pointer unchanged
        assert doc["git"]["target_commit"] == NEW  # candidate advertised
        assert doc["update_mode"] == "incremental"
        assert es.index.call_args.kwargs["id"] == build_v2_ref_id("acme", "widgets", "main")
        assert es.index.call_args.kwargs["index"] == REFS_INDEX_V2

    def test_first_index_has_no_completed_commit(self):
        es = MagicMock()
        write_v2_indexing(es, "acme", "widgets", "main", completed_commit=None, target_commit=NEW)
        assert _indexed_doc(es)["git"]["commit"] is None

    def test_carries_prior_counts(self):
        es = MagicMock()
        prior = {"files_count": 12, "lines_count": 340, "git": {"commit_date": "2026-01-01T00:00:00+00:00"}}
        write_v2_indexing(es, "acme", "widgets", "main", OLD, NEW, prior=prior)
        doc = _indexed_doc(es)
        assert doc["files_count"] == 12 and doc["lines_count"] == 340
        assert doc["git"]["commit_date"] == "2026-01-01T00:00:00+00:00"


class TestWriteV2Ready:
    def test_advances_commit_and_clears_target_and_error(self):
        es = MagicMock()
        write_v2_ready(es, "acme", "widgets", "main", commit=NEW,
                       commit_date_iso="2026-02-02T00:00:00+00:00", files_count=5, lines_count=99)
        doc = _indexed_doc(es)
        assert doc["status"] == "ready"
        assert doc["git"]["commit"] == NEW
        assert doc["git"]["target_commit"] is None
        assert doc["error"] is None and doc["failed_at"] is None
        assert doc["files_count"] == 5 and doc["lines_count"] == 99
        assert es.index.call_args.kwargs["refresh"] is True  # publication boundary


class TestWriteV2Failed:
    def test_keeps_status_indexing_and_retains_old_pointer(self):
        es = MagicMock()
        write_v2_failed(es, "acme", "widgets", "main", completed_commit=OLD,
                        target_commit=NEW, error="boom")
        doc = _indexed_doc(es)
        assert doc["status"] == "indexing"  # not advanced
        assert doc["git"]["commit"] == OLD
        assert doc["git"]["target_commit"] == NEW
        assert doc["error"] == "boom"
        assert doc["failed_at"] is not None

    def test_error_text_is_bounded(self):
        es = MagicMock()
        write_v2_failed(es, "acme", "widgets", "main", OLD, NEW, error="x" * 5000)
        assert len(_indexed_doc(es)["error"]) == ERROR_MAX_LEN


class TestReadV2Ref:
    def test_returns_source(self):
        es = MagicMock()
        es.get.return_value = {"_source": {"status": "ready", "git": {"commit": NEW}}}
        assert read_v2_ref(es, "acme", "widgets", "main") == {"status": "ready", "git": {"commit": NEW}}

    def test_missing_returns_none(self):
        es = MagicMock()
        es.get.side_effect = _not_found()
        assert read_v2_ref(es, "acme", "widgets", "main") is None
