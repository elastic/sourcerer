"""Unit tests for the ES-facing, READ-ONLY orphan-sweep helpers in sourcerer.queries
(index listing and tuple enumeration). Every ES call is mocked -- these tests assert the
shape of the requests (index patterns, aggregation pagination), not against a real cluster.

Deletion logic (delete_index, execute_orphan_deletions, execute_deletions) lives in
commands/prune/execute.py, not here -- see test_prune_deletions.py."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.indices import FILES_ALIAS, LINES_ALIAS, REFS_ALIAS
from sourcerer.queries import (
    enumerate_content_commits,
    enumerate_ref_tuples,
    gather_content_commit_tuples,
    list_sourcerer_indices,
)


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


def _composite_response(tuples: list[tuple[str, str, str]], after_key: dict | None) -> dict:
    return {
        "aggregations": {
            "tuples": {
                "buckets": [{"key": {"org": o, "repo": r, "commit": c}} for o, r, c in tuples],
                **({"after_key": after_key} if after_key else {}),
            }
        }
    }


class TestListSourcererIndices:
    def test_discovers_backing_indices_through_content_aliases(self):
        es = MagicMock()
        es.indices.get_alias.side_effect = [
            {"sourcerer-v1-files~acme~widgets": {}},
            {"sourcerer-v1-lines~acme~widgets": {}},
        ]
        names = list_sourcerer_indices(es)
        assert names == ["sourcerer-v1-files~acme~widgets", "sourcerer-v1-lines~acme~widgets"]
        assert es.indices.get_alias.call_args_list[0].kwargs == {"name": FILES_ALIAS}
        assert es.indices.get_alias.call_args_list[1].kwargs == {"name": LINES_ALIAS}

    def test_missing_aliases_return_no_indices(self):
        es = MagicMock()
        es.indices.get_alias.side_effect = _not_found()

        assert list_sourcerer_indices(es) == []


class TestCompositeTuples:
    def test_paginates_until_no_after_key(self):
        es = MagicMock()
        es.search.side_effect = [
            _composite_response([("acme", "widgets", "aaa")], after_key={"org": "acme"}),
            _composite_response([("acme", "widgets", "bbb")], after_key=None),
        ]
        result = enumerate_ref_tuples(es)
        assert result == {("acme", "widgets", "aaa"), ("acme", "widgets", "bbb")}
        assert es.search.call_count == 2
        first_kwargs = es.search.call_args_list[0].kwargs
        second_kwargs = es.search.call_args_list[1].kwargs
        assert first_kwargs["index"] == REFS_ALIAS
        assert "after" not in first_kwargs["aggs"]["tuples"]["composite"]
        assert second_kwargs["aggs"]["tuples"]["composite"]["after"] == {"org": "acme"}

    def test_empty_buckets_stops_pagination(self):
        es = MagicMock()
        es.search.return_value = _composite_response([], after_key=None)
        assert enumerate_ref_tuples(es) == set()
        es.search.assert_called_once()

    def test_missing_index_returns_empty_set(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert enumerate_content_commits(es, FILES_ALIAS) == set()


class TestGatherContentCommitTuples:
    def test_queries_content_aliases(self):
        es = MagicMock()
        es.search.side_effect = [
            _composite_response([("acme", "widgets", "aaa")], after_key=None),
            _composite_response([("acme", "widgets", "aaa")], after_key=None),
        ]
        result = gather_content_commit_tuples(es)
        assert result == {("acme", "widgets", "aaa")}
        assert es.search.call_count == 2
        assert [call.kwargs["index"] for call in es.search.call_args_list] == [FILES_ALIAS, LINES_ALIAS]
