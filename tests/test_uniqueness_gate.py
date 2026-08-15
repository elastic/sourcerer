"""Tests for the post-upgrade uniqueness gate: sourcerer.queries.check_ref_key_uniqueness
(INV-011) and its command.py wiring in _run_uniqueness_gate. Every ES call is mocked."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
from elastic_transport import ApiResponseMeta, HttpHeaders
from elasticsearch import NotFoundError

# App packages
from sourcerer.commands.index.command import _run_uniqueness_gate
from sourcerer.queries import check_ref_key_uniqueness, enumerate_content_ref_keys


def _not_found() -> NotFoundError:
    meta = ApiResponseMeta(status=404, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return NotFoundError("index_not_found_exception", meta, None)


class TestEnumerateContentRefKeys:
    def test_collects_keys_across_both_aliases(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"keys": {"buckets": [{"key": {"ref_key": "aaa"}}]}}},  # files
            {"aggregations": {"keys": {"buckets": [{"key": {"ref_key": "bbb"}}]}}},  # lines
        ]
        assert enumerate_content_ref_keys(es, "github", "acme", "widgets") == {"aaa", "bbb"}

    def test_missing_index_contributes_nothing(self):
        es = MagicMock()
        es.search.side_effect = _not_found()
        assert enumerate_content_ref_keys(es, "github", "acme", "widgets") == set()


class TestCheckRefKeyUniqueness:
    def test_clean_repo_returns_empty(self):
        es = MagicMock()
        es.search.side_effect = [
            # enumerate_content_ref_keys: files then lines
            {"aggregations": {"keys": {"buckets": [{"key": {"ref_key": "aaa"}}]}}},
            {"aggregations": {"keys": {"buckets": []}}},
            # uniqueness count query
            {"aggregations": {"keys": {"buckets": [{"key": "aaa", "doc_count": 1}]}}},
        ]
        assert check_ref_key_uniqueness(es, "github", "acme", "widgets") == []

    def test_missing_join_doc_is_offending(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"keys": {"buckets": [{"key": {"ref_key": "aaa"}}]}}},
            {"aggregations": {"keys": {"buckets": []}}},
            {"aggregations": {"keys": {"buckets": []}}},  # no join doc at all
        ]
        assert check_ref_key_uniqueness(es, "github", "acme", "widgets") == ["aaa"]

    def test_duplicate_join_doc_is_offending(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"keys": {"buckets": [{"key": {"ref_key": "aaa"}}]}}},
            {"aggregations": {"keys": {"buckets": []}}},
            {"aggregations": {"keys": {"buckets": [{"key": "aaa", "doc_count": 2}]}}},
        ]
        assert check_ref_key_uniqueness(es, "github", "acme", "widgets") == ["aaa"]

    def test_no_content_short_circuits(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"keys": {"buckets": []}}},
            {"aggregations": {"keys": {"buckets": []}}},
        ]
        assert check_ref_key_uniqueness(es, "github", "acme", "widgets") == []


class TestRunUniquenessGate:
    def test_passes_silently_when_clean(self):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"keys": {"buckets": []}}},
            {"aggregations": {"keys": {"buckets": []}}},
        ]
        assert _run_uniqueness_gate(es, "github", "acme", "widgets") is True

    def test_fails_and_reports_on_violation(self, capsys):
        es = MagicMock()
        es.search.side_effect = [
            {"aggregations": {"keys": {"buckets": [{"key": {"ref_key": "aaa"}}]}}},
            {"aggregations": {"keys": {"buckets": []}}},
            {"aggregations": {"keys": {"buckets": []}}},
        ]
        assert _run_uniqueness_gate(es, "github", "acme", "widgets") is False
        captured = capsys.readouterr()
        assert "aaa" in captured.err
