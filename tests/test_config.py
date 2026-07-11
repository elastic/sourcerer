"""Unit tests for the repos.yml model + parser/validator in sourcerer.config. Only
load_config touches the filesystem (a single open()); everything tested here is parse_config
and its pure helpers over already-parsed data."""

# Standard packages
from datetime import timedelta

# Third-party packages
import pytest

# App packages
from sourcerer.config import Since, parse_config, parse_date, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize(
        "text,expected_seconds",
        [
            ("30s", 30),
            ("12h", 12 * 3600),
            ("7d", 7 * 86400),
            ("2w", 2 * 604800),
            ("3m", 3 * 2592000),  # month = 30d
            ("1y", 365 * 86400),  # year = 365d
        ],
    )
    def test_units(self, text, expected_seconds):
        assert parse_duration(text) == timedelta(seconds=expected_seconds)

    def test_allows_surrounding_whitespace(self):
        assert parse_duration(" 1y ") == timedelta(days=365)

    @pytest.mark.parametrize("text", ["1", "1x", "y1", "", "1.5d"])
    def test_malformed_raises(self, text):
        with pytest.raises(ValueError, match="invalid duration"):
            parse_duration(text)


class TestParseDate:
    def test_valid_date_forced_to_utc(self):
        d = parse_date("2025-01-01")
        assert d.tzinfo is not None
        assert d.year == 2025 and d.month == 1 and d.day == 1

    def test_malformed_raises(self):
        with pytest.raises(ValueError, match="invalid date"):
            parse_date("not-a-date")


def _entry(org="acme", repo="widgets", refs=None):
    entry = {"org": org, "repo": repo}
    if refs is not None:
        entry["refs"] = refs
    return entry


class TestParseConfigStructure:
    def test_non_list_raises(self):
        with pytest.raises(ValueError, match="must be a YAML list"):
            parse_config({"org": "acme"})

    def test_entry_not_a_mapping_raises(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_config(["not-a-dict"])

    def test_missing_org_raises(self):
        with pytest.raises(ValueError, match="org.*repo"):
            parse_config([{"repo": "widgets"}])

    def test_missing_repo_raises(self):
        with pytest.raises(ValueError, match="org.*repo"):
            parse_config([{"org": "acme"}])

    def test_refs_not_a_list_raises(self):
        with pytest.raises(ValueError, match="'refs' must be a list"):
            parse_config([_entry(refs={"type": "branch"})])

    def test_omitted_refs_yields_no_selectors(self):
        cfgs = parse_config([_entry()])
        assert cfgs[0].selectors == []

    def test_empty_refs_yields_no_selectors(self):
        cfgs = parse_config([_entry(refs=[])])
        assert cfgs[0].selectors == []

    def test_multiple_entries_parsed_in_order(self):
        cfgs = parse_config([_entry(org="a", repo="b"), _entry(org="c", repo="d")])
        assert [(c.org, c.repo) for c in cfgs] == [("a", "b"), ("c", "d")]


def _selector(type_="branch", match="main", since=None, retain=None):
    sel = {"type": type_, "match": match}
    if since is not None:
        sel["since"] = since
    if retain is not None:
        sel["retain"] = retain
    return sel


class TestParseSelector:
    def test_bad_type_raises(self):
        with pytest.raises(ValueError, match="'type' must be"):
            parse_config([_entry(refs=[_selector(type_="commit")])])

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unknown keys"):
            parse_config([_entry(refs=[{**_selector(), "bogus": 1}])])

    def test_match_as_string(self):
        cfgs = parse_config([_entry(refs=[_selector(match="main")])])
        assert cfgs[0].selectors[0].raw_patterns == ["main"]

    def test_match_as_list(self):
        cfgs = parse_config([_entry(refs=[_selector(match=["main", "dev"])])])
        assert cfgs[0].selectors[0].raw_patterns == ["main", "dev"]

    def test_match_empty_list_raises(self):
        with pytest.raises(ValueError, match="'match' must be"):
            parse_config([_entry(refs=[_selector(match=[])])])

    def test_match_missing_raises(self):
        with pytest.raises(ValueError, match="'match' must be"):
            parse_config([_entry(refs=[{"type": "branch"}])])

    def test_versioned_patterns_must_agree_on_levels(self):
        with pytest.raises(ValueError, match="disagree on version levels"):
            parse_config([_entry(refs=[_selector(
                type_="tag",
                match=["v{major}.{minor}", "v{major}.{minor}.{patch}"],
            )])])

    def test_versioned_patterns_agreeing_on_levels_is_fine(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag",
            match=["v{major}.{minor}.{patch}", "v{major}.{minor}.{patch}-{prerelease}"],
        )])])
        assert cfgs[0].selectors[0].levels == ("major", "minor", "patch")


