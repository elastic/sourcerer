"""Unit tests for the pure id-hashing helper in sourcerer.utils. make_client/is_serverless
are ES-facing and out of scope here."""

# App packages
from sourcerer.utils import make_doc_id


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
