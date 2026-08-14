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

    def test_level_host(self):
        assert files_index("github", "elastic", "kibana", level="host") == "sourcerer-v2-files~github"

    def test_level_org(self):
        assert files_index("github", "Elastic", "Kibana", level="org") == "sourcerer-v2-files~github~elastic"

    def test_level_commit_requires_commit(self):
        assert lines_index("github", "elastic", "kibana", commit="ABC123", level="commit") == \
            "sourcerer-v2-lines~github~elastic~kibana~abc123"
        import pytest
        with pytest.raises(ValueError):
            files_index("github", "elastic", "kibana", level="commit")  # no commit

    def test_suffix_appended_with_caret(self):
        assert files_index("github", "elastic", "kibana", suffix="deploy") == \
            "sourcerer-v2-files~github~elastic~kibana^deploy"

    def test_suffix_lowercased(self):
        assert files_index("github", "elastic", "kibana", suffix="Deploy") == \
            "sourcerer-v2-files~github~elastic~kibana^deploy"

    def test_empty_suffix_is_no_suffix(self):
        assert files_index("github", "elastic", "kibana", suffix="") == \
            "sourcerer-v2-files~github~elastic~kibana"

    def test_level_and_suffix_combine(self):
        assert files_index("github", "elastic", "kibana", commit="abc", level="commit", suffix="deploy") == \
            "sourcerer-v2-files~github~elastic~kibana~abc^deploy"


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

    def test_suffix_parsed_and_identity_suffix_blind(self):
        parsed = parse_index_name("sourcerer-v2-files~github~acme~widgets^deploy")
        assert (parsed.host, parsed.org, parsed.repo, parsed.commit, parsed.suffix) == \
            ("github", "acme", "widgets", None, "deploy")

    def test_commit_level_with_suffix(self):
        parsed = parse_index_name("sourcerer-v2-lines~github~acme~w~abc123^deploy")
        assert (parsed.repo, parsed.commit, parsed.suffix) == ("w", "abc123", "deploy")

    def test_trailing_caret_is_malformed(self):
        assert parse_index_name("sourcerer-v2-files~github~acme~widgets^") is None

    def test_round_trip_invariant_all_levels_and_suffix(self):
        """The builder <-> parser contract: parse_index_name(files_index(...)) recovers the inputs
        for every level/suffix. This pins the naming formula so a future change that would break
        reconstruction (the migration/prune deletion path relies on it) fails loudly here."""
        cases = [
            ("host", None, None),
            ("org", None, None),
            ("repo", None, None),
            ("repo", None, "deploy"),
            ("commit", "abc1234", None),
            ("commit", "abc1234", "deploy"),
        ]
        for level, commit, suffix in cases:
            name = files_index("github", "acme", "widgets", commit=commit, level=level, suffix=suffix)
            p = parse_index_name(name)
            assert p is not None, name
            assert p.name == name
            assert p.suffix == suffix
            # Identity segments present at this level round-trip.
            if level in ("org", "repo", "commit"):
                assert p.org == "acme"
            if level in ("repo", "commit"):
                assert p.repo == "widgets"
            if level == "commit":
                assert p.commit == commit
