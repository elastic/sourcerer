"""Unit tests for the run_ref decision-building logic in sourcerer.commands.prune.command.

The core soundness invariant: run_ref fetches ALL markers for the repo and builds a decision
list where only the targeted ref is 'delete' and every other marker is 'keep'. This ensures
content_delete_set only drops commits that no surviving ref still references.

These tests drive the decision-building path via a thin harness that stubs out
fetch_markers and execute_deletions so no real ES cluster is needed. No orphan sweep
is performed by run_ref, so plan_orphans_now and execute_orphan_deletions are not patched.
"""

# Standard packages
from unittest.mock import MagicMock, patch

# App packages
from sourcerer.commands.prune.command import run_ref
from sourcerer.planner import Decision, Marker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _marker(id_: str, ref: str, ref_type: str, commit: str) -> Marker:
    return Marker(id=id_, ref=ref, ref_type=ref_type, commit=commit, commit_date=None, indexed_at=None)


def _make_es():
    """Return a MagicMock ES client whose indices.refresh is a no-op."""
    es = MagicMock()
    es.indices.refresh.return_value = None
    return es


# ---------------------------------------------------------------------------
# Decision-building: only the targeted ref becomes 'delete'
# ---------------------------------------------------------------------------

class TestRunRefDecisionBuilding:
    """Verify that run_ref builds decisions over ALL repo markers, marking only the target
    as 'delete' and every other marker as 'keep'. Tests do not actually call execute_deletions
    -- the captured decisions list is what we assert on. No orphan sweep is performed."""

    def _capture_decisions_for_tag(self, all_markers, target_tag):
        """Call run_ref targeting 'target_tag'; return the decisions list passed to
        execute_deletions (captured via monkeypatch)."""
        captured = {}

        def fake_fetch_markers(es, host, org, repo, ref_type=None, ref=None):
            return all_markers

        def fake_execute_deletions(es, host, org, repo, decisions):
            captured["decisions"] = list(decisions)
            return (1, 0)

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch_markers), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_execute_deletions), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag=target_tag,
                commit=None,
                dry_run=False,
                quiet=True,
            )

        return captured.get("decisions", [])

    def test_only_target_ref_is_delete(self):
        """A single targeted tag is marked 'delete'; every other marker is 'keep'."""
        markers = [
            _marker("m1", "v1.0", "tag", "aaabbb" * 6 + "aaab"),  # target
            _marker("m2", "v2.0", "tag", "cccddd" * 6 + "cccd"),  # bystander
            _marker("m3", "main", "branch", "eeefff" * 6 + "eeef"),  # bystander
        ]
        decisions = self._capture_decisions_for_tag(markers, "v1.0")
        actions = {d.marker.ref: d.action for d in decisions}
        assert actions == {"v1.0": "delete", "v2.0": "keep", "main": "keep"}

    def test_all_repo_markers_appear_in_decisions(self):
        """Decisions list length equals the total number of repo markers -- none are dropped."""
        markers = [_marker(f"m{i}", f"v{i}.0", "tag", f"{'ab' * 19}{i:02d}") for i in range(5)]
        decisions = self._capture_decisions_for_tag(markers, "v2.0")
        assert len(decisions) == len(markers)

    def test_shared_commit_not_deleted_when_another_ref_points_to_it(self):
        """The soundness constraint: a commit shared by the target and a kept ref must NOT
        appear in content_delete_set. If execute_deletions sees both a 'delete' and a 'keep'
        for the same SHA, content_delete_set returns empty -- no content is dropped."""
        shared_sha = "a" * 40
        markers = [
            _marker("m1", "old-branch", "branch", shared_sha),  # targeted
            _marker("m2", "release-tag", "tag", shared_sha),   # kept -- same commit
        ]
        # Target by branch name
        captured = {}

        def fake_fetch_markers(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_execute_deletions(es, host, org, repo, decisions):
            from sourcerer.planner import content_delete_set
            captured["drop_commits"] = content_delete_set(decisions)
            return (1, 0)

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch_markers), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_execute_deletions), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch="old-branch",
                tag=None,
                commit=None,
                dry_run=False,
                quiet=True,
            )

        assert captured["drop_commits"] == set(), (
            "Content shared with a surviving ref must not appear in content_delete_set"
        )

    def test_distinct_commit_is_safe_to_drop(self):
        """A commit referenced only by the deleted marker (no other ref) IS in
        content_delete_set -- its content has no surviving owner."""
        unique_sha = "b" * 40
        other_sha = "c" * 40
        markers = [
            _marker("m1", "old-branch", "branch", unique_sha),  # targeted, unique commit
            _marker("m2", "other-branch", "branch", other_sha),  # kept, distinct commit
        ]
        captured = {}

        def fake_fetch_markers(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_execute_deletions(es, host, org, repo, decisions):
            from sourcerer.planner import content_delete_set
            captured["drop_commits"] = content_delete_set(decisions)
            return (1, 1)

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch_markers), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_execute_deletions), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch="old-branch",
                tag=None,
                commit=None,
                dry_run=False,
                quiet=True,
            )

        assert captured["drop_commits"] == {unique_sha}, (
            "A commit with no surviving owner should be in content_delete_set"
        )

    def test_commit_ref_matched_by_prefix(self):
        """A -c <commit> prefix (>=7 hex chars) matches the stored full SHA."""
        full_sha = "d" * 40
        markers = [
            _marker("m1", full_sha, "commit", full_sha),
        ]
        captured = {}

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_exec(es, host, org, repo, decisions):
            captured["decisions"] = list(decisions)
            return (1, 1)

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_exec), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag=None,
                commit=full_sha[:10],  # short prefix -- should match
                dry_run=False,
                quiet=True,
            )

        assert "decisions" in captured
        assert any(d.action == "delete" for d in captured["decisions"]), (
            "A short-hash prefix should match the stored full SHA"
        )

    def test_ambiguous_commit_prefix_errors(self):
        """If a prefix matches more than one indexed commit, run_ref exits with an error."""
        sha_a = "abcdef1234" + "0" * 30
        sha_b = "abcdef5678" + "0" * 30
        markers = [
            _marker("m1", sha_a, "commit", sha_a),
            _marker("m2", sha_b, "commit", sha_b),
        ]

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            import pytest
            with pytest.raises(SystemExit):
                run_ref(
                    "github/acme/widgets",
                    branch=None,
                    tag=None,
                    commit="abcdef",  # matches both
                    dry_run=False,
                    quiet=True,
                )

    def test_not_indexed_ref_exits_without_calling_execute(self):
        """run_ref with a ref not found in the index is a no-op: execute_deletions never called."""
        markers = [
            _marker("m1", "v1.0", "tag", "a" * 40),
        ]
        execute_called = []

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_exec(*a, **kw):
            execute_called.append(True)
            return (0, 0)

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_exec), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag="v99.0",  # not in the index
                commit=None,
                dry_run=False,
                quiet=True,
            )

        assert not execute_called

    def test_dry_run_does_not_call_execute(self):
        """--dry-run must never call execute_deletions."""
        markers = [
            _marker("m1", "v1.0", "tag", "a" * 40),
        ]
        execute_called = []

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_exec(*a, **kw):
            execute_called.append(True)
            return (0, 0)

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_exec), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag="v1.0",
                commit=None,
                dry_run=True,
                quiet=True,
            )

        assert not execute_called


