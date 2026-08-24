"""Tests for the post-index join-uniqueness gate: sourcerer.queries.check_join_uniqueness
(INV-011) and its command.py wiring in _run_uniqueness_gate. Every ES call is mocked.

The gate is split by content shape (no mode on content docs):
  - Snapshot (git.commit IS NOT NULL): each commit must have ≥1 complete refs doc.
  - Incremental (git.ref_pattern IS NOT NULL): each (ref_type, ref_pattern) pair must have EXACTLY
    ONE incremental join doc (keyed by git.ref_pattern; one join doc per stream identity).
"""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.command import _run_uniqueness_gate
from sourcerer.queries import check_join_uniqueness


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


def _commit_enum_resp(commits: list[str]) -> dict:
    """Response for _enumerate_content_field(git.commit) — composite agg 'keys'."""
    return {"aggregations": {"keys": {"buckets": [{"key": {"val": c}} for c in commits]}}}


def _ref_enum_resp(ref_pairs: list[tuple[str, str]]) -> dict:
    """Response for _enumerate_incremental_content_ref_pairs — composite agg 'pairs'.
    Each pair is (ref_type, ref_pattern); content docs carry git.ref_pattern = the stream identity."""
    return {"aggregations": {"pairs": {"buckets": [
        {"key": {"ref_type": rt, "ref_pattern": r}} for rt, r in ref_pairs
    ]}}}


def _snapshot_presence_resp(found_commits: list[str]) -> dict:
    """Response for snapshot join-doc presence check — terms agg 'commits'."""
    return {"aggregations": {"commits": {"buckets": [
        {"key": c, "doc_count": 1} for c in found_commits
    ]}}}


def _incremental_unique_resp(pair_counts: dict[tuple[str, str], int]) -> dict:
    """Response for incremental uniqueness check — composite agg 'ref_pairs' on git.ref_pattern.
    pair_counts is {(ref_type, ref_pattern): doc_count}."""
    return {"aggregations": {"ref_pairs": {"buckets": [
        {"key": {"ref_type": rt, "ref_pattern": r}, "doc_count": cnt}
        for (rt, r), cnt in pair_counts.items()
    ]}}}


