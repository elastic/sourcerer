"""Unit tests for the pure id-hashing helper in sourcerer.utils, plus make_client TLS options."""

# Standard packages
from unittest.mock import MagicMock, patch

# Third-party packages
from elasticsearch.helpers import BulkIndexError

# App packages
from sourcerer.utils import format_es_error, make_doc_id, make_client, build_ref_key


class TestMakeDocId:
    def test_deterministic_for_same_parts(self):
        assert make_doc_id("acme", "widgets", "aaa", "a.txt") == make_doc_id(
            "acme", "widgets", "aaa", "a.txt"
        )

    def test_order_sensitive(self):
        assert make_doc_id("a", "b") != make_doc_id("b", "a")

    def test_nul_separator_prevents_boundary_collisions(self):
        # Without a separator, ("a", "bc") and ("ab", "c") would hash identically.
        assert make_doc_id("a", "bc") != make_doc_id("ab", "c")

    def test_returns_hex_string_of_expected_length(self):
        # ID_DIGEST_SIZE=16 bytes -> 32 hex chars.
        doc_id = make_doc_id("acme", "widgets")
        assert len(doc_id) == 32
        int(doc_id, 16)  # raises ValueError if not valid hex

    def test_non_utf8_parts_round_trip_via_surrogateescape(self):
        # A path with a byte that isn't valid UTF-8, decoded upstream (iter_tracked_files)
        # with surrogateescape, must still produce a stable id rather than raising.
        weird = b"\xff\xfe".decode("utf-8", errors="surrogateescape")
        assert make_doc_id("acme", "widgets", weird) == make_doc_id("acme", "widgets", weird)


class TestBuildRefKey:
    def test_ref_key_incremental_shape(self):
        assert build_ref_key("github", "elastic", "sourcerer", "branch", "main") == (
            "github~elastic~sourcerer~branch~main"
        )

    def test_ref_key_lowercases_host_org_repo_preserves_ref_case(self):
        assert build_ref_key("GitHub", "Elastic", "Sourcerer", "branch", "Feature/Mixed-Case") == (
            "github~elastic~sourcerer~branch~Feature/Mixed-Case"
        )

    def test_ref_key_tag_stream_keys_on_pattern(self):
        """A delta-tag stream's ref key uses the match pattern, not the concrete tag."""
        pattern = "deploy@{major}"
        concrete = "deploy@1788000000"
        assert build_ref_key("github", "elastic", "kibana", "tag", pattern) != (
            build_ref_key("github", "elastic", "kibana", "tag", concrete)
        )
        # The stable identity (pattern) produces a consistent key.
        assert build_ref_key("github", "elastic", "kibana", "tag", pattern) == (
            build_ref_key("github", "elastic", "kibana", "tag", pattern)
        )

    def test_ref_key_deterministic(self):
        assert build_ref_key("github", "acme", "widgets", "branch", "main") == build_ref_key(
            "github", "acme", "widgets", "branch", "main"
        )


class TestMakeClient:
    """make_client TLS behaviour — Elasticsearch constructor is patched, no real connection."""

    def _make(self, **kwargs):
        with patch("sourcerer.utils.Elasticsearch") as mock_es:
            make_client("https://es.example.com", "mykey", None, None, **kwargs)
            return mock_es

    def test_default_does_not_set_verify_certs(self):
        """Without --insecure, verify_certs is not passed so the ES client uses its default (True)."""
        mock_es = self._make()
        _, call_kwargs = mock_es.call_args
        assert "verify_certs" not in call_kwargs

    def test_insecure_passes_verify_certs_false(self):
        """With insecure=True, verify_certs=False is forwarded to the ES constructor."""
        mock_es = self._make(insecure=True)
        _, call_kwargs = mock_es.call_args
        assert call_kwargs.get("verify_certs") is False

    def test_insecure_false_does_not_set_verify_certs(self):
        """Explicitly passing insecure=False is the same as the default."""
        mock_es = self._make(insecure=False)
        _, call_kwargs = mock_es.call_args
        assert "verify_certs" not in call_kwargs


class TestFormatEsError:
    def _bulk_error(self, items):
        """Build a BulkIndexError the way elasticsearch-py's parallel_bulk does: one
        {op_type: item} dict per failed document, item carrying status + error.type/reason."""
        errors = [
            {"index": {"_index": "sourcerer-v3-lines~github~apache~hadoop", "_id": doc_id,
                       "status": status, "error": {"type": etype, "reason": reason}}}
            for doc_id, status, etype, reason in items
        ]
        return BulkIndexError(f"{len(errors)} document(s) failed to index.", errors)

    def test_non_bulk_error_falls_back_to_str(self):
        assert format_es_error(ValueError("boom")) == "boom"

    def test_single_distinct_error_included_in_detail(self):
        e = self._bulk_error([
            ("a", 400, "mapper_parsing_exception", "failed to parse field [file.content]"),
        ])
        detail = format_es_error(e)
        assert "1 document(s) failed to index." in detail
        assert "mapper_parsing_exception: failed to parse field [file.content]" in detail

    def test_repeated_identical_error_is_deduped_with_count(self):
        e = self._bulk_error([
            ("a", 400, "mapper_parsing_exception", "field too large"),
            ("b", 400, "mapper_parsing_exception", "field too large"),
            ("c", 400, "mapper_parsing_exception", "field too large"),
        ])
        detail = format_es_error(e)
        assert "mapper_parsing_exception: field too large (x3)" in detail
        # Only one distinct message, so no "...and N more" suffix.
        assert "more distinct error" not in detail

    def test_multiple_distinct_errors_are_each_shown(self):
        e = self._bulk_error([
            ("a", 400, "mapper_parsing_exception", "field too large"),
            ("b", 409, "version_conflict_engine_exception", "current version is higher"),
        ])
        detail = format_es_error(e)
        assert "mapper_parsing_exception: field too large" in detail
        assert "version_conflict_engine_exception: current version is higher" in detail

    def test_many_distinct_errors_are_capped_with_omitted_count(self):
        items = [
            (str(i), 400, f"error_type_{i}", f"reason {i}")
            for i in range(5)
        ]
        e = self._bulk_error(items)
        detail = format_es_error(e)
        assert "...and 2 more distinct error(s)" in detail
        # Most frequent are stable here since each occurs once; just assert the cap held.
        assert detail.count("error_type_") == 3
