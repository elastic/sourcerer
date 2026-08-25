"""Unit tests for the pure orphan-detection helpers in sourcerer.planner. No Elasticsearch
here -- every case is expressed as plain index-name lists and (host, org, repo, commit) tuple
sets. v3 index names carry a leading host segment; repo tuples are keyed (host, org, repo)."""

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
        assert parse_index_name("sourcerer-v3-lines~github~acme~widgets") == ParsedIndex(
            kind="lines", host="github", org="acme", repo="widgets", commit=None,
            name="sourcerer-v3-lines~github~acme~widgets",
        )

    def test_host_org_repo_commit(self):
        parsed = parse_index_name("sourcerer-v3-files~github~acme~widgets~deadbeef")
        assert parsed == ParsedIndex(
            kind="files", host="github", org="acme", repo="widgets", commit="deadbeef",
            name="sourcerer-v3-files~github~acme~widgets~deadbeef",
        )

    def test_refs_index_is_not_a_files_or_lines_index(self):
        assert parse_index_name("sourcerer-v3-refs") is None

    def test_unrelated_index_returns_none(self):
        assert parse_index_name("kibana_sample_data_ecommerce") is None

    def test_too_many_segments_returns_none(self):
        assert parse_index_name("sourcerer-v3-files~a~b~c~d~e") is None

    def test_empty_segment_returns_none(self):
        assert parse_index_name("sourcerer-v3-files~github~acme~~deadbeef") is None

    def test_custom_prefixes(self):
        parsed = parse_index_name("myprefix-files~github~acme~widgets", files_prefix="myprefix-files")
        assert parsed.host == "github" and parsed.org == "acme" and parsed.repo == "widgets"


