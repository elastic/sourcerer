"""Unit tests for the version-aware pattern DSL and retention primitives in
sourcerer.version. Everything here is pure (no ES, no git, no filesystem)."""

# Standard packages
from datetime import timedelta

# Third-party packages
import pytest

# App packages
from sourcerer.version import (
    Version,
    age_keep,
    compile_pattern,
    drop_superseded_prereleases,
    match_version,
    parse_bound,
    recent_keep,
    version_keep,
)

from conftest import dt, make_version


class TestCompilePattern:
    def test_numeric_tokens_become_capture_groups(self):
        cp = compile_pattern("v{major}.{minor}.{patch}")
        assert cp.levels == ("major", "minor", "patch")
        assert not cp.has_prerelease
        assert cp.is_versioned()

    def test_prerelease_token(self):
        cp = compile_pattern("v{major}.{minor}.{patch}-{prerelease}")
        assert cp.has_prerelease
        v = match_version(cp, "v9.0.0-rc1")
        assert v.prerelease == "rc1"

    def test_whitespace_inside_token(self):
        cp = compile_pattern("v{ major }.{ minor }")
        assert cp.levels == ("major", "minor")
        assert match_version(cp, "v1.2").components == (1, 2)

    def test_duplicate_numeric_token_raises(self):
        with pytest.raises(ValueError, match="duplicate"):
            compile_pattern("v{major}.{major}")

    def test_duplicate_prerelease_token_raises(self):
        with pytest.raises(ValueError, match="duplicate"):
            compile_pattern("v{major}-{prerelease}-{prerelease}")

    def test_non_prefix_level_ordering_raises(self):
        # {minor} without a preceding {major} is not a prefix of LEVELS.
        with pytest.raises(ValueError, match="must be a prefix"):
            compile_pattern("v{minor}")

    def test_skipped_level_raises(self):
        # {major} then {build}, skipping minor/patch, is not a prefix either.
        with pytest.raises(ValueError, match="must be a prefix"):
            compile_pattern("v{major}.{build}")

    def test_non_versioned_pattern_has_no_levels(self):
        cp = compile_pattern("my-dev-tag")
        assert cp.levels == ()
        assert not cp.is_versioned()


class TestGlobFragment:
    """Exercised indirectly through compile_pattern + match_version, since
    _glob_fragment only produces a regex fragment."""

    def test_star_matches_any_chars(self):
        cp = compile_pattern("release-*")
        assert match_version(cp, "release-2024-01") is not None
        assert match_version(cp, "release-") is not None
        assert match_version(cp, "other") is None

    def test_question_matches_one_char(self):
        cp = compile_pattern("v?.0")
        assert match_version(cp, "v8.0") is not None
        assert match_version(cp, "v88.0") is None

    def test_character_class(self):
        cp = compile_pattern("v[89].*")
        assert match_version(cp, "v8.14") is not None
        assert match_version(cp, "v9.0") is not None
        assert match_version(cp, "v7.0") is None

    def test_negated_character_class_bang(self):
        cp = compile_pattern("v[!89].0")
        assert match_version(cp, "v7.0") is not None
        assert match_version(cp, "v8.0") is None

    def test_negated_character_class_caret(self):
        cp = compile_pattern("v[^89].0")
        assert match_version(cp, "v7.0") is not None
        assert match_version(cp, "v9.0") is None

    def test_unterminated_bracket_is_treated_literally(self):
        cp = compile_pattern("abc[def")
        assert match_version(cp, "abc[def") is not None
        assert match_version(cp, "abcXdef") is None


class TestMatchVersion:
    def test_no_match_returns_none(self):
        cp = compile_pattern("v{major}.{minor}.{patch}")
        assert match_version(cp, "v1.2") is None

    def test_component_extraction(self):
        cp = compile_pattern("v{major}.{minor}.{patch}")
        v = match_version(cp, "v10.4.0")
        assert v.components == (10, 4, 0)
        assert v.prerelease == ""
        assert not v.is_prerelease

    def test_final_release_has_empty_prerelease(self):
        cp = compile_pattern("v{major}.{minor}.{patch}-{prerelease}")
        # The pattern requires a prerelease suffix, so a bare final doesn't match it --
        # this asserts a pattern *without* the token yields "" prerelease.
        plain_cp = compile_pattern("v{major}.{minor}.{patch}")
        v = match_version(plain_cp, "v9.0.0")
        assert v.prerelease == ""

    def test_non_versioned_pattern_acts_as_pure_glob_filter(self):
        cp = compile_pattern("my-dev-tag")
        v = match_version(cp, "my-dev-tag")
        assert v is not None
        assert v.components == ()
        assert match_version(cp, "other-tag") is None


