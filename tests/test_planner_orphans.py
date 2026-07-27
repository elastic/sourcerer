"""Unit tests for the pure orphan-detection helpers in sourcerer.planner. No Elasticsearch
here -- every case is expressed as plain index-name lists and (host, org, repo, commit) tuple
sets. v2 index names carry a leading host segment; repo tuples are keyed (host, org, repo)."""

# App packages
from sourcerer.planner import (
    ParsedIndex,
    orphan_content_commits,
    orphan_indices,
    orphan_markers,
    parse_index_name,
    plan_orphans,
)


class TestParseIndexName:
    def test_host_org(self):
        assert parse_index_name("sourcerer-v2-lines~github~acme~widgets") == ParsedIndex(
            kind="lines", host="github", org="acme", repo="widgets", commit=None,
            name="sourcerer-v2-lines~github~acme~widgets",
        )

    def test_host_org_repo_commit(self):
        parsed = parse_index_name("sourcerer-v2-files~github~acme~widgets~deadbeef")
        assert parsed == ParsedIndex(
            kind="files", host="github", org="acme", repo="widgets", commit="deadbeef",
            name="sourcerer-v2-files~github~acme~widgets~deadbeef",
        )

    def test_refs_index_is_not_a_files_or_lines_index(self):
        assert parse_index_name("sourcerer-v2-refs") is None

    def test_unrelated_index_returns_none(self):
        assert parse_index_name("kibana_sample_data_ecommerce") is None

    def test_too_many_segments_returns_none(self):
        assert parse_index_name("sourcerer-v2-files~a~b~c~d~e") is None

    def test_empty_segment_returns_none(self):
        assert parse_index_name("sourcerer-v2-files~github~acme~~deadbeef") is None

    def test_custom_prefixes(self):
        parsed = parse_index_name("myprefix-files~github~acme~widgets", files_prefix="myprefix-files")
        assert parsed.host == "github" and parsed.org == "acme" and parsed.repo == "widgets"


class TestOrphanIndices:
    def test_host_org_repo_index_with_no_ref_repo_is_orphaned(self):
        names = ["sourcerer-v2-files~github~acme~widgets"]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v2-files~github~acme~widgets"]

    def test_index_with_ref_repo_is_not_orphaned(self):
        names = ["sourcerer-v2-files~github~acme~widgets", "sourcerer-v2-lines~github~acme~widgets"]
        result = orphan_indices(
            names, ref_orgs={("github", "acme")}, ref_repos={("github", "acme", "widgets")}, ref_commits=set()
        )
        assert result == []

    def test_same_repo_distinct_hosts_judged_independently(self):
        # github/acme/widgets is in refs; gitlab/acme/widgets is not -> only the gitlab index
        # is an orphan.
        names = [
            "sourcerer-v2-files~github~acme~widgets",
            "sourcerer-v2-files~gitlab~acme~widgets",
        ]
        result = orphan_indices(
            names, ref_orgs={("github", "acme")}, ref_repos={("github", "acme", "widgets")}, ref_commits=set()
        )
        assert result == ["sourcerer-v2-files~gitlab~acme~widgets"]

    def test_org_level_orphan_subsumes_repo_and_commit_level_of_the_same_kind(self):
        names = [
            "sourcerer-v2-files~github~acme",
            "sourcerer-v2-files~github~acme~widgets",
            "sourcerer-v2-files~github~acme~widgets~deadbeef",
        ]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v2-files~github~acme"]

    def test_repo_level_orphan_subsumes_commit_level(self):
        names = [
            "sourcerer-v2-files~github~acme~widgets",
            "sourcerer-v2-files~github~acme~widgets~deadbeef",
        ]
        result = orphan_indices(names, ref_orgs={("github", "acme")}, ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v2-files~github~acme~widgets"]

    def test_commit_level_not_orphaned_when_ref_commit_present(self):
        names = ["sourcerer-v2-files~github~acme~widgets~deadbeef"]
        result = orphan_indices(
            names,
            ref_orgs={("github", "acme")},
            ref_repos={("github", "acme", "widgets")},
            ref_commits={("github", "acme", "widgets", "deadbeef")},
        )
        assert result == []

    def test_subsumption_does_not_cross_files_and_lines(self):
        names = [
            "sourcerer-v2-files~github~acme~widgets",
            "sourcerer-v2-lines~github~acme~widgets~aaa",
            "sourcerer-v2-lines~github~acme~widgets~bbb",
        ]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert set(result) == set(names)

    def test_unparseable_names_are_ignored(self):
        names = ["sourcerer-v2-refs", "some-other-index"]
        assert orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set()) == []


