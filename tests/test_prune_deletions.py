"""Unit tests for the deletion-executing functions in sourcerer.commands.prune.execute:
delete_index and execute_orphan_deletions (the orphan sweep), and execute_deletions
(retention). Every ES call is mocked -- these assert the shape of the requests (wildcard-free
deletes, query filters, single combined refs query), not against a real cluster. Repo tuples
are keyed (host, org, repo); index names are v3 and carry a leading host segment."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.prune.execute import (delete_index, execute_deletions,
                                              execute_orphan_deletions, wait_for_deletions)
from sourcerer.indices import REFS_INDEX, files_index, lines_index
from sourcerer.planner import Decision, Marker, OrphanPlan


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestDeleteIndex:
    def test_deletes_by_exact_name_no_wildcard(self):
        es = MagicMock()
        assert delete_index(es, "sourcerer-v3-files~github~acme~widgets") is True
        es.indices.delete.assert_called_once_with(index="sourcerer-v3-files~github~acme~widgets")

    def test_missing_index_returns_false_not_raise(self):
        es = MagicMock()
        es.indices.delete.side_effect = _not_found()
        assert delete_index(es, "sourcerer-v3-files~github~acme~widgets") is False


class TestExecuteOrphanDeletions:
    def test_deletes_indices_first(self):
        es = MagicMock()
        plan = OrphanPlan(
            orphan_index_names=["sourcerer-v3-files~github~ghostorg"],
            orphan_content={},
            orphan_marker_commits={},
        )
        indices_deleted, content_dropped, markers_dropped, stale_dropped, empty_deleted, task_ids = execute_orphan_deletions(es, plan)
        assert (indices_deleted, content_dropped, markers_dropped, stale_dropped, empty_deleted) == (1, 0, 0, 0, 0)
        assert task_ids == []
        es.indices.delete.assert_called_once_with(index="sourcerer-v3-files~github~ghostorg")
        es.delete_by_query.assert_not_called()

    def test_content_delete_by_query_targets_both_content_indices_with_terms_and_host_filter(self):
        es = MagicMock()
        es.delete_by_query.return_value = {"task": "node1:123"}
        plan = OrphanPlan(
            orphan_index_names=[],
            orphan_content={("github", "acme", "widgets"): {"aaa", "bbb"}},
            orphan_marker_commits={},
        )
        _, content_dropped, _, _, _, task_ids = execute_orphan_deletions(es, plan)
        assert content_dropped == 2
        assert task_ids == ["node1:123", "node1:123"]
        called_indices = {c.kwargs["index"] for c in es.delete_by_query.call_args_list}
        assert called_indices == {lines_index("github", "acme", "widgets"), files_index("github", "acme", "widgets")}
        for c in es.delete_by_query.call_args_list:
            filters = c.kwargs["query"]["bool"]["filter"]
            assert {"term": {"git.host": "github"}} in filters
            terms_filter = next(f for f in filters if "terms" in f)
            assert sorted(terms_filter["terms"]["git.commit"]) == ["aaa", "bbb"]
            assert c.kwargs["wait_for_completion"] is False

    def test_marker_delete_by_query_is_a_single_call_covering_all_repos(self):
        es = MagicMock()
        es.delete_by_query.return_value = {"task": "node1:456"}
        plan = OrphanPlan(
            orphan_index_names=[],
            orphan_content={},
            orphan_marker_commits={
                ("github", "acme", "widgets"): {"aaa"},
                ("gitlab", "globex", "gadgets"): {"bbb", "ccc"},
            },
        )
        _, _, markers_dropped, _, _, task_ids = execute_orphan_deletions(es, plan)
        assert markers_dropped == 3
        assert task_ids == ["node1:456"]
        es.delete_by_query.assert_called_once()
        kwargs = es.delete_by_query.call_args.kwargs
        assert kwargs["index"] == REFS_INDEX
        shoulds = kwargs["query"]["bool"]["should"]
        assert len(shoulds) == 2
        assert kwargs["query"]["bool"]["minimum_should_match"] == 1
        # every should clause carries a host filter
        for clause in shoulds:
            assert any("git.host" in f.get("term", {}) for f in clause["bool"]["filter"])

    def test_no_orphans_makes_no_calls(self):
        es = MagicMock()
        plan = OrphanPlan(orphan_index_names=[], orphan_content={}, orphan_marker_commits={})
        assert execute_orphan_deletions(es, plan) == (0, 0, 0, 0, 0, [])
        es.indices.delete.assert_not_called()
        es.delete_by_query.assert_not_called()

    def test_empty_indices_deleted_whole(self):
        es = MagicMock()
        plan = OrphanPlan(
            orphan_index_names=[], orphan_content={}, orphan_marker_commits={},
            empty_index_names=["sourcerer-v3-files~github~acme~widgets^olddeploy"],
        )
        _, _, _, _, empty_deleted, task_ids = execute_orphan_deletions(es, plan)
        assert empty_deleted == 1
        assert task_ids == []
        es.indices.delete.assert_called_once_with(
            index="sourcerer-v3-files~github~acme~widgets^olddeploy")
        es.delete_by_query.assert_not_called()


def _marker(id_: str, commit: str, ref: str = "main") -> Marker:
    return Marker(id=id_, ref=ref, ref_type="branch", commit=commit, commit_date=None, indexed_at=None)


class TestExecuteDeletions:
    def test_no_deletes_makes_no_calls(self):
        es = MagicMock()
        decisions = [Decision(_marker("m1", "aaa"), "keep", "retain forever")]
        assert execute_deletions(es, "github", "acme", "widgets", decisions) == (0, 0, [])
        es.delete_by_query.assert_not_called()

    def test_deletes_marker_docs_via_bulk_and_content_via_delete_by_query(self, monkeypatch):
        es = MagicMock()
        es.delete_by_query.return_value = {"task": "node1:789"}
        bulk_calls = []
        monkeypatch.setattr(
            "sourcerer.commands.prune.execute.bulk",
            lambda client, actions, **kw: bulk_calls.append((client, list(actions), kw)),
        )
        decisions = [Decision(_marker("m1", "aaa"), "delete", "count:1")]
        n_markers, n_commits, task_ids = execute_deletions(es, "github", "acme", "widgets", decisions)
        assert (n_markers, n_commits) == (1, 1)
        assert task_ids == ["node1:789", "node1:789"]
        assert len(bulk_calls) == 1
        _, actions, kwargs = bulk_calls[0]
        assert actions == [{"_op_type": "delete", "_index": REFS_INDEX, "_id": "m1"}]
        assert kwargs["refresh"] is False
        called_indices = {c.kwargs["index"] for c in es.delete_by_query.call_args_list}
        assert called_indices == {lines_index("github", "acme", "widgets"), files_index("github", "acme", "widgets")}
        for c in es.delete_by_query.call_args_list:
            assert {"term": {"git.host": "github"}} in c.kwargs["query"]["bool"]["filter"]

    def test_shared_commit_referenced_by_a_surviving_marker_keeps_its_content(self, monkeypatch):
        es = MagicMock()
        monkeypatch.setattr("sourcerer.commands.prune.execute.bulk", lambda *a, **k: None)
        decisions = [
            Decision(_marker("m1", "aaa", ref="old-branch"), "delete", "count:1"),
            Decision(_marker("m2", "aaa", ref="release-tag"), "keep", "keep forever"),
        ]
        n_markers, n_commits, task_ids = execute_deletions(es, "github", "acme", "widgets", decisions)
        assert (n_markers, n_commits) == (1, 0)
        assert task_ids == []
        es.delete_by_query.assert_not_called()


class TestWaitForDeletions:
    """wait_for_deletions polls es.tasks.get for every submitted task id until each reports
    completed: True, returning {task_id: status}. Backs `prune --wait`."""

    def test_no_task_ids_returns_empty_without_calling_es(self):
        es = MagicMock()
        assert wait_for_deletions(es, []) == {}
        es.tasks.get.assert_not_called()

    def test_immediately_completed_task_returns_its_status(self):
        es = MagicMock()
        es.tasks.get.return_value = {
            "completed": True,
            "task": {"status": {"total": 10, "deleted": 10, "version_conflicts": 0}},
        }
        result = wait_for_deletions(es, ["node1:1"])
        assert result == {"node1:1": {"total": 10, "deleted": 10, "version_conflicts": 0}}
        es.tasks.get.assert_called_once_with(task_id="node1:1")

    def test_polls_again_when_task_is_still_running(self, monkeypatch):
        es = MagicMock()
        es.tasks.get.side_effect = [
            {"completed": False},
            {"completed": True, "task": {"status": {"deleted": 5}}},
        ]
        sleeps = []
        monkeypatch.setattr("sourcerer.commands.prune.execute.time.sleep", lambda s: sleeps.append(s))
        result = wait_for_deletions(es, ["node1:2"], poll_interval=0.01)
        assert result == {"node1:2": {"deleted": 5}}
        assert es.tasks.get.call_count == 2
        assert sleeps == [0.01]

    def test_task_not_found_treated_as_done_with_empty_status(self):
        es = MagicMock()
        es.tasks.get.side_effect = _not_found()
        result = wait_for_deletions(es, ["node1:3"])
        assert result == {"node1:3": {}}

    def test_multiple_tasks_all_polled_before_returning(self):
        es = MagicMock()

        def fake_get(task_id):
            return {"completed": True, "task": {"status": {"deleted": 1}}}

        es.tasks.get.side_effect = fake_get
        result = wait_for_deletions(es, ["a", "b", "c"])
        assert set(result) == {"a", "b", "c"}
        assert es.tasks.get.call_count == 3

    def test_reports_progress_via_reporter_phase(self):
        from sourcerer.progress import PruneReporter

        es = MagicMock()
        es.tasks.get.return_value = {"completed": True, "task": {"status": {"deleted": 1}}}
        reporter = PruneReporter()
        wait_for_deletions(es, ["a", "b"], reporter=reporter)
        # No exception, and the (no-op) reporter's phase bookkeeping ran to completion --
        # nothing more to assert against the base no-op reporter beyond "it didn't crash".
