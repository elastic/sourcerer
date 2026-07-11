"""Unit tests for the pure orphan-detection helpers in sourcerer.planner. No Elasticsearch
here -- every case is expressed as plain index-name lists and (org, repo, commit) tuple sets."""

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
    def test_org_only(self):
        assert parse_index_name("sourcerer-v1-files~acme") == ParsedIndex(
            kind="files", org="acme", repo=None, commit=None, name="sourcerer-v1-files~acme"
        )

    def test_org_repo(self):
        assert parse_index_name("sourcerer-v1-lines~acme~widgets") == ParsedIndex(
            kind="lines", org="acme", repo="widgets", commit=None, name="sourcerer-v1-lines~acme~widgets"
        )

    def test_org_repo_commit(self):
        parsed = parse_index_name("sourcerer-v1-files~acme~widgets~deadbeef")
        assert parsed == ParsedIndex(
            kind="files", org="acme", repo="widgets", commit="deadbeef",
            name="sourcerer-v1-files~acme~widgets~deadbeef",
        )

    def test_refs_index_is_not_a_files_or_lines_index(self):
        assert parse_index_name("sourcerer-v1-refs") is None

    def test_unrelated_index_returns_none(self):
        assert parse_index_name("kibana_sample_data_ecommerce") is None

    def test_too_many_segments_returns_none(self):
        assert parse_index_name("sourcerer-v1-files~a~b~c~d") is None

    def test_empty_segment_returns_none(self):
        assert parse_index_name("sourcerer-v1-files~acme~~deadbeef") is None

    def test_custom_prefixes(self):
        parsed = parse_index_name("myprefix-files~acme~widgets", files_prefix="myprefix-files")
        assert parsed.org == "acme" and parsed.repo == "widgets"


class TestOrphanIndices:
    def test_org_repo_index_with_no_ref_repo_is_orphaned(self):
        names = ["sourcerer-v1-files~acme~widgets"]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v1-files~acme~widgets"]

    def test_org_repo_index_with_ref_repo_is_not_orphaned(self):
        names = ["sourcerer-v1-files~acme~widgets", "sourcerer-v1-lines~acme~widgets"]
        result = orphan_indices(
            names, ref_orgs={"acme"}, ref_repos={("acme", "widgets")}, ref_commits=set()
        )
        assert result == []

    def test_org_level_orphan_subsumes_repo_and_commit_level_of_the_same_kind(self):
        # A future org-granularity index, plus its (redundant) same-kind repo/commit siblings --
        # all for an org entirely absent from refs. Only the org-level DELETE of that kind
        # should be planned; a same-kind repo/commit sibling is redundant with it.
        names = [
            "sourcerer-v1-files~acme",
            "sourcerer-v1-files~acme~widgets",
            "sourcerer-v1-files~acme~widgets~deadbeef",
        ]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v1-files~acme"]

    def test_repo_level_orphan_subsumes_commit_level(self):
        names = [
            "sourcerer-v1-files~acme~widgets",
            "sourcerer-v1-files~acme~widgets~deadbeef",
        ]
        # org exists in refs (so the org-level pass doesn't fire), but this repo doesn't.
        result = orphan_indices(names, ref_orgs={"acme"}, ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v1-files~acme~widgets"]

    def test_commit_level_orphan_only_when_repo_and_org_present(self):
        names = ["sourcerer-v1-files~acme~widgets~deadbeef"]
        result = orphan_indices(
            names, ref_orgs={"acme"}, ref_repos={("acme", "widgets")}, ref_commits=set()
        )
        assert result == ["sourcerer-v1-files~acme~widgets~deadbeef"]

    def test_commit_level_not_orphaned_when_ref_commit_present(self):
        names = ["sourcerer-v1-files~acme~widgets~deadbeef"]
        result = orphan_indices(
            names,
            ref_orgs={"acme"},
            ref_repos={("acme", "widgets")},
            ref_commits={("acme", "widgets", "deadbeef")},
        )
        assert result == []

    def test_descending_coarseness_order(self):
        # org orphan, repo orphan, and commit orphan all present at once -> org first, then
        # repo, then commit (the order deletion must proceed in).
        names = [
            "sourcerer-v1-files~acme~widgets~deadbeef",  # commit-level orphan (repo present)
            "sourcerer-v1-files~globex~gadgets",          # repo-level orphan (org present)
            "sourcerer-v1-files~initech",                 # org-level orphan
        ]
        result = orphan_indices(
            names,
            ref_orgs={"acme", "globex"},
            ref_repos={("acme", "widgets")},
            ref_commits=set(),
        )
        assert result == [
            "sourcerer-v1-files~initech",
            "sourcerer-v1-files~globex~gadgets",
            "sourcerer-v1-files~acme~widgets~deadbeef",
        ]

    def test_subsumption_does_not_cross_files_and_lines(self):
        # Real-world case found via live testing: a repo mid-migration has already collapsed
        # its `files` index to org~repo granularity while `lines` still has legacy
        # org~repo~commit indices. Neither org nor repo has any ref entry, so ALL of these are
        # orphans -- but the files~org~repo orphan must NOT suppress the lines~org~repo~commit
        # ones, since deleting the files index leaves the lines indices untouched.
        names = [
            "sourcerer-v1-files~acme~widgets",
            "sourcerer-v1-lines~acme~widgets~aaa",
            "sourcerer-v1-lines~acme~widgets~bbb",
        ]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert set(result) == set(names)

    def test_unparseable_names_are_ignored(self):
        names = ["sourcerer-v1-refs", "some-other-index"]
        assert orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set()) == []