class TestParseBound:
    def test_pads_short_input(self):
        assert parse_bound("9", ("major", "minor", "patch")) == (9, 0, 0)

    def test_exact_length(self):
        assert parse_bound("9.10.1", ("major", "minor", "patch")) == (9, 10, 1)

    def test_truncates_long_input_to_levels_length(self):
        assert parse_bound("9.10.1.5", ("major", "minor")) == (9, 10)


class TestVersionKeep:
    def test_majors_value_relative_threshold(self):
        # Headline case from the docstring: majors {2, 9} indexed, majors:2 keeps only {9}
        # (threshold 9-(2-1)=8), NOT {9, 2} (which a count-based keep would give).
        v2 = make_version("v2.0.0", (2, 0, 0))
        v9 = make_version("v9.0.0", (9, 0, 0))
        kept = version_keep([v2, v9], {"major": 2}, ("major", "minor", "patch"))
        assert kept == {v9}

    def test_patches_newest_per_major_minor(self):
        old_patch = make_version("v8.17.0", (8, 17, 0))
        new_patch = make_version("v8.17.3", (8, 17, 3))
        other_minor = make_version("v8.18.0", (8, 18, 0))
        kept = version_keep(
            [old_patch, new_patch, other_minor], {"patch": 1}, ("major", "minor", "patch")
        )
        assert kept == {new_patch, other_minor}

    def test_level_absent_from_counts_imposes_no_constraint(self):
        versions = [make_version("v1.0.0", (1, 0, 0)), make_version("v2.0.0", (2, 0, 0))]
        assert version_keep(versions, {}, ("major", "minor", "patch")) == set(versions)

    def test_nested_grouping_across_multiple_levels(self):
        va = make_version("v8.17.0", (8, 17, 0))
        vb = make_version("v8.17.3", (8, 17, 3))
        vc = make_version("v8.18.0", (8, 18, 0))
        vd = make_version("v9.0.0", (9, 0, 0))
        # minors:1 first narrows each major to its newest minor (va/vb's minor-17 series is
        # dropped in favor of vc's minor-18, even though it has a newer patch); patches:1 then
        # keeps the newest patch of what's left.
        kept = version_keep(
            [va, vb, vc, vd], {"minor": 1, "patch": 1}, ("major", "minor", "patch", "build")
        )
        assert kept == {vc, vd}


class TestDropSupersededPrereleases:
    def test_prerelease_with_matching_final_is_dropped(self):
        rc = make_version("v9.0.0-rc1", (9, 0, 0), prerelease="rc1")
        final = make_version("v9.0.0", (9, 0, 0), prerelease="")
        kept = drop_superseded_prereleases([rc, final])
        assert kept == {final}

    def test_prerelease_without_matching_final_survives(self):
        rc = make_version("v9.1.0-rc1", (9, 1, 0), prerelease="rc1")
        final = make_version("v9.0.0", (9, 0, 0), prerelease="")
        kept = drop_superseded_prereleases([rc, final])
        assert kept == {rc, final}


class TestRecentKeep:
    def test_keeps_newest_n_by_timestamp(self):
        refs = [("a", dt(2024, 1, 1)), ("b", dt(2024, 6, 1)), ("c", dt(2023, 1, 1))]
        assert recent_keep(refs, 2) == {"a", "b"}

    def test_n_larger_than_available_keeps_all(self):
        refs = [("a", dt(2024, 1, 1))]
        assert recent_keep(refs, 5) == {"a"}


class TestAgeKeep:
    def test_keeps_within_max_age(self):
        now = dt(2024, 6, 1)
        refs = [("recent", dt(2024, 5, 1)), ("old", dt(2020, 1, 1))]
        assert age_keep(refs, timedelta(days=365), now) == {"recent"}

    def test_boundary_is_inclusive(self):
        now = dt(2024, 6, 1)
        exactly_at_boundary = now - timedelta(days=30)
        refs = [("edge", exactly_at_boundary)]
        assert age_keep(refs, timedelta(days=30), now) == {"edge"}