class TestParseSince:
    def test_exactly_one_required_zero_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            parse_config([_entry(refs=[_selector(since={})])])

    def test_exactly_one_required_two_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            parse_config([_entry(refs=[_selector(since={"age": "1y", "date": "2025-01-01"})])])

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unknown keys"):
            parse_config([_entry(refs=[_selector(since={"bogus": "x"})])])

    def test_age_resolves_to_since(self):
        cfgs = parse_config([_entry(refs=[_selector(since={"age": "1y"})])])
        since = cfgs[0].selectors[0].since
        assert since.kind == "age"
        assert since.value == timedelta(days=365)

    def test_date_resolves_to_since(self):
        cfgs = parse_config([_entry(refs=[_selector(since={"date": "2025-01-01"})])])
        since = cfgs[0].selectors[0].since
        assert since.kind == "date"

    def test_commit_resolves_to_since(self):
        cfgs = parse_config([_entry(refs=[_selector(since={"commit": "deadbeef"})])])
        since = cfgs[0].selectors[0].since
        assert since == Since("commit", "deadbeef")

    def test_ref_resolves_to_since(self):
        cfgs = parse_config([_entry(refs=[_selector(since={"ref": "v8.0.0"})])])
        since = cfgs[0].selectors[0].since
        assert since == Since("ref", "v8.0.0")


class TestParseRetain:
    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match="unknown keys"):
            parse_config([_entry(refs=[_selector(retain={"bogus": 1})])])

    def test_count_must_be_int_gte_1(self):
        with pytest.raises(ValueError, match="retain.count"):
            parse_config([_entry(refs=[_selector(retain={"count": 0})])])

    def test_count_non_int_raises(self):
        with pytest.raises(ValueError, match="retain.count"):
            parse_config([_entry(refs=[_selector(retain={"count": "5"})])])

    def test_version_without_versioned_match_raises(self):
        with pytest.raises(ValueError, match="has no version tokens"):
            parse_config([_entry(refs=[_selector(
                type_="tag", match="my-dev-tag", retain={"version": {"majors": 2}},
            )])])

    def test_version_unknown_level_raises(self):
        with pytest.raises(ValueError, match="unknown levels"):
            parse_config([_entry(refs=[_selector(
                type_="tag", match="v{major}.{minor}.{patch}",
                retain={"version": {"bogus": 2}},
            )])])

    def test_version_counts_below_one_raise(self):
        with pytest.raises(ValueError):
            parse_config([_entry(refs=[_selector(
                type_="tag", match="v{major}.{minor}.{patch}",
                retain={"version": {"majors": 0}},
            )])])

    def test_version_null_level_is_no_constraint(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}",
            retain={"version": {"majors": 2, "minors": None}},
        )])])
        assert cfgs[0].selectors[0].retain.version.counts == {"major": 2}

    def test_prerelease_enum_invalid_raises(self):
        with pytest.raises(ValueError, match="'keep' or 'superseded'"):
            parse_config([_entry(refs=[_selector(retain={"prerelease": "bogus"})])])

    def test_all_default_retain_collapses_to_none(self):
        # age/count/version all None and prerelease default "keep" -> Retain.is_empty() -> None.
        cfgs = parse_config([_entry(refs=[_selector(retain={})])])
        assert cfgs[0].selectors[0].retain is None

    def test_non_empty_retain_is_kept(self):
        cfgs = parse_config([_entry(refs=[_selector(retain={"count": 5})])])
        assert cfgs[0].selectors[0].retain is not None
        assert cfgs[0].selectors[0].retain.count == 5

    def test_no_retain_key_means_keep_forever(self):
        cfgs = parse_config([_entry(refs=[_selector()])])
        assert cfgs[0].selectors[0].retain is None


