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
    empty_content_indices,
    enumerate_content_commits,
    enumerate_ref_tuples,
    gather_content_commit_tuples,
    list_sourcerer_indices,
)


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


def _composite_response(tuples: list[tuple[str, str, str, str]], after_key: dict | None) -> dict:
    return {
        "aggregations": {
            "tuples": {
                "buckets": [{"key": {"host": h, "org": o, "repo": r, "commit": c}} for h, o, r, c in tuples],
                **({"after_key": after_key} if after_key else {}),
            }
        }
    }


class TestListSourcererIndices:
    def test_discovers_backing_indices_through_content_aliases(self):
        es = MagicMock()
        es.indices.get_alias.side_effect = [
            {"sourcerer-v3-files~github~acme~widgets": {}},
            {"sourcerer-v3-lines~github~acme~widgets": {}},
        ]
        names = list_sourcerer_indices(es)
        assert names == ["sourcerer-v3-files~github~acme~widgets", "sourcerer-v3-lines~github~acme~widgets"]
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
            _composite_response([("github", "acme", "widgets", "aaa")], after_key={"host": "github"}),
            _composite_response([("github", "acme", "widgets", "bbb")], after_key=None),
        ]
        result = enumerate_ref_tuples(es)
        assert result == {("github", "acme", "widgets", "aaa"), ("github", "acme", "widgets", "bbb")}
        assert es.search.call_count == 2
        first_kwargs = es.search.call_args_list[0].kwargs
        second_kwargs = es.search.call_args_list[1].kwargs
        assert first_kwargs["index"] == REFS_ALIAS
        assert "after" not in first_kwargs["aggs"]["tuples"]["composite"]
        assert second_kwargs["aggs"]["tuples"]["composite"]["after"] == {"host": "github"}
        # host is the leading composite source
        sources = first_kwargs["aggs"]["tuples"]["composite"]["sources"]
        assert list(sources[0]) == ["host"]

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
            _composite_response([("github", "acme", "widgets", "aaa")], after_key=None),
            _composite_response([("github", "acme", "widgets", "aaa")], after_key=None),
        ]
        result = gather_content_commit_tuples(es)
        assert result == {("github", "acme", "widgets", "aaa")}
        assert es.search.call_count == 2
        assert [call.kwargs["index"] for call in es.search.call_args_list] == [FILES_ALIAS, LINES_ALIAS]


class TestEmptyContentIndices:
    """empty_content_indices: sourcerer content indices with zero docs, guarded by
    parse_index_name so unrelated / refs indices are never returned."""

    def _es_with_counts(self, counts: dict[str, int]):
        es = MagicMock()

        def fake_count(index):
            if index not in counts:
                raise _not_found()
            return {"count": counts[index]}

        es.count.side_effect = fake_count
        return es

    def test_returns_only_zero_doc_content_indices(self):
        counts = {
            "sourcerer-v3-files~github~acme~widgets": 0,        # empty -> returned
            "sourcerer-v3-files~github~acme~widgets^deploy": 5, # non-empty -> skipped
        }
        es = self._es_with_counts(counts)
        result = empty_content_indices(es, list(counts))
        assert result == ["sourcerer-v3-files~github~acme~widgets"]

    def test_non_sourcerer_index_never_considered(self):
        # An unrelated (even empty) index must not be counted or returned.
        es = self._es_with_counts({"some-other-index": 0})
        result = empty_content_indices(es, ["some-other-index"])
        assert result == []
        es.count.assert_not_called()  # guarded by parse_index_name before any count

    def test_refs_index_not_considered(self):
        es = self._es_with_counts({"sourcerer-v3-refs": 0})
        assert empty_content_indices(es, ["sourcerer-v3-refs"]) == []
        es.count.assert_not_called()

    def test_index_that_vanished_is_skipped(self):
        # count raises NotFound (deleted between listing and counting) -> just skipped.
        es = self._es_with_counts({})  # every count -> NotFound
        assert empty_content_indices(es, ["sourcerer-v3-files~github~acme~widgets"]) == []