class TestOrphanContentCommits:
    def test_commit_with_no_marker_is_orphaned(self):
        content = {("acme", "widgets"): {"aaa", "bbb"}}
        refs = {("acme", "widgets"): {"aaa"}}
        result = orphan_content_commits(content, refs, skip_repos=set())
        assert result == {("acme", "widgets"): {"bbb"}}

    def test_all_commits_referenced_is_not_orphaned(self):
        content = {("acme", "widgets"): {"aaa"}}
        refs = {("acme", "widgets"): {"aaa"}}
        assert orphan_content_commits(content, refs, skip_repos=set()) == {}

    def test_repo_with_no_ref_entry_at_all(self):
        content = {("acme", "widgets"): {"aaa"}}
        assert orphan_content_commits(content, ref_commits_by_repo={}, skip_repos=set()) == {
            ("acme", "widgets"): {"aaa"}
        }

    def test_skip_repos_excludes_class_a_repos(self):
        content = {("acme", "widgets"): {"aaa"}}
        result = orphan_content_commits(content, ref_commits_by_repo={}, skip_repos={("acme", "widgets")})
        assert result == {}


class TestOrphanMarkers:
    def test_commit_with_no_content_is_orphaned(self):
        refs = {("acme", "widgets"): {"aaa", "bbb"}}
        content = {("acme", "widgets"): {"aaa"}}
        result = orphan_markers(refs, content, skip_repos=set())
        assert result == {("acme", "widgets"): {"bbb"}}

    def test_whole_repo_content_missing_orphans_every_commit(self):
        refs = {("acme", "widgets"): {"aaa", "bbb"}}
        result = orphan_markers(refs, content_commits_by_repo={}, skip_repos=set())
        assert result == {("acme", "widgets"): {"aaa", "bbb"}}

    def test_skip_repos_excludes_class_a_repos(self):
        refs = {("acme", "widgets"): {"aaa"}}
        result = orphan_markers(refs, content_commits_by_repo={}, skip_repos={("acme", "widgets")})
        assert result == {}


class TestPlanOrphans:
    def test_no_orphans(self):
        names = ["sourcerer-v1-files~acme~widgets", "sourcerer-v1-lines~acme~widgets"]
        ref_tuples = {("acme", "widgets", "aaa")}
        content_tuples = {("acme", "widgets", "aaa")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == []
        assert plan.orphan_content == {}
        assert plan.orphan_marker_commits == {}

    def test_class_a_subsumes_class_b_for_the_same_repo(self):
        # The repo's whole index is a Class-A orphan (no ref entry at all, hence no ref tuple
        # for it either -- so there's nothing for Class-C to fire on here by construction).
        # Its stray content must NOT also surface as a separate Class-B delete_by_query --
        # the index DELETE removes it.
        names = ["sourcerer-v1-files~acme~widgets"]
        ref_tuples: set[tuple[str, str, str]] = set()
        content_tuples = {("acme", "widgets", "bbb")}  # would be Class-B if not subsumed
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == ["sourcerer-v1-files~acme~widgets"]
        assert plan.orphan_content == {}
        assert plan.orphan_marker_commits == {}

    def test_class_b_and_c_fire_independently_when_index_present(self):
        names = ["sourcerer-v1-files~acme~widgets"]
        ref_tuples = {("acme", "widgets", "aaa"), ("acme", "widgets", "bbb")}
        content_tuples = {("acme", "widgets", "aaa"), ("acme", "widgets", "ccc")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == []
        assert plan.orphan_content == {("acme", "widgets"): {"ccc"}}  # content, no marker
        assert plan.orphan_marker_commits == {("acme", "widgets"): {"bbb"}}  # marker, no content
