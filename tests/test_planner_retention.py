"""Unit tests for the retention half of sourcerer.planner (plan_repo and friends). The
orphan-detection half is covered by test_planner_orphans.py; this file exercises keep/delete/
unmanaged decisions, the union-across-selectors / intersection-within-selector semantics, and
content_delete_set. All pure -- no Elasticsearch."""

# App packages
from sourcerer.config import parse_config
from sourcerer.planner import Decision, _describe, content_delete_set, plan_repo

from conftest import dt, make_marker


def _cfg(refs, org="acme", repo="widgets"):
    return parse_config([{"org": org, "repo": repo, "refs": refs}])[0]


def _sel(type_="branch", match="main", retain=None):
    d = {"type": type_, "match": match}
    if retain is not None:
        d["retain"] = retain
    return d


class TestUnmanagedAndKeepForever:
    def test_marker_matching_no_selector_is_unmanaged(self):
        cfg = _cfg([_sel(match="main")])
        m = make_marker(ref="other-branch")
        [decision] = plan_repo([m], cfg)
        assert decision.action == "unmanaged"

    def test_no_retain_keeps_forever(self):
        cfg = _cfg([_sel(match="main")])
        m = make_marker(ref="main")
        [decision] = plan_repo([m], cfg)
        assert decision.action == "keep"


class TestDeleteByEachCriterion:
    def test_count(self):
        cfg = _cfg([_sel(match="main", retain={"count": 1})])
        old = make_marker("old", ref="main", commit="aaa", commit_date=dt(2020, 1, 1))
        new = make_marker("new", ref="main", commit="bbb", commit_date=dt(2024, 1, 1))
        decisions = {d.marker.id: d for d in plan_repo([old, new], cfg)}
        assert decisions["old"].action == "delete"
        assert decisions["old"].criteria == ("count",)
        assert decisions["new"].action == "keep"

    def test_age(self):
        now = dt(2024, 6, 1)
        cfg = _cfg([_sel(match="main", retain={"age": "30d"})])
        old = make_marker("old", ref="main", commit_date=dt(2020, 1, 1))
        recent = make_marker("recent", ref="main", commit_date=dt(2024, 5, 20))
        decisions = {d.marker.id: d for d in plan_repo([old, recent], cfg, now=now)}
        assert decisions["old"].action == "delete"
        assert decisions["old"].criteria == ("age",)
        assert decisions["recent"].action == "keep"

    def test_version(self):
        cfg = _cfg([_sel(
            type_="tag", match="v{major}.{minor}.{patch}", retain={"version": {"majors": 1}},
        )])
        v8 = make_marker("v8", ref="v8.0.0", ref_type="tag")
        v9 = make_marker("v9", ref="v9.0.0", ref_type="tag")
        decisions = {d.marker.id: d for d in plan_repo([v8, v9], cfg)}
        assert decisions["v8"].action == "delete"
        assert decisions["v8"].criteria == ("version",)
        assert decisions["v9"].action == "keep"

    def test_prerelease_superseded(self):
        cfg = _cfg([_sel(
            type_="tag",
            match=["v{major}.{minor}.{patch}", "v{major}.{minor}.{patch}-{prerelease}"],
            retain={"prerelease": "superseded"},
        )])
        rc = make_marker("rc", ref="v9.0.0-rc1", ref_type="tag")
        final = make_marker("final", ref="v9.0.0", ref_type="tag")
        decisions = {d.marker.id: d for d in plan_repo([rc, final], cfg)}
        assert decisions["rc"].action == "delete"
        assert decisions["rc"].criteria == ("prerelease",)
        assert decisions["final"].action == "keep"