class TestCheckJoinUniqueness:
    """Tests for check_join_uniqueness: the combined snapshot + incremental gate."""

    def _make_es(
        self,
        snapshot_commits: list[str] = (),
        snapshot_found: list[str] = (),
        incremental_ref_pairs: list[tuple[str, str]] = (),
        incremental_pair_counts: dict[tuple[str, str], int] | None = None,
    ) -> MagicMock:
        """Build a mock ES with side_effects matching the exact call order of check_join_uniqueness:

          1. _enumerate_content_field(git.commit): 1 search per index (FILES, LINES)
             → composite agg 'keys', each bucket {'key': {'val': <commit>}}
          2. if commits non-empty → snapshot presence check (1 search on sourcerer-refs)
             → terms agg 'commits'
          3. _enumerate_incremental_content_ref_pairs: 1 search per index (FILES, LINES)
             → composite agg 'pairs', each bucket {'key': {'ref_type': ..., 'ref_pattern': ...}}
          4. if ref_pairs non-empty → incremental uniqueness check (1 search on sourcerer-refs)
             → composite agg 'ref_pairs' (on git.ref_pattern), each bucket
             {'key': {'ref_type': ..., 'ref_pattern': ...}, 'doc_count': N}

        The composite agg loop breaks on the first empty page, so exactly one search per index.
        """
        es = MagicMock()
        side_effects: list[dict] = []
        # (1) enumerate git.commit: FILES then LINES
        side_effects.append(_commit_enum_resp(list(snapshot_commits)))  # FILES
        side_effects.append(_commit_enum_resp(list(snapshot_commits)))  # LINES
        # (2) snapshot join-doc presence check (only if commits found)
        if snapshot_commits:
            side_effects.append(_snapshot_presence_resp(list(snapshot_found)))
        # (3) enumerate incremental (ref_type, ref) pairs: FILES then LINES
        side_effects.append(_ref_enum_resp(list(incremental_ref_pairs)))  # FILES
        side_effects.append(_ref_enum_resp(list(incremental_ref_pairs)))  # LINES
        # (4) incremental uniqueness check on refs-side `match` (only if pairs found)
        if incremental_ref_pairs:
            counts = incremental_pair_counts or {}
            side_effects.append(_incremental_unique_resp(counts))
        es.search.side_effect = side_effects
        return es

    def test_clean_repo_no_content(self):
        """No content at all → gate passes."""
        es = MagicMock()
        # All composite enumeration calls return empty buckets.
        # Steps 1+3: commit enum (keys agg, empty) and ref-pair enum (pairs agg, empty).
        # We return a response that satisfies both agg names by making the mock return
        # the correct format for each call in sequence.
        es.search.side_effect = [
            _commit_enum_resp([]),   # step 1a FILES git.commit
            _commit_enum_resp([]),   # step 1b LINES git.commit
            _ref_enum_resp([]),      # step 3a FILES ref pairs
            _ref_enum_resp([]),      # step 3b LINES ref pairs
        ]
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []

    def test_clean_snapshot_all_present(self):
        """Snapshot commits all have a complete refs doc → clean."""
        es = self._make_es(
            snapshot_commits=["aaa"],
            snapshot_found=["aaa"],
        )
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []

    def test_snapshot_missing_refs_doc_is_offending(self):
        """A snapshot commit with no complete refs doc is reported."""
        es = self._make_es(
            snapshot_commits=["aaa"],
            snapshot_found=[],  # no complete refs doc found
        )
        result = check_join_uniqueness(es, "github", "acme", "widgets")
        assert "aaa" in result

    def test_clean_incremental_exactly_one_join_doc(self):
        """Incremental ref with exactly one join doc → clean."""
        es = self._make_es(
            incremental_ref_pairs=[("branch", "main")],
            incremental_pair_counts={("branch", "main"): 1},
        )
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []

    def test_clean_incremental_tag_stream_one_join_doc(self):
        """A delta-tag stream (pattern as identity) with one join doc → clean."""
        es = self._make_es(
            incremental_ref_pairs=[("tag", "deploy@{major}")],
            incremental_pair_counts={("tag", "deploy@{major}"): 1},
        )
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []

    def test_incremental_missing_join_doc_is_offending(self):
        """An incremental ref with no join doc is reported."""
        es = self._make_es(
            incremental_ref_pairs=[("branch", "main")],
            incremental_pair_counts={},  # zero docs found
        )
        result = check_join_uniqueness(es, "github", "acme", "widgets")
        assert "branch/main" in result

    def test_incremental_duplicate_join_doc_is_offending(self):
        """An incremental ref with more than one join doc (e.g. stale snapshot marker) is reported."""
        es = self._make_es(
            incremental_ref_pairs=[("branch", "main")],
            incremental_pair_counts={("branch", "main"): 2},  # two docs — fan-out!
        )
        result = check_join_uniqueness(es, "github", "acme", "widgets")
        assert "branch/main" in result

    def test_missing_index_contributes_nothing(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []


class TestRunUniquenessGate:
    def test_passes_silently_when_clean(self):
        es = MagicMock()
        # No content: all enumerations return empty pages.
        es.search.side_effect = [
            _commit_enum_resp([]),   # step 1a
            _commit_enum_resp([]),   # step 1b
            _ref_enum_resp([]),      # step 3a
            _ref_enum_resp([]),      # step 3b
        ]
        assert _run_uniqueness_gate(es, "github", "acme", "widgets") is True

    def test_fails_and_reports_on_snapshot_violation(self, capsys):
        es = MagicMock()
        # Snapshot commit "aaa" exists in content but has no complete refs doc.
        es.search.side_effect = [
            _commit_enum_resp(["aaa"]),          # step 1a: FILES git.commit
            _commit_enum_resp(["aaa"]),          # step 1b: LINES git.commit
            _snapshot_presence_resp([]),         # step 2: no complete refs doc for "aaa"
            _ref_enum_resp([]),                  # step 3a: no incremental refs
            _ref_enum_resp([]),                  # step 3b
        ]
        assert _run_uniqueness_gate(es, "github", "acme", "widgets") is False
        captured = capsys.readouterr()
        assert "aaa" in captured.err
