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
            return (1, 0, [])

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
            return (1, 0, [])

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
            return (1, 1, [])

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
            return (1, 1, [])

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
            return (0, 0, [])

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
            return (0, 0, [])

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


class TestRunRefCommitMatchesAnyRefType:
    """Verify that -c <sha> matches markers of any ref_type (branch/tag/commit) by
    marker.commit, not by marker.ref or marker.ref_type.

    New behaviour introduced when the ref_type == "commit" guard was removed from
    _matches_commit: the match is now marker.commit.startswith(target) regardless of
    how the commit was originally indexed."""

    def _capture_commit_prune(self, markers, commit_prefix):
        """Run run_ref with -c <commit_prefix>; return (decisions, execute_called)."""
        captured = {}

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_exec(es, host, org, repo, decisions):
            captured["decisions"] = list(decisions)
            return (len([d for d in decisions if d.action == "delete"]), 1, [])

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_exec), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag=None,
                commit=commit_prefix,
                dry_run=False,
                quiet=True,
            )
        return captured.get("decisions", []), "decisions" in captured

    def test_branch_marker_matched_by_commit_sha(self):
        """-c <sha> deletes a branch marker whose git.commit matches, even though ref_type==branch."""
        sha = "e" * 40
        markers = [
            _marker("m1", "main", "branch", sha),
        ]
        decisions, called = self._capture_commit_prune(markers, sha[:10])
        assert called, "execute_deletions should have been called"
        actions = {d.marker.ref: d.action for d in decisions}
        assert actions == {"main": "delete"}, "branch marker at target SHA must be deleted"

    def test_tag_marker_matched_by_commit_sha(self):
        """-c <sha> deletes a tag marker whose git.commit matches."""
        sha = "f" * 40
        markers = [
            _marker("m1", "v1.0.0", "tag", sha),
        ]
        decisions, called = self._capture_commit_prune(markers, sha)
        assert called
        assert any(d.action == "delete" and d.marker.ref == "v1.0.0" for d in decisions)

    def test_branch_and_tag_on_same_sha_both_deleted_no_ambiguity(self):
        """A branch and a tag legitimately sharing one SHA must both be deleted and must NOT
        trigger the ambiguous-prefix guard (they share one distinct commit, not two)."""
        sha = "cc" * 20
        markers = [
            _marker("m1", "main", "branch", sha),
            _marker("m2", "v2.0", "tag", sha),
            _marker("m3", "other", "branch", "dd" * 20),  # bystander
        ]
        decisions, called = self._capture_commit_prune(markers, sha[:9])
        assert called, "execute_deletions should have been called"
        actions = {d.marker.ref: d.action for d in decisions}
        assert actions["main"] == "delete"
        assert actions["v2.0"] == "delete"
        assert actions["other"] == "keep"

    def test_ambiguous_prefix_two_distinct_shas_errors(self):
        """A prefix matching two DISTINCT commits still raises an error."""
        sha_a = "abc1111111" + "0" * 30
        sha_b = "abc2222222" + "0" * 30
        markers = [
            _marker("m1", "main", "branch", sha_a),
            _marker("m2", "feature", "branch", sha_b),
        ]

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        import pytest
        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            with pytest.raises(SystemExit):
                run_ref(
                    "github/acme/widgets",
                    branch=None,
                    tag=None,
                    commit="abc",  # prefix matches both distinct SHAs
                    dry_run=False,
                    quiet=True,
                )

    def test_bystander_on_different_sha_kept(self):
        """A marker on a different commit is not affected by a -c targeting another SHA."""
        target_sha = "1" * 40
        other_sha = "2" * 40
        markers = [
            _marker("m1", "main", "branch", target_sha),
            _marker("m2", "v9.0", "tag", other_sha),
        ]
        decisions, _ = self._capture_commit_prune(markers, target_sha)
        actions = {d.marker.ref: d.action for d in decisions}
        assert actions["main"] == "delete"
        assert actions["v9.0"] == "keep"

    def test_content_safety_guard_honoured_for_branch_target(self):
        """When -c <sha> matches multiple markers (branch + tag) on the same commit, ALL of
        them are deleted.  Because no surviving marker keeps the commit, content_delete_set
        includes the SHA -- the full-removal semantics the user requested.

        Contrast with the branch-prune path (-b <name>): only the named branch marker is
        deleted, so a tag on the same SHA remains in 'kept' and protects the content.  Here
        we deliberately remove every ref at the SHA, so the content is correctly dropped too."""
        sha = "9" * 40
        markers = [
            _marker("m1", "main", "branch", sha),  # targeted -- deleted by -c
            _marker("m2", "v1.0", "tag", sha),     # also targeted (same SHA) -- also deleted
        ]
        captured = {}

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_exec(es, host, org, repo, decisions):
            from sourcerer.planner import content_delete_set
            captured["drop_commits"] = content_delete_set(decisions)
            return (2, 1, [])

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_exec), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag=None,
                commit=sha,
                dry_run=False,
                quiet=True,
            )

        assert captured.get("drop_commits") == {sha}, (
            "All markers at the SHA are deleted, so content_delete_set must include the SHA"
        )

    def test_content_safety_guard_still_blocks_when_another_commit_survives(self):
        """-c <sha> targets only the markers at that SHA. A surviving marker on a *different*
        SHA in the same repo is NOT affected -- and the bystander's content is NOT dropped."""
        target_sha = "a" * 40
        other_sha = "b" * 40
        markers = [
            _marker("m1", "main", "branch", target_sha),  # targeted
            _marker("m2", "other", "branch", other_sha),  # bystander on a different SHA
        ]
        captured = {}

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        def fake_exec(es, host, org, repo, decisions):
            from sourcerer.planner import content_delete_set
            captured["drop_commits"] = content_delete_set(decisions)
            return (1, 1, [])

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", fake_exec), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag=None,
                commit=target_sha,
                dry_run=False,
                quiet=True,
            )

        assert captured.get("drop_commits") == {target_sha}, (
            "Only the targeted SHA should be in the content drop set; bystander SHA must not appear"
        )
        assert other_sha not in captured.get("drop_commits", set()), (
            "Bystander marker's content must be protected"
        )


