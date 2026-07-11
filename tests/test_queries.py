"""Unit tests for the pure datetime-parsing helper in sourcerer.queries. The ES-facing read
helpers (fetch_markers, list_sourcerer_indices, enumerate_*) are already covered by
test_index_orphans.py."""

# Standard packages
from datetime import timezone

# App packages
from sourcerer.queries import _parse_dt


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
