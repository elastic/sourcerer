"""Unit tests for the ES-facing, READ-ONLY orphan-sweep helpers in sourcerer.commands.index
(index listing and tuple enumeration). Every ES call is mocked -- these tests assert the
shape of the requests (index patterns, aggregation pagination), not against a real cluster.

Deletion logic (delete_index, execute_orphan_deletions, execute_deletions) lives in
commands/prune.py, not here -- see test_prune_deletions.py."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index import (
    FILES_INDEX_PREFIX,
    LINES_INDEX_PREFIX,
    REFS_INDEX,
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
    def test_queries_only_files_and_lines_prefixes(self):
        es = MagicMock()
        es.cat.indices.return_value = [
            {"index": "sourcerer-v1-files~acme~widgets"},
            {"index": "sourcerer-v1-lines~acme~widgets"},
        ]
        names = list_sourcerer_indices(es)
        assert names == ["sourcerer-v1-files~acme~widgets", "sourcerer-v1-lines~acme~widgets"]
        _, kwargs = es.cat.indices.call_args
        assert kwargs["index"] == f"{FILES_INDEX_PREFIX}*,{LINES_INDEX_PREFIX}*"
        assert REFS_INDEX not in kwargs["index"]
        assert kwargs["h"] == "index"


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
        assert first_kwargs["index"] == REFS_INDEX
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
        assert enumerate_content_commits(es, "sourcerer-v1-files~acme~widgets") == set()


class TestGatherContentCommitTuples:
    def test_only_queries_org_repo_granularity_indices(self):
        es = MagicMock()
        es.search.return_value = _composite_response([("acme", "widgets", "aaa")], after_key=None)
        index_names = [
            "sourcerer-v1-files~acme",                      # org-only: skipped
            "sourcerer-v1-files~acme~widgets",               # org~repo: queried
            "sourcerer-v1-lines~acme~widgets~deadbeef",      # org~repo~commit: skipped
            "sourcerer-v1-refs",                             # unrelated: skipped
        ]
        result = gather_content_commit_tuples(es, index_names)
        assert result == {("acme", "widgets", "aaa")}
        assert es.search.call_count == 1
        assert es.search.call_args.kwargs["index"] == "sourcerer-v1-files~acme~widgets"
