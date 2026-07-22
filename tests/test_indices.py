"""Unit tests for the index-name builders in sourcerer.indices, including a round-trip
check against planner.parse_index_name (their documented inverse)."""

# App packages
from sourcerer.indices import (
    files_index,
    files_index_v2,
    lines_index,
    lines_index_v2,
)
from sourcerer.planner import parse_index_name


class TestIndexNameBuilders:
    def test_files_index_shape(self):
        assert files_index("elastic", "elasticsearch") == "sourcerer-v1-files~elastic~elasticsearch"

    def test_lines_index_shape(self):
        assert lines_index("elastic", "elasticsearch") == "sourcerer-v1-lines~elastic~elasticsearch"

    def test_org_and_repo_are_lowercased(self):
        assert files_index("Elastic", "ElasticSearch") == "sourcerer-v1-files~elastic~elasticsearch"
        assert lines_index("ACME", "Widgets") == "sourcerer-v1-lines~acme~widgets"


class TestV2IndexNameBuilders:
    def test_files_index_v2_shape(self):
        assert files_index_v2("elastic", "elasticsearch") == "sourcerer-v2-files~elastic~elasticsearch"

    def test_lines_index_v2_shape(self):
        assert lines_index_v2("elastic", "elasticsearch") == "sourcerer-v2-lines~elastic~elasticsearch"

    def test_v2_org_and_repo_are_lowercased(self):
        # The index NAME is lowercased for a stable physical index per repo; case sensitivity
        # of ref identity is enforced inside the documents/ref-key, not the index name.
        assert files_index_v2("Elastic", "ElasticSearch") == "sourcerer-v2-files~elastic~elasticsearch"
        assert lines_index_v2("ACME", "Widgets") == "sourcerer-v2-lines~acme~widgets"


class TestRoundTripWithParseIndexName:
    def test_files_index_round_trips(self):
        name = files_index("acme", "widgets")
        parsed = parse_index_name(name)
        assert (parsed.kind, parsed.org, parsed.repo, parsed.commit) == ("files", "acme", "widgets", None)

    def test_lines_index_round_trips(self):
        name = lines_index("acme", "widgets")
        parsed = parse_index_name(name)
        assert (parsed.kind, parsed.org, parsed.repo, parsed.commit) == ("lines", "acme", "widgets", None)
