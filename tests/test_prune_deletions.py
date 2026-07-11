"""Unit tests for the deletion-executing functions in sourcerer.commands.prune: delete_index
and execute_orphan_deletions (the orphan sweep), and execute_deletions (retention). Every ES
call is mocked -- these assert the shape of the requests (wildcard-free deletes, query
filters, single combined refs query), not against a real cluster."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index import REFS_INDEX, files_index, lines_index
from sourcerer.commands.prune import delete_index, execute_deletions, execute_orphan_deletions
from sourcerer.planner import Decision, Marker, OrphanPlan


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestDeleteIndex:
    def test_deletes_by_exact_name_no_wildcard(self):
        es = MagicMock()
        assert delete_index(es, "sourcerer-v1-files~acme~widgets") is True
        es.indices.delete.assert_called_once_with(index="sourcerer-v1-files~acme~widgets")

    def test_missing_index_returns_false_not_raise(self):
        es = MagicMock()
        es.indices.delete.side_effect = _not_found()
        assert delete_index(es, "sourcerer-v1-files~acme~widgets") is False


class TestExecuteOrphanDeletions:
    def test_deletes_indices_first(self):
        es = MagicMock()
        plan = OrphanPlan(
            orphan_index_names=["sourcerer-v1-files~ghostorg"],
            orphan_content={},
            orphan_marker_commits={},
        )
        indices_deleted, content_dropped, markers_dropped = execute_orphan_deletions(es, plan)
        assert (indices_deleted, content_dropped, markers_dropped) == (1, 0, 0)
        es.indices.delete.assert_called_once_with(index="sourcerer-v1-files~ghostorg")
        es.delete_by_query.assert_not_called()

    def test_content_delete_by_query_targets_both_content_indices_with_terms_filter(self):
        es = MagicMock()
        plan = OrphanPlan(
            orphan_index_names=[],
            orphan_content={("acme", "widgets"): {"aaa", "bbb"}},
            orphan_marker_commits={},
        )
        _, content_dropped, _ = execute_orphan_deletions(es, plan)
        assert content_dropped == 2
        called_indices = {c.kwargs["index"] for c in es.delete_by_query.call_args_list}
        assert called_indices == {lines_index("acme", "widgets"), files_index("acme", "widgets")}
        for c in es.delete_by_query.call_args_list:
            filters = c.kwargs["query"]["bool"]["filter"]
            terms_filter = next(f for f in filters if "terms" in f)
            assert sorted(terms_filter["terms"]["git.commit"]) == ["aaa", "bbb"]
            assert c.kwargs["wait_for_completion"] is False

    def test_marker_delete_by_query_is_a_single_call_covering_all_repos(self):
        es = MagicMock()
        plan = OrphanPlan(
            orphan_index_names=[],
            orphan_content={},
            orphan_marker_commits={
                ("acme", "widgets"): {"aaa"},
                ("globex", "gadgets"): {"bbb", "ccc"},
            },
        )
        _, _, markers_dropped = execute_orphan_deletions(es, plan)
        assert markers_dropped == 3
        es.delete_by_query.assert_called_once()
        kwargs = es.delete_by_query.call_args.kwargs
        assert kwargs["index"] == REFS_INDEX
        shoulds = kwargs["query"]["bool"]["should"]
        assert len(shoulds) == 2
        assert kwargs["query"]["bool"]["minimum_should_match"] == 1

    def test_missing_content_index_is_swallowed(self):
        es = MagicMock()
        es.delete_by_query.side_effect = _not_found()
        plan = OrphanPlan(
            orphan_index_names=[],
            orphan_content={("acme", "widgets"): {"aaa"}},
            orphan_marker_commits={},
        )
        # Should not raise.
        execute_orphan_deletions(es, plan)

    def test_no_orphans_makes_no_calls(self):
        es = MagicMock()
        plan = OrphanPlan(orphan_index_names=[], orphan_content={}, orphan_marker_commits={})
        assert execute_orphan_deletions(es, plan) == (0, 0, 0)
        es.indices.delete.assert_not_called()
        es.delete_by_query.assert_not_called()


def _marker(id_: str, commit: str, ref: str = "main") -> Marker:
    return Marker(id=id_, ref=ref, ref_type="branch", commit=commit, commit_date=None, indexed_at=None)


class TestExecuteDeletions:
    def test_no_deletes_makes_no_calls(self):
        es = MagicMock()
        decisions = [Decision(_marker("m1", "aaa"), "keep", "retain forever")]
        assert execute_deletions(es, "acme", "widgets", decisions) == (0, 0)
        es.delete_by_query.assert_not_called()

    def test_deletes_marker_docs_via_bulk_and_content_via_delete_by_query(self, monkeypatch):
        es = MagicMock()
        bulk_calls = []
        monkeypatch.setattr(
            "sourcerer.commands.prune.bulk",
            lambda client, actions, **kw: bulk_calls.append((client, list(actions), kw)),
        )
        decisions = [Decision(_marker("m1", "aaa"), "delete", "count:1")]
        n_markers, n_commits = execute_deletions(es, "acme", "widgets", decisions)
        assert (n_markers, n_commits) == (1, 1)
        assert len(bulk_calls) == 1
        _, actions, kwargs = bulk_calls[0]
        assert actions == [{"_op_type": "delete", "_index": REFS_INDEX, "_id": "m1"}]
        assert kwargs["refresh"] is False
        called_indices = {c.kwargs["index"] for c in es.delete_by_query.call_args_list}
        assert called_indices == {lines_index("acme", "widgets"), files_index("acme", "widgets")}

    def test_shared_commit_referenced_by_a_surviving_marker_keeps_its_content(self, monkeypatch):
        es = MagicMock()
        monkeypatch.setattr("sourcerer.commands.prune.bulk", lambda *a, **k: None)
        decisions = [
            Decision(_marker("m1", "aaa", ref="old-branch"), "delete", "count:1"),
            Decision(_marker("m2", "aaa", ref="release-tag"), "keep", "keep forever"),
        ]
        n_markers, n_commits = execute_deletions(es, "acme", "widgets", decisions)
        assert (n_markers, n_commits) == (1, 0)
        es.delete_by_query.assert_not_called()
