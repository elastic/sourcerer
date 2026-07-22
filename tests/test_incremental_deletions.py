"""Unit tests for the synchronous, ref-scoped v2 deletion helpers in
sourcerer.commands.index.markers. Every ES call is mocked; the point is to prove the query
scope (exact git.ref_key + path terms, never a wildcard), the synchronous options
(wait_for_completion + conflicts=proceed), and that only v2 content indices are targeted.
"""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.markers import delete_v2_branch, delete_v2_paths
from sourcerer.indices import files_index_v2, lines_index_v2
from sourcerer.utils import build_ref_key


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestDeleteV2Paths:
    def test_targets_both_v2_content_indices(self):
        es = MagicMock()
        delete_v2_paths(es, "acme", "widgets", "main", ["a.txt", "b.txt"])
        indices = {c.kwargs["index"] for c in es.delete_by_query.call_args_list}
        assert indices == {files_index_v2("acme", "widgets"), lines_index_v2("acme", "widgets")}

    def test_query_scope_is_exact_ref_key_and_path_terms(self):
        es = MagicMock()
        delete_v2_paths(es, "acme", "widgets", "main", ["a.txt", "b.txt"])
        query = es.delete_by_query.call_args_list[0].kwargs["query"]
        filters = query["bool"]["filter"]
        assert {"term": {"git.ref_key": build_ref_key("acme", "widgets", "main")}} in filters
        assert {"terms": {"file.path": ["a.txt", "b.txt"]}} in filters

    def test_synchronous_options(self):
        es = MagicMock()
        delete_v2_paths(es, "acme", "widgets", "main", ["a.txt"])
        kwargs = es.delete_by_query.call_args_list[0].kwargs
        assert kwargs["wait_for_completion"] is True
        assert kwargs["conflicts"] == "proceed"

    def test_empty_paths_is_noop(self):
        es = MagicMock()
        delete_v2_paths(es, "acme", "widgets", "main", [])
        es.delete_by_query.assert_not_called()

    def test_missing_index_is_ignored(self):
        es = MagicMock()
        es.delete_by_query.side_effect = _not_found()
        delete_v2_paths(es, "acme", "widgets", "main", ["a.txt"])  # must not raise


class TestDeleteV2Branch:
    def test_scoped_to_exact_ref_key_only(self):
        es = MagicMock()
        delete_v2_branch(es, "acme", "widgets", "main")
        query = es.delete_by_query.call_args_list[0].kwargs["query"]
        filters = query["bool"]["filter"]
        assert filters == [{"term": {"git.ref_key": build_ref_key("acme", "widgets", "main")}}]

    def test_targets_both_v2_content_indices(self):
        es = MagicMock()
        delete_v2_branch(es, "acme", "widgets", "main")
        indices = {c.kwargs["index"] for c in es.delete_by_query.call_args_list}
        assert indices == {files_index_v2("acme", "widgets"), lines_index_v2("acme", "widgets")}