class TestOrphanIndices:
    def test_host_org_repo_index_with_no_ref_repo_is_orphaned(self):
        names = ["sourcerer-v3-files~github~acme~widgets"]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v3-files~github~acme~widgets"]

    def test_index_with_ref_repo_is_not_orphaned(self):
        names = ["sourcerer-v3-files~github~acme~widgets", "sourcerer-v3-lines~github~acme~widgets"]
        result = orphan_indices(
            names, ref_orgs={("github", "acme")}, ref_repos={("github", "acme", "widgets")}, ref_commits=set()
        )
        assert result == []

    def test_same_repo_distinct_hosts_judged_independently(self):
        # github/acme/widgets is in refs; gitlab/acme/widgets is not -> only the gitlab index
        # is an orphan.
        names = [
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-files~gitlab~acme~widgets",
        ]
        result = orphan_indices(
            names, ref_orgs={("github", "acme")}, ref_repos={("github", "acme", "widgets")}, ref_commits=set()
        )
        assert result == ["sourcerer-v3-files~gitlab~acme~widgets"]

    def test_org_level_orphan_subsumes_repo_and_commit_level_of_the_same_kind(self):
        names = [
            "sourcerer-v3-files~github~acme",
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-files~github~acme~widgets~deadbeef",
        ]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v3-files~github~acme"]

    def test_repo_level_orphan_subsumes_commit_level(self):
        names = [
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-files~github~acme~widgets~deadbeef",
        ]
        result = orphan_indices(names, ref_orgs={("github", "acme")}, ref_repos=set(), ref_commits=set())
        assert result == ["sourcerer-v3-files~github~acme~widgets"]

    def test_commit_level_not_orphaned_when_ref_commit_present(self):
        names = ["sourcerer-v3-files~github~acme~widgets~deadbeef"]
        result = orphan_indices(
            names,
            ref_orgs={("github", "acme")},
            ref_repos={("github", "acme", "widgets")},
            ref_commits={("github", "acme", "widgets", "deadbeef")},
        )
        assert result == []

    def test_subsumption_does_not_cross_files_and_lines(self):
        names = [
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-lines~github~acme~widgets~aaa",
            "sourcerer-v3-lines~github~acme~widgets~bbb",
        ]
        result = orphan_indices(names, ref_orgs=set(), ref_repos=set(), ref_commits=set())
        assert set(result) == set(names)

    def test_unparseable_names_are_ignored(self):
        names = ["sourcerer-v3-refs", "some-other-index"]
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
        names = ["sourcerer-v3-files~github~acme~widgets", "sourcerer-v3-lines~github~acme~widgets"]
        ref_tuples = {("github", "acme", "widgets", "aaa")}
        content_tuples = {("github", "acme", "widgets", "aaa")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == []
        assert plan.orphan_content == {}
        assert plan.orphan_marker_commits == {}

    def test_class_a_subsumes_class_b_for_the_same_repo(self):
        names = ["sourcerer-v3-files~github~acme~widgets"]
        ref_tuples: set[tuple[str, str, str, str]] = set()
        content_tuples = {("github", "acme", "widgets", "bbb")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == ["sourcerer-v3-files~github~acme~widgets"]
        assert plan.orphan_content == {}
        assert plan.orphan_marker_commits == {}

    def test_class_b_and_c_fire_independently_when_index_present(self):
        names = ["sourcerer-v3-files~github~acme~widgets"]
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
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-files~gitlab~acme~widgets",
        ]
        ref_tuples = {("github", "acme", "widgets", "aaa")}
        content_tuples = {("gitlab", "acme", "widgets", "bbb")}
        plan = plan_orphans(names, ref_tuples, content_tuples)
        assert plan.orphan_index_names == ["sourcerer-v3-files~gitlab~acme~widgets"]
        assert plan.orphan_content == {}  # gitlab content subsumed by the Class-A index DELETE
        assert plan.orphan_marker_commits == {("github", "acme", "widgets"): {"aaa"}}


class TestOrphanStaleContent:
    """Class D: content in an index none of its commit's markers point at (the index.level/suffix
    migration backstop). intended_index_by_commit is the reconstructed marker-intended locations."""

    def test_content_at_unintended_index_is_stale(self):
        from sourcerer.planner import orphan_stale_content
        ct = ("github", "acme", "widgets", "abc")
        content_by_index = {
            "sourcerer-v3-files~github~acme~widgets": {ct},          # old copy, left by a migration
            "sourcerer-v3-files~github~acme~widgets^deploy": {ct},   # new (intended) copy
        }
        intended = {ct: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        stale = orphan_stale_content(content_by_index, intended, skip_indices=set())
        assert stale == {"sourcerer-v3-files~github~acme~widgets": {"abc"}}

    def test_intended_index_not_flagged(self):
        from sourcerer.planner import orphan_stale_content
        ct = ("github", "acme", "widgets", "abc")
        content_by_index = {"sourcerer-v3-files~github~acme~widgets^deploy": {ct}}
        intended = {ct: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        assert orphan_stale_content(content_by_index, intended, set()) == {}

    def test_commit_without_marker_is_not_class_d(self):
        """No marker at all -> Class B territory (orphan_content), not stale-location."""
        from sourcerer.planner import orphan_stale_content
        ct = ("github", "acme", "widgets", "abc")
        content_by_index = {"sourcerer-v3-files~github~acme~widgets": {ct}}
        assert orphan_stale_content(content_by_index, {}, set()) == {}

    def test_skip_indices_excluded(self):
        from sourcerer.planner import orphan_stale_content
        ct = ("github", "acme", "widgets", "abc")
        content_by_index = {"sourcerer-v3-files~github~acme~widgets": {ct}}
        intended = {ct: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        # index is already going away via a Class-A whole-index DELETE
        assert orphan_stale_content(content_by_index, intended,
                                    {"sourcerer-v3-files~github~acme~widgets"}) == {}

    def test_plan_orphans_wires_class_d(self):
        ct = ("github", "acme", "widgets", "abc")
        names = [
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-files~github~acme~widgets^deploy",
        ]
        ref_tuples = {ct}
        content_tuples = {ct}
        content_by_index = {
            "sourcerer-v3-files~github~acme~widgets": {ct},
            "sourcerer-v3-files~github~acme~widgets^deploy": {ct},
        }
        intended = {ct: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        plan = plan_orphans(names, ref_tuples, content_tuples,
                            content_by_index_commit=content_by_index,
                            intended_index_by_commit=intended)
        assert plan.orphan_stale == {"sourcerer-v3-files~github~acme~widgets": {"abc"}}


class TestOrphanStaleIncrementalContent:
    """Class D-I: incremental (ref-addressed, commit-less) content in an index that the branch's
    join doc no longer intends.  Mirrors Class D but keyed on (host, org, repo, ref) tuples."""

    def test_incremental_content_at_unintended_index_is_stale(self):
        from sourcerer.planner import orphan_stale_incremental_content
        rt = ("github", "acme", "widgets", "main")
        content_by_index = {
            "sourcerer-v3-files~github~acme~widgets": {rt},         # old copy from a suffix migration
            "sourcerer-v3-files~github~acme~widgets^deploy": {rt},  # new (intended) copy
        }
        intended = {rt: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        stale = orphan_stale_incremental_content(content_by_index, intended, skip_indices=set())
        assert stale == {"sourcerer-v3-files~github~acme~widgets": {rt}}

    def test_intended_index_not_flagged(self):
        from sourcerer.planner import orphan_stale_incremental_content
        rt = ("github", "acme", "widgets", "main")
        content_by_index = {"sourcerer-v3-files~github~acme~widgets^deploy": {rt}}
        intended = {rt: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        assert orphan_stale_incremental_content(content_by_index, intended, set()) == {}

    def test_ref_without_join_doc_is_not_class_di(self):
        """No join doc for this ref -> different sweep, not stale-location."""
        from sourcerer.planner import orphan_stale_incremental_content
        rt = ("github", "acme", "widgets", "main")
        content_by_index = {"sourcerer-v3-files~github~acme~widgets": {rt}}
        assert orphan_stale_incremental_content(content_by_index, {}, set()) == {}

    def test_skip_indices_excluded(self):
        from sourcerer.planner import orphan_stale_incremental_content
        rt = ("github", "acme", "widgets", "main")
        content_by_index = {"sourcerer-v3-files~github~acme~widgets": {rt}}
        intended = {rt: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        # Index is already going away via a Class-A whole-index DELETE.
        assert orphan_stale_incremental_content(
            content_by_index, intended, {"sourcerer-v3-files~github~acme~widgets"}
        ) == {}

    def test_plan_orphans_wires_class_di(self):
        """plan_orphans exposes orphan_stale_incremental when location data is supplied."""
        rt = ("github", "acme", "widgets", "main")
        ct = ("github", "acme", "widgets", "abc")
        names = [
            "sourcerer-v3-files~github~acme~widgets",
            "sourcerer-v3-files~github~acme~widgets^deploy",
        ]
        # One snapshot commit ref present so Class A/B/C don't fire on the identity.
        ref_tuples = {ct}
        content_tuples = {ct}
        incremental_content_by_index = {
            "sourcerer-v3-files~github~acme~widgets": {rt},         # stale copy
            "sourcerer-v3-files~github~acme~widgets^deploy": {rt},  # intended
        }
        intended_incremental_by_ref = {rt: {"sourcerer-v3-files~github~acme~widgets^deploy"}}
        plan = plan_orphans(
            names, ref_tuples, content_tuples,
            incremental_content_by_index=incremental_content_by_index,
            intended_incremental_index_by_ref=intended_incremental_by_ref,
        )
        assert plan.orphan_stale_incremental == {
            "sourcerer-v3-files~github~acme~widgets": {rt},
        }

    def test_plan_orphans_class_di_empty_when_not_supplied(self):
        """Back-compat: callers that omit incremental location data get an empty Class D-I."""
        ct = ("github", "acme", "widgets", "abc")
        plan = plan_orphans(
            ["sourcerer-v3-files~github~acme~widgets"],
            {ct}, {ct},
        )
        assert plan.orphan_stale_incremental == {}


class TestEmptyIndexSweep:
    """Class E: an empty content index is deleted even when its git identity still has markers
    (the suffix a->b migration case), and is de-duped against Class-A orphans."""

    def test_empty_index_included_even_when_identity_has_markers(self):
        ct = ("github", "acme", "widgets", "abc")
        names = [
            "sourcerer-v3-files~github~acme~widgets^a",  # drained by a suffix a->b migration
            "sourcerer-v3-files~github~acme~widgets^b",  # now holds the content
        ]
        # Identity (github, acme, widgets) still has markers (they point at ^b), so ^a is NOT a
        # Class-A orphan -- but it's empty, so Class E must catch it.
        plan = plan_orphans(
            names, ref_commit_tuples={ct}, content_commit_tuples={ct},
            empty_index_names=["sourcerer-v3-files~github~acme~widgets^a"],
        )
        assert "sourcerer-v3-files~github~acme~widgets^a" in plan.empty_index_names
        # ^a is not a Class-A orphan (identity is backed by refs).
        assert "sourcerer-v3-files~github~acme~widgets^a" not in plan.orphan_index_names

    def test_empty_index_deduped_against_class_a(self):
        # An index that is BOTH empty AND identity-orphaned appears only under Class A (one DELETE).
        names = ["sourcerer-v3-files~github~ghostorg~gone"]
        plan = plan_orphans(
            names, ref_commit_tuples=set(), content_commit_tuples=set(),
            empty_index_names=["sourcerer-v3-files~github~ghostorg~gone"],
        )
        assert "sourcerer-v3-files~github~ghostorg~gone" in plan.orphan_index_names
        assert plan.empty_index_names == []
