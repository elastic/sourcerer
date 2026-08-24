"""Unit tests for the pure id-hashing helper in sourcerer.utils, plus make_client TLS options."""

# Standard packages
from unittest.mock import MagicMock, patch

# App packages
from sourcerer.utils import make_doc_id, make_client, build_ref_key


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