class TestRunRefNoMarkerFallback:
    """Verify the content-only fallback: when -c <sha> matches no marker, run_ref falls back
    to querying the content indices directly and deleting content if the commit is found there.
    This covers the 'old commits a branch has moved past' case.

    The fallback is invoked only for ref_type == 'commit' (i.e. when -c is used); branch/tag
    with no marker still exits as 'not indexed, nothing to prune'."""

    def _run_no_marker(self, commit_arg, resolved_shas, *, dry_run=False):
        """Call run_ref with -c and empty marker list. resolved_shas is the set returned by
        the fake resolve_content_commit. Returns list of delete_commit_content call args."""
        deleted = []

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return []  # no markers

        def fake_resolve(es, host, org, repo, prefix):
            return resolved_shas

        def fake_delete_content(es, host, org, repo, sha, index_names=None):
            deleted.append(sha)
            return []

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.resolve_content_commit", fake_resolve), \
             patch("sourcerer.commands.prune.command.content_indices_for_commit", return_value=[]), \
             patch("sourcerer.commands.prune.command.delete_commit_content", fake_delete_content), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch=None,
                tag=None,
                commit=commit_arg,
                dry_run=dry_run,
                quiet=True,
            )
        return deleted

    def test_no_marker_content_found_deletes_content(self):
        """When -c matches no marker but content exists for the SHA, the content is deleted."""
        sha = "abcdef1234567890" + "0" * 24
        deleted = self._run_no_marker(sha[:10], {sha})
        assert deleted == [sha], "delete_commit_content must be called with the resolved SHA"

    def test_no_marker_no_content_is_noop(self):
        """When -c matches nothing in markers or content, resolve returns empty → no deletion."""
        deleted = self._run_no_marker("aaaaaaa", set())
        assert deleted == [], "No content found → nothing to delete"

    def test_no_marker_ambiguous_content_errors(self):
        """If the prefix resolves to multiple SHAs in content, run_ref exits with an error."""
        sha_a = "aab1111111" + "0" * 30
        sha_b = "aab2222222" + "0" * 30

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return []

        def fake_resolve(es, host, org, repo, prefix):
            return {sha_a, sha_b}

        import pytest
        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.resolve_content_commit", fake_resolve), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            with pytest.raises(SystemExit):
                run_ref(
                    "github/acme/widgets",
                    branch=None,
                    tag=None,
                    commit="aab",
                    dry_run=False,
                    quiet=True,
                )

    def test_no_marker_dry_run_does_not_delete_content(self):
        """--dry-run on the content-only path must not call delete_commit_content."""
        sha = "bbbbbbbbbb" + "0" * 30
        deleted = self._run_no_marker(sha[:10], {sha}, dry_run=True)
        assert deleted == [], "--dry-run must not call delete_commit_content"

    def test_branch_not_in_index_still_noop(self):
        """A branch/tag not in the index still produces 'not indexed' without triggering the
        content-only fallback (fallback is -c only)."""
        execute_called = []
        resolve_called = []

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return []

        def fake_resolve(*a, **kw):
            resolve_called.append(True)
            return set()

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.resolve_content_commit", fake_resolve), \
             patch("sourcerer.commands.prune.command.execute_deletions",
                   side_effect=lambda *a, **kw: execute_called.append(True) or (0, 0)), \
             patch("sourcerer.commands.prune.command.make_client", return_value=_make_es()):
            run_ref(
                "github/acme/widgets",
                branch="no-such-branch",
                tag=None,
                commit=None,
                dry_run=False,
                quiet=True,
            )

        assert not execute_called, "execute_deletions must not be called"
        assert not resolve_called, "resolve_content_commit must not be called for branch/tag path"


class TestRunRefNoOrphanSweep:
    """Verify that run_ref never touches the orphan sweep, regardless of dry_run."""

    def _run_ref_with_spy(self, dry_run: bool):
        markers = [_marker("m1", "v1.0", "tag", "a" * 40)]

        def fake_fetch(es, host, org, repo, ref_type=None, ref=None):
            return markers

        sweep_called = []

        with patch("sourcerer.commands.prune.command.fetch_markers", fake_fetch), \
             patch("sourcerer.commands.prune.command.execute_deletions", return_value=(1, 0, [])), \
             patch("sourcerer.commands.prune.command.plan_orphans_now",
                   side_effect=lambda *a, **kw: sweep_called.append("plan") or None), \
             patch("sourcerer.commands.prune.command.execute_orphan_deletions",
                   side_effect=lambda *a, **kw: sweep_called.append("exec") or (0, 0, 0, 0, 0, [])), \
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