class TestUnionAcrossSelectors:
    def test_keep_forever_selector_acts_as_allowlist(self):
        # v9.0.0 matches both a bare allowlist selector (no retain) and the trimming
        # versioned selector; union means ANY keeping selector wins.
        cfg = _cfg([
            _sel(type_="tag", match="v9.0.0"),  # no retain -> keep forever
            _sel(type_="tag", match="v{major}.{minor}.{patch}", retain={"version": {"majors": 1}}),
        ])
        v8 = make_marker("v8", ref="v8.0.0", ref_type="tag")
        v9 = make_marker("v9", ref="v9.0.0", ref_type="tag")
        decisions = {d.marker.id: d for d in plan_repo([v8, v9], cfg)}
        assert decisions["v9"].action == "keep"
        assert decisions["v8"].action == "delete"  # only matches the trimming selector


class TestIntersectionWithinSelector:
    def test_marker_survives_only_if_every_criterion_keeps_it(self):
        cfg = _cfg([_sel(
            type_="tag", match="v{major}.{minor}.{patch}",
            retain={"version": {"majors": 2}, "count": 1},
        )])
        # All three pass the version check (majors:2 keeps majors >= 8), but only the
        # newest-dated one passes the pooled tag `count:1` check.
        v8_old = make_marker("v8old", ref="v8.0.0", ref_type="tag", commit_date=dt(2022, 1, 1))
        v8_new = make_marker("v8new", ref="v8.1.0", ref_type="tag", commit_date=dt(2023, 1, 1))
        v9 = make_marker("v9", ref="v9.0.0", ref_type="tag", commit_date=dt(2024, 1, 1))
        decisions = {d.marker.id: d for d in plan_repo([v8_old, v8_new, v9], cfg)}
        assert decisions["v8old"].action == "delete"
        assert decisions["v8old"].criteria == ("count",)  # excluded by count, not version
        assert decisions["v8new"].action == "delete"
        assert decisions["v8new"].criteria == ("count",)
        assert decisions["v9"].action == "keep"


class TestBranchVsTagCountGrouping:
    def test_count_is_per_branch_name(self):
        cfg = _cfg([_sel(match="*", retain={"count": 1})])
        b1_old = make_marker("b1old", ref="release-8", commit_date=dt(2022, 1, 1))
        b1_new = make_marker("b1new", ref="release-8", commit_date=dt(2023, 1, 1))
        b2_old = make_marker("b2old", ref="release-9", commit_date=dt(2020, 1, 1))
        b2_new = make_marker("b2new", ref="release-9", commit_date=dt(2024, 1, 1))
        decisions = {d.marker.id: d for d in plan_repo([b1_old, b1_new, b2_old, b2_new], cfg)}
        # Each branch keeps its own newest, independent of the other branch's dates.
        assert decisions["b1new"].action == "keep"
        assert decisions["b1old"].action == "delete"
        assert decisions["b2new"].action == "keep"
        assert decisions["b2old"].action == "delete"

    def test_count_is_pooled_across_the_tag_family(self):
        cfg = _cfg([_sel(type_="tag", match="v{major}.{minor}.{patch}", retain={"count": 1})])
        v8 = make_marker("v8", ref="v8.0.0", ref_type="tag", commit_date=dt(2022, 1, 1))
        v9 = make_marker("v9", ref="v9.0.0", ref_type="tag", commit_date=dt(2024, 1, 1))
        decisions = {d.marker.id: d for d in plan_repo([v8, v9], cfg)}
        assert decisions["v9"].action == "keep"
        assert decisions["v8"].action == "delete"