class TestSelectorMatches:
    def test_ref_type_mismatch_returns_none(self):
        cfgs = parse_config([_entry(refs=[_selector(type_="branch", match="main")])])
        sel = cfgs[0].selectors[0]
        assert sel.matches("tag", "main") is None

    def test_matches_if_any_pattern_hits(self):
        cfgs = parse_config([_entry(refs=[_selector(match=["main", "dev"])])])
        sel = cfgs[0].selectors[0]
        assert sel.matches("branch", "dev") is not None
        assert sel.matches("branch", "main") is not None
        assert sel.matches("branch", "other") is None


class TestSinceVersionFloor:
    def test_full_ref_name(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}", since={"ref": "v8.17.0"},
        )])])
        assert cfgs[0].selectors[0].since_version_floor() == (8, 17, 0)

    def test_bare_version(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}", since={"ref": "8.17.0"},
        )])])
        assert cfgs[0].selectors[0].since_version_floor() == (8, 17, 0)

    def test_non_version_anchor_returns_none(self):
        # A branch name with no digits given as the `ref` anchor under a versioned selector.
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}", since={"ref": "main"},
        )])])
        assert cfgs[0].selectors[0].since_version_floor() is None

    def test_date_based_since_returns_none(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}", since={"age": "1y"},
        )])])
        assert cfgs[0].selectors[0].since_version_floor() is None

    def test_no_since_returns_none(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}",
        )])])
        assert cfgs[0].selectors[0].since_version_floor() is None

    def test_non_versioned_selector_returns_none(self):
        cfgs = parse_config([_entry(refs=[_selector(
            type_="tag", match="my-dev-tag", since={"ref": "my-dev-tag"},
        )])])
        assert cfgs[0].selectors[0].since_version_floor() is None


class TestDottedKeys:
    def test_since_ref_dotted(self):
        cfgs = parse_config([_entry(refs=[{
            "type": "tag", "match": "v{major}.{minor}.{patch}", "since.ref": "v8.0.0",
        }])])
        assert cfgs[0].selectors[0].since == Since("ref", "v8.0.0")

    def test_retain_version_dotted(self):
        cfgs = parse_config([_entry(refs=[{
            "type": "tag", "match": "v{major}.{minor}.{patch}",
            "retain.version.majors": 2, "retain.version.patches": 1,
        }])])
        assert cfgs[0].selectors[0].retain.version.counts == {"major": 2, "patch": 1}

    def test_dotted_equivalent_to_nested(self):
        nested = parse_config([_entry(refs=[_selector(
            type_="tag", match="v{major}.{minor}.{patch}",
            since={"ref": "v8.0.0"}, retain={"version": {"majors": 2, "patches": 1}},
        )])])
        dotted = parse_config([_entry(refs=[{
            "type": "tag", "match": "v{major}.{minor}.{patch}",
            "since.ref": "v8.0.0", "retain.version.majors": 2, "retain.version.patches": 1,
        }])])
        assert nested[0].selectors[0] == dotted[0].selectors[0]

    def test_mixed_nested_and_dotted(self):
        cfgs = parse_config([_entry(refs=[{
            "type": "tag", "match": "v{major}.{minor}.{patch}",
            "retain": {"version": {"majors": 2}}, "retain.version.patches": 1,
        }])])
        assert cfgs[0].selectors[0].retain.version.counts == {"major": 2, "patch": 1}

    def test_conflicting_since_forms_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            parse_config([_entry(refs=[{
                "type": "branch", "match": "main",
                "since": {"age": "1y"}, "since.ref": "main",
            }])])

    def test_scalar_dotted_conflict_raises(self):
        with pytest.raises(ValueError, match="conflicts with a non-mapping value"):
            parse_config([_entry(refs=[{
                "type": "branch", "match": "main",
                "since": "not-a-mapping", "since.ref": "main",
            }])])

    def test_duplicate_dotted_leaf_raises(self):
        # Nested `since.ref` and dotted `since.ref` both set the same leaf -> reject as a
        # duplicate rather than silently letting the later one win.
        with pytest.raises(ValueError, match="duplicate key"):
            parse_config([_entry(refs=[{
                "type": "branch", "match": "main",
                "since": {"ref": "main"}, "since.ref": "other",
            }])])
