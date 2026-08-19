"""Tests for the one-time upgrade backfill in sourcerer.commands.index.markers: stamping
git.ref_key onto pre-existing snapshot content, stamping ref_key onto existing markers that
predate the one-doc-per-source change, and deleting legacy shadow join docs.
Every ES call is mocked (INV-009/INV-010)."""

# Standard packages
from unittest.mock import MagicMock, call

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
    commits_with_ref_key_carrier,
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


class TestCommitsWithRefKeyCarrier:
    """commits_with_ref_key_carrier (and its alias commits_with_join_doc) returns the subset
    of commits for which a refs doc carries git.ref_key == commit."""

    def test_empty_input_short_circuits(self):
        es = MagicMock()
        assert commits_with_ref_key_carrier(es, set()) == set()
        es.search.assert_not_called()

    def test_returns_commits_from_agg_buckets(self):
        es = MagicMock()
        es.search.return_value = {"aggregations": {"carriers": {"buckets": [{"key": "aaa"}]}}}
        assert commits_with_ref_key_carrier(es, {"aaa", "bbb"}) == {"aaa"}

    def test_missing_index_returns_empty_set(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert commits_with_ref_key_carrier(es, {"aaa"}) == set()

    def test_alias_commits_with_join_doc_is_the_same_function(self):
        # commits_with_join_doc kept as alias for backward compatibility.
        assert commits_with_join_doc is commits_with_ref_key_carrier


class TestBackfillRefsJoinDocs:
    def test_stamps_ref_key_onto_existing_marker(self):
        # Normal case: a complete marker exists for the commit; backfill stamps ref_key on it.
        es = MagicMock()
        marker_id = "deadbeef" * 4  # any string
        es.search.side_effect = [
            # distinct_commits_for_repo
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            # commits_with_ref_key_carrier -- no carrier yet
            {"aggregations": {"carriers": {"buckets": []}}},
            # per-missing-commit: find the existing marker (no ref_key yet)
            {"hits": {"hits": [{"_id": marker_id, "_source": {"git": {"ref": "v1.0", "ref_type": "tag"}}}]}},
        ]
        stamped = backfill_refs_join_docs(es, "github", "acme", "widgets")
        assert stamped == 1
        # Must use es.update (partial doc), not es.index
        es.update.assert_called_once()
        assert es.update.call_args.kwargs["id"] == marker_id
        assert es.update.call_args.kwargs["doc"] == {"git": {"ref_key": FULL_SHA}}
        es.index.assert_not_called()

    def test_falls_back_to_index_for_orphan_content(self):
        # No marker found for the commit: write a minimal _id=commit carrier doc.
        es = MagicMock()
        es.search.side_effect = [
            # distinct_commits_for_repo
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            # commits_with_ref_key_carrier -- no carrier
            {"aggregations": {"carriers": {"buckets": []}}},
            # per-missing-commit marker search -- no marker
            {"hits": {"hits": []}},
            # fallback: any refs doc for this commit (for informational fields)
            {"hits": {"hits": []}},
        ]
        stamped = backfill_refs_join_docs(es, "github", "acme", "widgets")
        assert stamped == 1
        es.update.assert_not_called()
        # Falls back to writing an _id=commit carrier
        es.index.assert_called_once()
        call_kwargs = es.index.call_args.kwargs
        assert call_kwargs["id"] == FULL_SHA
        assert call_kwargs["document"]["git"]["ref_key"] == FULL_SHA
        assert call_kwargs["document"]["git"]["commit"] == FULL_SHA

    def test_second_run_stamps_nothing(self):
        # INV-009/INV-010: every commit already has a carrier -> no-op.
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            {"aggregations": {"carriers": {"buckets": [{"key": FULL_SHA}]}}},
        ]
        assert backfill_refs_join_docs(es, "github", "acme", "widgets") == 0
        es.update.assert_not_called()
        es.index.assert_not_called()

    def test_no_commits_short_circuits(self):
        es = MagicMock()
        es.search.return_value = {"aggregations": {"commits": {"buckets": []}}}
        assert backfill_refs_join_docs(es, "github", "acme", "widgets") == 0
        es.update.assert_not_called()
        es.index.assert_not_called()

    def test_refreshes_refs_index_when_carriers_stamped(self):
        # A uniqueness-gate run immediately afterward must see the just-stamped carrier
        # rather than racing the refs index's refresh interval.
        es = MagicMock()
        marker_id = "aabbccdd" * 4
        es.search.side_effect = [
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            {"aggregations": {"carriers": {"buckets": []}}},
            {"hits": {"hits": [{"_id": marker_id, "_source": {"git": {"ref": "v1.0", "ref_type": "tag"}}}]}},
        ]
        backfill_refs_join_docs(es, "github", "acme", "widgets")
        assert es.indices.refresh.call_args.kwargs["index"] == REFS_INDEX

    def test_no_refresh_when_nothing_stamped(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"commits": {"buckets": [{"key": FULL_SHA}]}}},
            {"aggregations": {"carriers": {"buckets": [{"key": FULL_SHA}]}}},
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
        # All search calls return empty (no commits -> backfill no-ops; delete_by_query finds nothing)
        es.search.return_value = {"aggregations": {"commits": {"buckets": []}}}
        es.delete_by_query.return_value = {"deleted": 0}
        summary = backfill_repo(
            es, "github", "acme", "widgets", refs_mapping={"properties": {}},
            files_mapping={"properties": {}}, lines_mapping={"properties": {}},
        )
        assert summary == {"content_updated": 0, "carriers_stamped": 0, "shadow_docs_deleted": 0}

    def test_summary_keys(self):
        # Confirm the returned dict uses the new key names.
        es = MagicMock()
        es.update_by_query.return_value = {"updated": 0}
        es.search.return_value = {"aggregations": {"commits": {"buckets": []}}}
        es.delete_by_query.return_value = {"deleted": 0}
        summary = backfill_repo(es, "github", "acme", "widgets")
        assert set(summary.keys()) == {"content_updated", "carriers_stamped", "shadow_docs_deleted"}

    def test_deletes_legacy_shadow_docs(self):
        # Migration: after stamping carriers, delete legacy update_mode:snapshot shadow docs.
        es = MagicMock()
        es.update_by_query.return_value = {"updated": 0}
        es.search.return_value = {"aggregations": {"commits": {"buckets": []}}}
        es.delete_by_query.return_value = {"deleted": 3}
        summary = backfill_repo(es, "github", "acme", "widgets")
        assert summary["shadow_docs_deleted"] == 3
        # delete_by_query must target the physical REFS_INDEX (not the alias -- only writes go there)
        assert es.delete_by_query.call_args.kwargs["index"] == REFS_INDEX
        # Must scope to update_mode: "snapshot" (the legacy shadow doc shape)
        filters = es.delete_by_query.call_args.kwargs["query"]["bool"]["filter"]
        assert {"term": {"git.host": "github"}} in filters
        assert {"term": {"update_mode": "snapshot"}} in filters
