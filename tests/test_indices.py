"""Unit tests for the index-name builders in sourcerer.indices, including a round-trip
check against planner.parse_index_name (their documented inverse). v2 names carry a leading
git.host segment."""

# App packages
from sourcerer.indices import FILES_ALIAS, LINES_ALIAS, REFS_ALIAS, REFS_INDEX, files_index, lines_index
from sourcerer.planner import parse_index_name


class TestIndexNameBuilders:
    def test_read_aliases(self):
        assert (FILES_ALIAS, LINES_ALIAS, REFS_ALIAS) == (
            "sourcerer-files",
            "sourcerer-lines",
            "sourcerer-refs",
        )

    def test_refs_index_is_v2(self):
        assert REFS_INDEX == "sourcerer-v2-refs"

    def test_files_index_shape(self):
        assert files_index("github", "elastic", "elasticsearch") == \
            "sourcerer-v2-files~github~elastic~elasticsearch"

    def test_lines_index_shape(self):
        assert lines_index("github", "elastic", "elasticsearch") == \
            "sourcerer-v2-lines~github~elastic~elasticsearch"

    def test_host_org_repo_lowercased(self):
        assert files_index("GitHub", "Elastic", "ElasticSearch") == \
            "sourcerer-v2-files~github~elastic~elasticsearch"
        assert lines_index("MyGitea", "ACME", "Widgets") == \
            "sourcerer-v2-lines~mygitea~acme~widgets"

    def test_same_org_repo_distinct_hosts_differ(self):
        assert files_index("github", "acme", "w") != files_index("gitlab", "acme", "w")


class TestRoundTripWithParseIndexName:
    def test_files_index_round_trips(self):
        parsed = parse_index_name(files_index("github", "acme", "widgets"))
        assert (parsed.kind, parsed.host, parsed.org, parsed.repo, parsed.commit) == \
            ("files", "github", "acme", "widgets", None)

    def test_lines_index_round_trips(self):
        parsed = parse_index_name(lines_index("gitlab", "acme", "widgets"))
        assert (parsed.kind, parsed.host, parsed.org, parsed.repo, parsed.commit) == \
            ("lines", "gitlab", "acme", "widgets", None)

    def test_refs_index_is_not_parsed(self):
        assert parse_index_name(REFS_INDEX) is None

    def test_host_only_granularity(self):
        parsed = parse_index_name("sourcerer-v2-files~github")
        assert (parsed.host, parsed.org, parsed.repo, parsed.commit) == ("github", None, None, None)

    def test_host_org_repo_commit_granularity(self):
        parsed = parse_index_name("sourcerer-v2-files~github~acme~w~abc123")
        assert (parsed.host, parsed.org, parsed.repo, parsed.commit) == ("github", "acme", "w", "abc123")

    def test_too_many_segments_rejected(self):
        assert parse_index_name("sourcerer-v2-files~a~b~c~d~e") is None

    def test_empty_segment_rejected(self):
        assert parse_index_name("sourcerer-v2-files~github~~repo") is None
