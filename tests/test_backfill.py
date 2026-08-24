"""Tests for mapping helpers and the stale-marker switchover in markers.py.

The ref_key backfill subsystem (backfill_snapshot_ref_keys, backfill_refs_join_docs,
backfill_repo, etc.) has been removed along with git.ref_key. This file tests the
surviving apply_*_index_mapping helpers and the new mark_snapshot_markers_stale helper
that handles mode-switch from snapshot to incremental.
"""

# Standard packages
from unittest.mock import MagicMock, call

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import (
    apply_content_index_mapping,
    apply_refs_index_mapping,
    mark_snapshot_markers_stale,
    stale_snapshot_markers_for_ref,
)
from sourcerer.indices import FILES_ALIAS, LINES_ALIAS, REFS_INDEX


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestApplyContentIndexMapping:
    def test_puts_mapping_on_both_aliases(self):
        es = MagicMock()
        apply_content_index_mapping(
            es,
            {"properties": {"git": {"properties": {"ref": {"type": "keyword"}}}}},
            {"properties": {"git": {"properties": {"ref": {"type": "keyword"}}}}},
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
        apply_refs_index_mapping(es, {"properties": {"status": {"type": "keyword"}}})
        assert es.indices.put_mapping.call_args.kwargs["index"] == REFS_INDEX

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.indices.put_mapping.side_effect = _not_found()
        apply_refs_index_mapping(es, {"properties": {}})  # no raise


class TestStaleSnapshotMarkersForRef:
    def test_returns_complete_non_incremental_markers(self):
        """Returns complete markers that are mode=snapshot (i.e. snapshot markers)."""
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [
            {"_id": "abc123", "_source": {"git": {"commit": "deadbeef"}}},
        ]}}
        hits = stale_snapshot_markers_for_ref(es, "github", "acme", "widgets", "main")
        assert len(hits) == 1
        assert hits[0]["_id"] == "abc123"

    def test_missing_index_returns_empty(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert stale_snapshot_markers_for_ref(es, "github", "acme", "widgets", "main") == []

    def test_query_scopes_to_host_org_repo_ref(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": []}}
        stale_snapshot_markers_for_ref(es, "github", "acme", "widgets", "main")
        query = es.search.call_args.kwargs["query"]
        filt = query["bool"]["filter"]
        assert {"term": {"git.host": "github"}} in filt
        assert {"term": {"git.org": "acme"}} in filt
        assert {"term": {"git.repo": "widgets"}} in filt
        assert {"term": {"git.ref_pattern": "main"}} in filt
        assert {"term": {"status": "complete"}} in filt
        assert {"term": {"mode": "snapshot"}} in filt


class TestMarkSnapshotMarkersStale:
    def test_flips_each_marker_to_stale(self):
        """mark_snapshot_markers_stale flips every found marker to status:'stale'."""
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [
            {"_id": "marker1", "_source": {"git": {"commit": "aaa"}}},
            {"_id": "marker2", "_source": {"git": {"commit": "aaa"}}},
        ]}}
        count = mark_snapshot_markers_stale(es, "github", "acme", "widgets", "main")
        assert count == 2
        assert es.update.call_count == 2
        ids_updated = {c.kwargs["id"] for c in es.update.call_args_list}
        assert ids_updated == {"marker1", "marker2"}
        for c in es.update.call_args_list:
            assert c.kwargs["doc"] == {"status": "stale"}

    def test_no_markers_returns_zero(self):
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": []}}
        assert mark_snapshot_markers_stale(es, "github", "acme", "widgets", "main") == 0
        es.update.assert_not_called()

    def test_update_not_found_is_ignored(self):
        """A marker that disappears between the search and the update is not an error."""
        es = MagicMock()
        es.search.return_value = {"hits": {"hits": [
            {"_id": "gone", "_source": {"git": {"commit": "aaa"}}},
        ]}}
        es.update.side_effect = _not_found()
        count = mark_snapshot_markers_stale(es, "github", "acme", "widgets", "main")
        assert count == 1  # 1 found, even if the update race-lost
