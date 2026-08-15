"""Tests for the one-time upgrade backfill in sourcerer.commands.index.markers: stamping
git.ref_key/update_mode onto pre-existing snapshot content, migrating the refs index mapping,
and creating missing snapshot join docs. Every ES call is mocked (INV-009/INV-010)."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import (
    apply_content_index_mapping,
    apply_refs_index_mapping,
    backfill_refs_join_docs,
    backfill_repo,
    backfill_snapshot_ref_keys,
    commits_with_join_doc,
    distinct_commits_for_repo,
)
from sourcerer.indices import FILES_ALIAS, LINES_ALIAS, REFS_INDEX

FULL_SHA = "cfefb3b2378ccbadefa7c8f4f9e21b3a1d2e5f60"


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestBackfillSnapshotRefKeys:
    def test_scoped_to_repo_and_missing_ref_key(self):
        es = MagicMock()
        es.update_by_query.return_value = {"updated": 3}
        total = backfill_snapshot_ref_keys(es, "github", "acme", "widgets")
        assert total == 6  # 3 (files) + 3 (lines)
        assert es.update_by_query.call_count == 2
        indices = {c.kwargs["index"] for c in es.update_by_query.call_args_list}
        assert indices == {FILES_ALIAS, LINES_ALIAS}
        for call in es.update_by_query.call_args_list:
            query = call.kwargs["query"]
            assert {"term": {"git.host": "github"}} in query["bool"]["filter"]
            assert {"exists": {"field": "git.ref_key"}} in query["bool"]["must_not"]

    def test_second_run_is_a_no_op(self):
        # Idempotency (INV-009): once every doc has ref_key, the must_not:exists filter
        # matches nothing, so a repeat run updates 0 docs.
        es = MagicMock()
        es.update_by_query.return_value = {"updated": 0}
        assert backfill_snapshot_ref_keys(es, "github", "acme", "widgets") == 0

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.update_by_query.side_effect = _not_found()
        assert backfill_snapshot_ref_keys(es, "github", "acme", "widgets") == 0


class TestDistinctCommitsForRepo:
    def test_returns_bucket_keys(self):
        es = MagicMock()
        es.search.return_value = {"aggregations": {"commits": {"buckets": [
            {"key": "aaa"}, {"key": "bbb"},
        ]}}}
        assert distinct_commits_for_repo(es, "github", "acme", "widgets") == {"aaa", "bbb"}

    def test_missing_index_returns_empty_set(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert distinct_commits_for_repo(es, "github", "acme", "widgets") == set()


class TestCommitsWithJoinDoc:
    def test_empty_input_short_circuits(self):
        es = MagicMock()
        assert commits_with_join_doc(es, set()) == set()
        es.search.assert_not_called()

    def test_returns_hit_ids(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [{"_id": "aaa"}]}}
        assert commits_with_join_doc(es, {"aaa", "bbb"}) == {"aaa"}


class TestBackfillRefsJoinDocs:
    def test_creates_join_doc_for_missing_commit(self):
        es = MagicMock()
        es.search.side_effect = [
            # distinct_commits_for_repo
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            # commits_with_join_doc -- no existing join docs
            {"hits": {"hits": []}},
            # per-missing-commit lookup for a build_ref_id marker (none found)
            {"hits": {"hits": []}},
        ]
        created = backfill_refs_join_docs(es, "github", "acme", "widgets")
        assert created == 1
        assert es.index.call_args.kwargs["id"] == FULL_SHA
        assert es.index.call_args.kwargs["document"]["git"]["ref_key"] == FULL_SHA

    def test_second_run_creates_nothing(self):
        # INV-009/INV-010: every commit already has a join doc -> no-op.
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            {"hits": {"hits": [{"_id": FULL_SHA}]}},
        ]
        assert backfill_refs_join_docs(es, "github", "acme", "widgets") == 0
        es.index.assert_not_called()

    def test_no_commits_short_circuits(self):
        es = MagicMock()
        es.search.return_value = {"aggregations": {"commits": {"buckets": []}}}
        assert backfill_refs_join_docs(es, "github", "acme", "widgets") == 0
        es.index.assert_not_called()

    def test_refreshes_refs_index_when_docs_created(self):
        # A uniqueness-gate run immediately afterward must see the just-created join doc
        # rather than racing the refs index's refresh interval.
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            {"hits": {"hits": []}},
            {"hits": {"hits": []}},
        ]
        backfill_refs_join_docs(es, "github", "acme", "widgets")
        assert es.indices.refresh.call_args.kwargs["index"] == REFS_INDEX

    def test_no_refresh_when_nothing_created(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            {"hits": {"hits": [{"_id": FULL_SHA}]}},
        ]
        backfill_refs_join_docs(es, "github", "acme", "widgets")
        es.indices.refresh.assert_not_called()


class TestApplyContentIndexMapping:
    def test_puts_mapping_on_both_aliases(self):
        es = MagicMock()
        apply_content_index_mapping(
            es,
            {"properties": {"git": {"properties": {"ref_key": {"type": "keyword"}}}}},
            {"properties": {"git": {"properties": {"ref_key": {"type": "keyword"}}}}},
        )
        indices = {c.kwargs["index"] for c in es.indices.put_mapping.call_args_list}
        assert indices == {FILES_ALIAS, LINES_ALIAS}

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.indices.put_mapping.side_effect = _not_found()
        apply_content_index_mapping(es, {"properties": {}}, {"properties": {}})  # no raise


class TestApplyRefsIndexMapping:
    def test_puts_mapping_on_refs_index(self):
        es = MagicMock()
        apply_refs_index_mapping(es, {"properties": {"git": {"properties": {"ref_key": {"type": "keyword"}}}}})
        assert es.indices.put_mapping.call_args.kwargs["index"] == REFS_INDEX

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.indices.put_mapping.side_effect = _not_found()
        apply_refs_index_mapping(es, {"properties": {}})  # no raise


class TestBackfillRepo:
    def test_second_run_is_fully_idempotent(self):
        es = MagicMock()
        es.update_by_query.return_value = {"updated": 0}
        es.search.return_value = {"hits": {"hits": []}, "aggregations": {"commits": {"buckets": []}}}
        summary = backfill_repo(
            es, "github", "acme", "widgets", refs_mapping={"properties": {}},
            files_mapping={"properties": {}}, lines_mapping={"properties": {}},
        )
        assert summary == {"content_updated": 0, "join_docs_created": 0}
