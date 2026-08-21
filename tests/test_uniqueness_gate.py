"""Tests for the post-index join-uniqueness gate: sourcerer.queries.check_join_uniqueness
(INV-011) and its command.py wiring in _run_uniqueness_gate. Every ES call is mocked.

The gate is split by content shape (no mode on content docs):
  - Snapshot (git.commit IS NOT NULL): each commit must have ≥1 complete refs doc.
  - Incremental (git.ref IS NOT NULL): each ref must have EXACTLY ONE incremental join doc.
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


def _composite_resp(values: list[str]) -> dict:
    """Build a composite agg response with the given values."""
    return {"aggregations": {"keys": {"buckets": [{"key": {"val": v}} for v in values]}}}


def _terms_resp(counts: dict[str, int]) -> dict:
    """Build a terms agg response mapping key -> doc_count."""
    return {"aggregations": {
        "commits": {"buckets": [{"key": k, "doc_count": v} for k, v in counts.items()]},
        "refs": {"buckets": [{"key": k, "doc_count": v} for k, v in counts.items()]},
    }}


class TestCheckJoinUniqueness:
    """Tests for check_join_uniqueness: the combined snapshot + incremental gate."""

    def _make_es(self, snapshot_commits=(), snapshot_found=(), incremental_refs=(), incremental_counts=None):
        """Build a mock ES with side_effects matching the exact call order of check_join_uniqueness:

          1. _enumerate_content_field(git.commit): 1 search per index (FILES, LINES)
          2. if commits non-empty → snapshot presence check (1 search on sourcerer-refs)
          3. _enumerate_content_field(git.ref): 1 search per index (FILES, LINES)
          4. if refs non-empty → incremental uniqueness check (1 search on sourcerer-refs)

        The composite agg loop breaks on the first empty page (no after_key returned), so exactly
        one search per index per field enumeration.
        """
        es = MagicMock()
        side_effects = []
        # (1) enumerate git.commit: FILES then LINES
        side_effects.append(_composite_resp(list(snapshot_commits)))  # FILES git.commit
        side_effects.append(_composite_resp(list(snapshot_commits)))  # LINES git.commit
        # (2) snapshot join-doc presence check (only if commits found)
        if snapshot_commits:
            found = {c: 1 for c in snapshot_found}
            side_effects.append({"aggregations": {"commits": {"buckets": [
                {"key": k, "doc_count": v} for k, v in found.items()
            ]}}})
        # (3) enumerate git.ref: FILES then LINES
        side_effects.append(_composite_resp(list(incremental_refs)))  # FILES git.ref
        side_effects.append(_composite_resp(list(incremental_refs)))  # LINES git.ref
        # (4) incremental uniqueness check (only if refs found)
        if incremental_refs:
            counts = incremental_counts or {}
            side_effects.append({"aggregations": {"refs": {"buckets": [
                {"key": k, "doc_count": v} for k, v in counts.items()
            ]}}})
        es.search.side_effect = side_effects
        return es

    def test_clean_repo_no_content(self):
        """No content at all → gate passes."""
        es = MagicMock()
        es.search.return_value = {"aggregations": {"keys": {"buckets": []}}}
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
            incremental_refs=["main"],
            incremental_counts={"main": 1},
        )
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []

    def test_incremental_missing_join_doc_is_offending(self):
        """An incremental ref with no join doc is reported."""
        es = self._make_es(
            incremental_refs=["main"],
            incremental_counts={},  # zero docs found
        )
        result = check_join_uniqueness(es, "github", "acme", "widgets")
        assert "main" in result

    def test_incremental_duplicate_join_doc_is_offending(self):
        """An incremental ref with more than one join doc (e.g. stale snapshot marker) is reported."""
        es = self._make_es(
            incremental_refs=["main"],
            incremental_counts={"main": 2},  # two docs — fan-out!
        )
        result = check_join_uniqueness(es, "github", "acme", "widgets")
        assert "main" in result

    def test_missing_index_contributes_nothing(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert check_join_uniqueness(es, "github", "acme", "widgets") == []


class TestRunUniquenessGate:
    def test_passes_silently_when_clean(self):
        es = MagicMock()
        # No content: all composite aggs return empty
        es.search.return_value = {"aggregations": {"keys": {"buckets": []}}}
        assert _run_uniqueness_gate(es, "github", "acme", "widgets") is True

    def test_fails_and_reports_on_snapshot_violation(self, capsys):
        es = MagicMock()
        # Snapshot commit "aaa" exists in content but has no complete refs doc.
        # sources structure: [{"val": {"terms": {"field": "git.commit"}}}]
        def side_effect(*args, **kwargs):
            aggs = kwargs.get("aggs", {})
            if "keys" in aggs and aggs["keys"].get("composite"):
                sources = aggs["keys"]["composite"].get("sources", [])
                # Each source is {"<alias>": {"terms": {"field": "<field>"}}}
                field = None
                if sources:
                    src = sources[0]
                    for alias_val in src.values():
                        field = alias_val.get("terms", {}).get("field")
                if field == "git.commit":
                    return {"aggregations": {"keys": {"buckets": [{"key": {"val": "aaa"}}]}}}
                return {"aggregations": {"keys": {"buckets": []}}}
            # terms agg for join doc presence (snapshot or incremental)
            return {"aggregations": {"commits": {"buckets": []}, "refs": {"buckets": []}}}
        es.search.side_effect = side_effect
        assert _run_uniqueness_gate(es, "github", "acme", "widgets") is False
        captured = capsys.readouterr()
        assert "aaa" in captured.err