class TestCommitSelectorRetention:
    OLD_SHA = "cfefb3b2378ccbadefa7c8f4f9e21b3a1d2e5f60"
    NEW_SHA = "deadbeefcafebabe1234567890abcdef12345678"

    def test_age_retention(self):
        now = dt(2024, 6, 1)
        cfg = _cfg([_sel(type_="commit", match=["cfefb3b", "deadbee"], retain={"age": "30d"})])
        old = make_marker("old", ref=self.OLD_SHA, ref_type="commit",
                           commit=self.OLD_SHA, commit_date=dt(2020, 1, 1))
        recent = make_marker("recent", ref=self.NEW_SHA, ref_type="commit",
                              commit=self.NEW_SHA, commit_date=dt(2024, 5, 20))
        decisions = {d.marker.id: d for d in plan_repo([old, recent], cfg, now=now)}
        assert decisions["old"].action == "delete"
        assert decisions["old"].criteria == ("age",)
        assert decisions["recent"].action == "keep"

    def test_no_retain_keeps_forever(self):
        cfg = _cfg([_sel(type_="commit", match="cfefb3b")])
        m = make_marker(ref=self.OLD_SHA, ref_type="commit",
                         commit=self.OLD_SHA, commit_date=dt(2020, 1, 1))
        [decision] = plan_repo([m], cfg)
        assert decision.action == "keep"

    def test_full_sha_managed_by_short_prefix_selector(self):
        cfg = _cfg([_sel(type_="commit", match="cfefb3b", retain={"age": "30d"})])
        m = make_marker(ref=self.OLD_SHA, ref_type="commit",
                         commit=self.OLD_SHA, commit_date=dt(2020, 1, 1))
        [decision] = plan_repo([m], cfg, now=dt(2024, 6, 1))
        assert decision.action == "delete"
        assert decision.criteria == ("age",)

    def test_non_matching_sha_is_unmanaged(self):
        cfg = _cfg([_sel(type_="commit", match="cfefb3b")])
        other = make_marker(ref=self.NEW_SHA, ref_type="commit", commit=self.NEW_SHA)
        [decision] = plan_repo([other], cfg)
        assert decision.action == "unmanaged"


class TestDateIndependentOnly:
    def test_skips_count_and_age_but_not_version(self):
        cfg = _cfg([_sel(
            type_="tag", match="v{major}.{minor}.{patch}",
            retain={"version": {"majors": 1}, "count": 1},
        )])
        v9_a = make_marker("v9a", ref="v9.0.0", ref_type="tag", commit_date=dt(2022, 1, 1))
        v9_b = make_marker("v9b", ref="v9.1.0", ref_type="tag", commit_date=dt(2024, 1, 1))
        v8 = make_marker("v8", ref="v8.0.0", ref_type="tag", commit_date=dt(2020, 1, 1))
        decisions = {
            d.marker.id: d
            for d in plan_repo([v9_a, v9_b, v8], cfg, date_independent_only=True)
        }
        # count would normally prune one of v9_a/v9_b, but date_independent_only skips it.
        assert decisions["v9a"].action == "keep"
        assert decisions["v9b"].action == "keep"
        # version still applies: major 8 is below the majors:1 threshold (latest major 9).
        assert decisions["v8"].action == "delete"
        assert decisions["v8"].criteria == ("version",)


class TestContentDeleteSet:
    def test_commit_shared_by_a_surviving_marker_is_not_dropped(self):
        deleted = Decision(make_marker("old", commit="shared"), "delete", "count:1")
        kept = Decision(make_marker("new", commit="shared", ref="other-tag"), "keep", "keep forever")
        assert content_delete_set([deleted, kept]) == set()

    def test_commit_referenced_by_no_surviving_marker_is_dropped(self):
        deleted = Decision(make_marker("old", commit="gone"), "delete", "count:1")
        assert content_delete_set([deleted]) == {"gone"}

    def test_unmanaged_markers_are_not_deleted(self):
        unmanaged = Decision(make_marker("x", commit="c1"), "unmanaged", "matches no selector")
        assert content_delete_set([unmanaged]) == set()


class TestDescribe:
    def test_keep_forever_description(self):
        cfg = _cfg([_sel(match="main")])
        assert "keep forever" in _describe(cfg.selectors[0])

    def test_retain_bits_are_rendered(self):
        cfg = _cfg([_sel(
            type_="tag", match="v{major}.{minor}.{patch}",
            retain={"version": {"majors": 2}, "count": 5, "prerelease": "superseded"},
        )])
        desc = _describe(cfg.selectors[0])
        assert "version(majors:2)" in desc
        assert "count:5" in desc
        assert "drop-superseded-pre" in desc