class TestOrphanContentCommits:
    def test_commit_with_no_marker_is_orphaned(self):
        content = {("github", "acme", "widgets"): {"aaa", "bbb"}}
        refs = {("github", "acme", "widgets"): {"aaa"}}
        result = orphan_content_commits(content, refs, skip_repos=set())
        assert result == {("github", "acme", "widgets"): {"bbb"}}

    def test_skip_repos_excludes_class_a_repos(self):
        content = {("github", "acme", "widgets"): {"aaa"}}
        result = orphan_content_commits(content, ref_commits_by_repo={}, skip_repos={("github", "acme", "widgets")})
        assert result == {}


class TestOrphanMarkers:
    def test_commit_with_no_content_is_orphaned(self):
        refs = {("github", "acme", "widgets"): {"aaa", "bbb"}}
        content = {("github", "acme", "widgets"): {"aaa"}}
        result = orphan_markers(refs, content, skip_repos=set())
        assert result == {("github", "acme", "widgets"): {"bbb"}}

    def test_skip_repos_excludes_class_a_repos(self):
        refs = {("github", "acme", "widgets"): {"aaa"}}
        result = orphan_markers(refs, content_commits_by_repo={}, skip_repos={("github", "acme", "widgets")})
        assert result == {}


class TestPlanOrphans:
    def test_no_orphans(self):
        names = ["sourcerer-v2-files~github~acme~widgets", "sourcerer-v2-lines~github~acme~widgets"]
        ref_tuples = {("github", "acme", "widgets", "aaa")}
        content_tuples = {("github", "acme", "widgets", "aaa")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == []
        assert plan.orphan_content == {}
        assert plan.orphan_marker_commits == {}

    def test_class_a_subsumes_class_b_for_the_same_repo(self):
        names = ["sourcerer-v2-files~github~acme~widgets"]
        ref_tuples: set[tuple[str, str, str, str]] = set()
        content_tuples = {("github", "acme", "widgets", "bbb")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == ["sourcerer-v2-files~github~acme~widgets"]
        assert plan.orphan_content == {}
        assert plan.orphan_marker_commits == {}

    def test_class_b_and_c_fire_independently_when_index_present(self):
        names = ["sourcerer-v2-files~github~acme~widgets"]
        ref_tuples = {("github", "acme", "widgets", "aaa"), ("github", "acme", "widgets", "bbb")}
        content_tuples = {("github", "acme", "widgets", "aaa"), ("github", "acme", "widgets", "ccc")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == []
        assert plan.orphan_content == {("github", "acme", "widgets"): {"ccc"}}
        assert plan.orphan_marker_commits == {("github", "acme", "widgets"): {"bbb"}}

    def test_same_repo_two_hosts_do_not_cross_contaminate(self):
        # github has a marker with no content -> Class-C (marker orphan). gitlab has an index +
        # content but no ref entry at all -> the whole gitlab index is a Class-A orphan, so its
        # content is subsumed by the index DELETE (not a separate Class-B query). The two hosts
        # must be judged independently: github's marker orphan must not be cancelled by gitlab's
        # content just because org/repo match.
        names = [
            "sourcerer-v2-files~github~acme~widgets",
            "sourcerer-v2-files~gitlab~acme~widgets",
        ]
        ref_tuples = {("github", "acme", "widgets", "aaa")}
        content_tuples = {("gitlab", "acme", "widgets", "bbb")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == ["sourcerer-v2-files~gitlab~acme~widgets"]
        assert plan.orphan_content == {}  # gitlab content subsumed by the Class-A index DELETE
        assert plan.orphan_marker_commits == {("github", "acme", "widgets"): {"aaa"}}