class TestRunRefNoOrphanSweep:
    """Verify that run_ref never touches the orphan sweep, regardless of dry_run."""

    def _run_ref_with_spy(self, dry_run: bool):
        markers = [_marker("m1", "v1.0", "tag", "a" * 40)]

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        sweep_called = []

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", return_value=(1, 0)), \
             patch("sourcerer.commands.prune.command.plan_orphans_now",
                   side_effect=lambda *a, **kw: sweep_called.append("plan") or None), \
             patch("sourcerer.commands.prune.command.execute_orphan_deletions",
                   side_effect=lambda *a, **kw: sweep_called.append("exec") or (0, 0, 0)), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag="v1.0",
                commit=None,
                dry_run=dry_run,
                quiet=True,
            )

        return sweep_called

    def test_orphan_sweep_not_called_on_live_run(self):
        """run_ref must not call plan_orphans_now or execute_orphan_deletions on a live run."""
        assert self._run_ref_with_spy(dry_run=False) == [], (
            "Orphan sweep must not run during a single-ref prune"
        )

    def test_orphan_sweep_not_called_on_dry_run(self):
        """run_ref must not call plan_orphans_now or execute_orphan_deletions on --dry-run."""
        assert self._run_ref_with_spy(dry_run=True) == [], (
            "Orphan sweep must not run during a single-ref --dry-run prune"
        )
