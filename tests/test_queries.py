"""Unit tests for sourcerer.queries: the pure datetime-parsing helper, plus the merged
per-index and refs-scan gather helpers that feed the orphan sweep's stale-location detection
(gather_content_and_incremental_by_index, gather_intended_locations). Other ES-facing read
helpers (fetch_markers, list_sourcerer_indices, enumerate_*) are covered by
test_index_orphans.py."""

# Standard packages
from datetime import timezone
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.indices import REFS_ALIAS, files_index, lines_index
from sourcerer.queries import (
    _parse_dt,
    gather_content_and_incremental_by_index,
    gather_intended_locations,
)


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestParseDt:
    def test_none_returns_none(self):
        assert _parse_dt(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_dt("") is None

    def test_z_suffix_parses_to_aware_utc(self):
        result = _parse_dt("2025-01-01T00:00:00Z")
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0

    def test_explicit_offset_parses(self):
        result = _parse_dt("2025-01-01T00:00:00+02:00")
        assert result is not None
        assert result.utcoffset().total_seconds() == 2 * 3600

    def test_invalid_returns_none(self):
        assert _parse_dt("not-a-date") is None

    def test_naive_and_aware_compare_consistently(self):
        z = _parse_dt("2025-01-01T00:00:00Z")
        assert z.astimezone(timezone.utc).hour == 0


def _bucket(host, org, repo, commit=None, ref_type=None, ref_pattern=None) -> dict:
    return {"key": {"host": host, "org": org, "repo": repo,
                    "commit": commit, "ref_type": ref_type, "ref_pattern": ref_pattern}}


class TestGatherContentAndIncrementalByIndex:
    """gather_content_and_incremental_by_index: ONE composite aggregation per index (via
    missing_bucket:true) replaces the old gather_content_by_index + gather_incremental_content_by_index
    pair, partitioning snapshot vs. incremental tuples from the same response."""

    def test_partitions_snapshot_and_incremental_tuples_from_one_composite(self):
        es = MagicMock()

        def fake_search(index, size, aggs):
            buckets = [
                _bucket("github", "acme", "widgets", commit="aaa"),
                _bucket("github", "acme", "widgets", ref_type="branch", ref_pattern="main"),
            ]
            return {"aggregations": {"tuples": {"buckets": buckets}}}

        es.search.side_effect = fake_search
        content_by_index, incremental_by_index = gather_content_and_incremental_by_index(
            es, ["sourcerer-v3-files~github~acme~widgets"]
        )
        assert content_by_index == {
            "sourcerer-v3-files~github~acme~widgets": {("github", "acme", "widgets", "aaa")}
        }
        assert incremental_by_index == {
            "sourcerer-v3-files~github~acme~widgets": {("github", "acme", "widgets", "branch", "main")}
        }

    def test_composite_sources_use_missing_bucket_on_the_disjoint_fields(self):
        es = MagicMock()
        es.search.return_value = {"aggregations": {"tuples": {"buckets": []}}}
        gather_content_and_incremental_by_index(es, ["idx1"])
        sources = es.search.call_args.kwargs["aggs"]["tuples"]["composite"]["sources"]
        by_name = {list(s.keys())[0]: s for s in sources}
        assert by_name["commit"]["commit"]["terms"]["missing_bucket"] is True
        assert by_name["ref_type"]["ref_type"]["terms"]["missing_bucket"] is True
        assert by_name["ref_pattern"]["ref_pattern"]["terms"]["missing_bucket"] is True
        assert "missing_bucket" not in by_name["host"]["host"]["terms"]

    def test_missing_index_contributes_nothing(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        content_by_index, incremental_by_index = gather_content_and_incremental_by_index(es, ["gone"])
        assert content_by_index == {}
        assert incremental_by_index == {}

    def test_empty_index_names_short_circuits_without_calling_es(self):
        es = MagicMock()
        content_by_index, incremental_by_index = gather_content_and_incremental_by_index(es, [])
        assert (content_by_index, incremental_by_index) == ({}, {})
        es.search.assert_not_called()

    def test_paginates_per_index_until_no_after_key(self):
        es = MagicMock()
        calls = {"n": 0}

        def fake_search(index, size, aggs):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"aggregations": {"tuples": {
                    "buckets": [_bucket("github", "acme", "widgets", commit="aaa")],
                    "after_key": {"host": "github"},
                }}}
            return {"aggregations": {"tuples": {
                "buckets": [_bucket("github", "acme", "widgets", commit="bbb")],
            }}}

        es.search.side_effect = fake_search
        content_by_index, _ = gather_content_and_incremental_by_index(es, ["idx1"])
        assert content_by_index["idx1"] == {
            ("github", "acme", "widgets", "aaa"), ("github", "acme", "widgets", "bbb"),
        }


class TestGatherIntendedLocations:
    """gather_intended_locations: ONE refs scan replaces gather_intended_index_by_commit +
    gather_intended_incremental_index_by_ref, branching per-hit on `mode` instead of running two
    separate scans."""

    def test_snapshot_and_delta_docs_populate_respective_maps(self, monkeypatch):
        hits = [
            {"_source": {"git": {"host": "github", "org": "acme", "repo": "widgets", "commit": "aaa"},
                        "mode": "snapshot", "index_level": "repo", "index_suffix": None}},
            {"_source": {"git": {"host": "github", "org": "acme", "repo": "widgets",
                                 "ref_type": "branch", "ref_pattern": "main", "commit": "bbb"},
                        "mode": "delta", "index_level": "repo", "index_suffix": None}},
        ]
        monkeypatch.setattr(
            "sourcerer.queries.scan",
            lambda es, index, query, _source, preserve_order: iter(hits),
        )
        by_commit, by_ref = gather_intended_locations(MagicMock())
        assert by_commit[("github", "acme", "widgets", "aaa")] == {
            files_index("github", "acme", "widgets", "aaa"),
            lines_index("github", "acme", "widgets", "aaa"),
        }
        assert by_commit[("github", "acme", "widgets", "bbb")] == {
            files_index("github", "acme", "widgets", "bbb"),
            lines_index("github", "acme", "widgets", "bbb"),
        }
        assert by_ref[("github", "acme", "widgets", "branch", "main")] == {
            files_index("github", "acme", "widgets"),
            lines_index("github", "acme", "widgets"),
        }

    def test_snapshot_marker_ref_pattern_does_not_leak_into_incremental_map(self, monkeypatch):
        # Snapshot markers also carry git.ref_pattern (defaulted to ref) -- must be excluded
        # from the incremental map since mode != "delta".
        hits = [
            {"_source": {"git": {"host": "github", "org": "acme", "repo": "widgets", "commit": "aaa",
                                 "ref_type": "branch", "ref_pattern": "main"},
                        "mode": "snapshot", "index_level": "repo", "index_suffix": None}},
        ]
        monkeypatch.setattr(
            "sourcerer.queries.scan",
            lambda es, index, query, _source, preserve_order: iter(hits),
        )
        by_commit, by_ref = gather_intended_locations(MagicMock())
        assert by_ref == {}
        assert by_commit

    def test_missing_index_returns_empty_dicts(self, monkeypatch):
        def raise_not_found(*a, **kw):
            raise _not_found()

        monkeypatch.setattr("sourcerer.queries.scan", raise_not_found)
        assert gather_intended_locations(MagicMock()) == ({}, {})

    def test_scope_adds_query_filter(self, monkeypatch):
        captured = {}

        def fake_scan(es, index, query, _source, preserve_order):
            captured["query"] = query
            return iter([])

        monkeypatch.setattr("sourcerer.queries.scan", fake_scan)
        gather_intended_locations(MagicMock(), host="github", org="acme", repo="widgets")
        assert captured["query"]["query"]["bool"]["filter"] == [
            {"term": {"git.host": "github"}},
            {"term": {"git.org": "acme"}},
            {"term": {"git.repo": "widgets"}},
        ]

    def test_no_scope_uses_match_all(self, monkeypatch):
        captured = {}

        def fake_scan(es, index, query, _source, preserve_order):
            captured["query"] = query
            return iter([])

        monkeypatch.setattr("sourcerer.queries.scan", fake_scan)
        gather_intended_locations(MagicMock())
        assert captured["query"] == {"query": {"match_all": {}}}
