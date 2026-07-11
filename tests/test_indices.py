"""Unit tests for the index-name builders in sourcerer.indices, including a round-trip
check against planner.parse_index_name (their documented inverse)."""

# App packages
from sourcerer.indices import files_index, lines_index
from sourcerer.planner import parse_index_name


class TestIndexNameBuilders:
    def test_files_index_shape(self):
        assert files_index("elastic", "elasticsearch") == "sourcerer-v1-files~elastic~elasticsearch"

    def test_lines_index_shape(self):
        assert lines_index("elastic", "elasticsearch") == "sourcerer-v1-lines~elastic~elasticsearch"

    def test_org_and_repo_are_lowercased(self):
        assert files_index("Elastic", "ElasticSearch") == "sourcerer-v1-files~elastic~elasticsearch"
        assert lines_index("ACME", "Widgets") == "sourcerer-v1-lines~acme~widgets"


class TestRoundTripWithParseIndexName:
    def test_files_index_round_trips(self):
        name = files_index("acme", "widgets")
        parsed = parse_index_name(name)
        assert (parsed.kind, parsed.org, parsed.repo, parsed.commit) == ("files", "acme", "widgets", None)

    def test_lines_index_round_trips(self):
        name = lines_index("acme", "widgets")
        parsed = parse_index_name(name)
        assert (parsed.kind, parsed.org, parsed.repo, parsed.commit) == ("lines", "acme", "widgets", None)
