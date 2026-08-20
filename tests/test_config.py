"""Unit tests for the sourcerer.yml model + parser/validator in sourcerer.config. Only
load_config touches the filesystem (a single open()); everything tested here is parse_config
and its pure helpers over already-parsed data. parse_config returns a Config(hosts, repos)."""

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
            ("15m", 15 * 60),     # minutes
            ("12h", 12 * 3600),
            ("7d", 7 * 86400),
            ("2w", 2 * 604800),
            ("3M", 3 * 2592000),  # months = 30d each
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


def _git(host="github", org="acme", repo="widgets", ref_type="branch"):
    return {"host": host, "org": org, "repo": repo, "ref_type": ref_type}


def _source(host="github", org="acme", repo="widgets", ref_type="branch",
            match="main", since=None, retain=None, omit_match=False, strategy=None, index=None):
    src = {"git": _git(host, org, repo, ref_type)}
    if not omit_match:
        src["match"] = match
    if since is not None:
        src["since"] = since
    if retain is not None:
        src["retain"] = retain
    # strategy is a convenience shim: merges {"strategy": ...} into the index: block.
    if strategy is not None or index is not None:
        merged = dict(index or {})
        if strategy is not None:
            merged["strategy"] = strategy
        src["index"] = merged
    return src


def _cfg(sources):
    return parse_config({"sources": sources})


class TestParseConfigStructure:
    def test_non_mapping_raises(self):
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            parse_config([{"git": _git()}])

    def test_none_is_empty_config(self):
        cfg = parse_config(None)
        assert cfg.repos == []
        assert "github" in cfg.hosts  # built-in defaults always present

    def test_unknown_top_level_key_raises(self):
        with pytest.raises(ValueError, match="unknown top-level keys"):
            parse_config({"bogus": 1})

    def test_sources_not_a_list_raises(self):
        with pytest.raises(ValueError, match="'sources' must be a list"):
            parse_config({"sources": {"git": _git()}})

    def test_hosts_not_a_list_raises(self):
        with pytest.raises(ValueError, match="'hosts' must be a list"):
            parse_config({"hosts": {"id": "x"}})

    def test_source_not_a_mapping_raises(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            parse_config({"sources": ["nope"]})

    def test_omitted_sources_yields_no_repos(self):
        assert parse_config({}).repos == []

    def test_grouping_by_host_org_repo(self):
        cfg = _cfg([
            _source(ref_type="branch", match="main"),
            _source(ref_type="tag", match="v{major}.{minor}.{patch}"),
        ])
        # Same (host, org, repo) -> one RepoConfig with two selectors.
        assert len(cfg.repos) == 1
        assert len(cfg.repos[0].selectors) == 2
        assert (cfg.repos[0].host, cfg.repos[0].org, cfg.repos[0].repo) == ("github", "acme", "widgets")

    def test_distinct_hosts_are_separate_repos(self):
        cfg = parse_config({
            "hosts": [{
                "id": "my_gitea",
                "urls": {
                    "clone": "https://g/{git.org}/{git.repo}.git",
                    "directory": "https://g/{git.org}/{git.repo}/{file.directory}",
                    "file": "https://g/{git.org}/{git.repo}/{file.path}",
                    "line": "https://g/{git.org}/{git.repo}/{file.path}#L{line.number}",
                    "line_range": "https://g/{git.org}/{git.repo}/{file.path}#L{line.number_start}",
                },
            }],
            "sources": [
                _source(host="github", match="main"),
                _source(host="my_gitea", match="main"),
            ],
        })
        assert {c.host for c in cfg.repos} == {"github", "my_gitea"}


class TestParseGitScope:
    def test_missing_git_raises(self):
        with pytest.raises(ValueError, match="'git' must be a mapping"):
            parse_config({"sources": [{"match": "main"}]})

    @pytest.mark.parametrize("field", ["host", "org", "repo", "ref_type"])
    def test_missing_field_raises(self, field):
        git = _git()
        del git[field]
        with pytest.raises(ValueError, match=f"'{field}' must be a non-empty string"):
            parse_config({"sources": [{"git": git, "match": "main"}]})

    def test_git_list_value_rejected(self):
        git = _git()
        git["org"] = ["a", "b"]
        with pytest.raises(ValueError, match="'org' must be a non-empty string"):
            parse_config({"sources": [{"git": git, "match": "main"}]})

    def test_bad_ref_type_raises(self):
        with pytest.raises(ValueError, match="ref_type"):
            _cfg([_source(ref_type="sha")])

    def test_unknown_git_key_raises(self):
        git = {**_git(), "project": "x"}
        with pytest.raises(ValueError, match="unknown keys"):
            parse_config({"sources": [{"git": git, "match": "main"}]})

    def test_bad_host_id_raises(self):
        with pytest.raises(ValueError, match="git.host"):
            _cfg([_source(host="Git/Hub")])

    def test_unknown_host_raises(self):
        with pytest.raises(ValueError, match="unknown host"):
            _cfg([_source(host="notahost")])

class TestParseSourceMatch:
    def test_match_as_string(self):
        cfg = _cfg([_source(match="main")])
        assert cfg.repos[0].selectors[0].raw_patterns == ["main"]

    def test_match_as_list(self):
        cfg = _cfg([_source(match=["main", "dev"])])
        assert cfg.repos[0].selectors[0].raw_patterns == ["main", "dev"]

    def test_match_empty_list_raises(self):
        with pytest.raises(ValueError, match="'match' must be"):
            _cfg([_source(match=[])])

    def test_match_missing_raises(self):
        with pytest.raises(ValueError, match="'match' must be"):
            _cfg([_source(omit_match=True)])

    def test_versioned_patterns_must_agree_on_levels(self):
        with pytest.raises(ValueError, match="disagree on version levels"):
            _cfg([_source(ref_type="tag", match=["v{major}.{minor}", "v{major}.{minor}.{patch}"])])

    def test_versioned_patterns_agreeing_on_levels_is_fine(self):
        cfg = _cfg([_source(ref_type="tag",
                            match=["v{major}.{minor}.{patch}", "v{major}.{minor}.{patch}-{prerelease}"])])
        assert cfg.repos[0].selectors[0].levels == ("major", "minor", "patch")


class TestParseIndexStrategy:
    def test_default_is_snapshot(self):
        cfg = _cfg([_source()])
        assert cfg.repos[0].selectors[0].index_strategy == "snapshot"

    def test_incremental_accepted_on_branch(self):
        cfg = _cfg([_source(ref_type="branch", strategy="incremental")])
        assert cfg.repos[0].selectors[0].index_strategy == "incremental"

    def test_incremental_rejected_on_tag(self):
        with pytest.raises(ValueError, match="only valid for git.ref_type: branch"):
            _cfg([_source(ref_type="tag", match="v1.0.0", strategy="incremental")])

    def test_incremental_rejected_on_commit(self):
        with pytest.raises(ValueError, match="only valid for git.ref_type: branch"):
            _cfg([_source(ref_type="commit", match="cfefb3b", strategy="incremental")])

    def test_invalid_strategy_raises(self):
        with pytest.raises(ValueError, match="must be one of"):
            _cfg([_source(strategy="bogus")])

    def test_incremental_with_since_raises(self):
        with pytest.raises(ValueError, match="cannot be combined with 'since'"):
            _cfg([_source(ref_type="branch", strategy="incremental", since={"age": "1y"})])

    def test_incremental_with_retain_raises(self):
        with pytest.raises(ValueError, match="cannot be combined with 'retain'"):
            _cfg([_source(ref_type="branch", strategy="incremental", retain={"count": 5})])

    def test_incremental_with_commit_level_index_raises(self):
        with pytest.raises(ValueError, match="cannot be combined with 'index.level: commit'"):
            _cfg([_source(ref_type="branch", strategy="incremental", index={"level": "commit"})])

    def test_top_level_update_key_raises(self):
        with pytest.raises(ValueError, match="unknown keys"):
            _cfg([{"git": {"host": "github", "org": "acme", "repo": "widgets", "ref_type": "branch"},
                   "match": "main", "update": "incremental"}])

    def test_incremental_with_repo_level_index_is_fine(self):
        cfg = _cfg([_source(ref_type="branch", strategy="incremental", index={"level": "repo"})])
        assert cfg.repos[0].selectors[0].index_strategy == "incremental"


class TestParseCommitSource:
    def test_full_sha_accepted(self):
        cfg = _cfg([_source(ref_type="commit", match="a" * 40)])
        assert cfg.repos[0].selectors[0].raw_patterns == ["a" * 40]

    def test_short_prefix_accepted(self):
        cfg = _cfg([_source(ref_type="commit", match="cfefb3b")])
        assert cfg.repos[0].selectors[0].raw_patterns == ["cfefb3b"]

    def test_lowercase_normalized(self):
        cfg = _cfg([_source(ref_type="commit", match="CFEFB3B")])
        assert cfg.repos[0].selectors[0].raw_patterns == ["cfefb3b"]

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="7-40 hex chars"):
            _cfg([_source(ref_type="commit", match="abc123")])

    def test_non_hex_raises(self):
        with pytest.raises(ValueError, match="7-40 hex chars"):
            _cfg([_source(ref_type="commit", match="not-a-sha")])

    def test_since_raises(self):
        with pytest.raises(ValueError, match="do not support 'since'"):
            _cfg([_source(ref_type="commit", match="cfefb3b", since={"age": "1y"})])

    def test_retain_age_allowed(self):
        cfg = _cfg([_source(ref_type="commit", match="cfefb3b", retain={"age": "2y"})])
        assert cfg.repos[0].selectors[0].retain.age == timedelta(days=730)

    def test_retain_count_raises(self):
        with pytest.raises(ValueError, match="only 'age' retention"):
            _cfg([_source(ref_type="commit", match="cfefb3b", retain={"count": 5})])

    def test_retain_prerelease_superseded_raises(self):
        with pytest.raises(ValueError, match="only 'age' retention"):
            _cfg([_source(ref_type="commit", match="cfefb3b", retain={"prerelease": "superseded"})])


class TestParseSince:
    def test_exactly_one_required_zero_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            _cfg([_source(since={})])

    def test_exactly_one_required_two_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            _cfg([_source(since={"age": "1y", "date": "2025-01-01"})])

    def test_age_resolves_to_since(self):
        since = _cfg([_source(since={"age": "1y"})]).repos[0].selectors[0].since
        assert since.kind == "age" and since.value == timedelta(days=365)

    def test_ref_resolves_to_since(self):
        since = _cfg([_source(since={"ref": "v8.0.0"})]).repos[0].selectors[0].since
        assert since == Since("ref", "v8.0.0")


class TestParseRetain:
    def test_count_must_be_int_gte_1(self):
        with pytest.raises(ValueError, match="retain.count"):
            _cfg([_source(retain={"count": 0})])

    def test_version_without_versioned_match_raises(self):
        with pytest.raises(ValueError, match="has no version tokens"):
            _cfg([_source(ref_type="tag", match="my-dev-tag", retain={"version": {"majors": 2}})])

    def test_version_null_level_is_no_constraint(self):
        cfg = _cfg([_source(ref_type="tag", match="v{major}.{minor}.{patch}",
                            retain={"version": {"majors": 2, "minors": None}})])
        assert cfg.repos[0].selectors[0].retain.version.counts == {"major": 2}

    def test_all_default_retain_collapses_to_none(self):
        cfg = _cfg([_source(retain={})])
        assert cfg.repos[0].selectors[0].retain is None

    def test_no_retain_key_means_keep_forever(self):
        cfg = _cfg([_source()])
        assert cfg.repos[0].selectors[0].retain is None


class TestSelectorMatches:
    def test_ref_type_mismatch_returns_none(self):
        sel = _cfg([_source(ref_type="branch", match="main")]).repos[0].selectors[0]
        assert sel.matches("tag", "main") is None

    def test_matches_if_any_pattern_hits(self):
        sel = _cfg([_source(match=["main", "dev"])]).repos[0].selectors[0]
        assert sel.matches("branch", "dev") is not None
        assert sel.matches("branch", "other") is None

    def test_commit_prefix_matches_full_sha(self):
        sel = _cfg([_source(ref_type="commit", match="cfefb3b")]).repos[0].selectors[0]
        assert sel.matches("commit", "cfefb3b2378ccbadefa7c8f4f9e21b3a1d2e5f60") is not None
        assert sel.matches("commit", "deadbeef" * 5) is None


class TestSinceVersionFloor:
    def test_full_ref_name(self):
        cfg = _cfg([_source(ref_type="tag", match="v{major}.{minor}.{patch}", since={"ref": "v8.17.0"})])
        assert cfg.repos[0].selectors[0].since_version_floor() == (8, 17, 0)

    def test_date_based_since_returns_none(self):
        cfg = _cfg([_source(ref_type="tag", match="v{major}.{minor}.{patch}", since={"age": "1y"})])
        assert cfg.repos[0].selectors[0].since_version_floor() is None


class TestDottedKeys:
    def test_git_host_dotted(self):
        cfg = parse_config({"sources": [{
            "git.host": "github", "git.org": "acme", "git.repo": "widgets", "git.ref_type": "branch",
            "match": "main",
        }]})
        assert (cfg.repos[0].host, cfg.repos[0].org, cfg.repos[0].repo) == ("github", "acme", "widgets")

    def test_since_and_retain_dotted(self):
        cfg = parse_config({"sources": [{
            "git": _git(ref_type="tag"),
            "match": "v{major}.{minor}.{patch}",
            "since.ref": "v8.0.0",
            "retain.version.majors": 2, "retain.version.patches": 1,
        }]})
        sel = cfg.repos[0].selectors[0]
        assert sel.since == Since("ref", "v8.0.0")
        assert sel.retain.version.counts == {"major": 2, "patch": 1}


class TestHostsMerge:
    def test_override_one_leaf_keeps_defaults(self):
        cfg = parse_config({"hosts": [{"id": "github", "name": "GH Enterprise"}]})
        gh = cfg.hosts["github"]
        assert gh.name == "GH Enterprise"
        # urls untouched -> still the github.com defaults
        assert "github.com" in gh.url_template("clone")

    def test_all_builtins_present(self):
        cfg = parse_config({})
        for hid in ("github", "gitlab", "bitbucket", "azure-devops", "aws-codecommit"):
            assert hid in cfg.hosts


class TestIndexRouting:
    """sources[i].index.level / index.suffix parsing + validation (specs/sourcerer-yml.md)."""

    def test_defaults_when_omitted(self):
        sel = _cfg([_source()]).repos[0].selectors[0]
        assert sel.index_level == "repo" and sel.index_suffix is None

    def test_level_and_suffix_nested(self):
        src = _source()
        src["index"] = {"level": "commit", "suffix": "deploy"}
        sel = _cfg([src]).repos[0].selectors[0]
        assert sel.index_level == "commit" and sel.index_suffix == "deploy"

    def test_dotted_form(self):
        src = _source()
        src["index.level"] = "org"
        sel = _cfg([src]).repos[0].selectors[0]
        assert sel.index_level == "org"

    def test_empty_suffix_is_none(self):
        src = _source()
        src["index"] = {"suffix": ""}
        assert _cfg([src]).repos[0].selectors[0].index_suffix is None

    def test_invalid_level_rejected(self):
        src = _source()
        src["index"] = {"level": "bogus"}
        with pytest.raises(ValueError, match="index.level"):
            _cfg([src])

    def test_bad_suffix_chars_rejected(self):
        for bad in ("a~b", "a^b", "a/b", "a b", "Deploy"):
            src = _source()
            src["index"] = {"suffix": bad}
            with pytest.raises(ValueError, match="index.suffix"):
                _cfg([src])

    def test_per_source_routing_allowed_within_a_repo(self):
        """Two sources sharing (host, org, repo) may route to different indices (the kibana
        release-vs-deploy case) -- no agreement rule. They group into one RepoConfig."""
        release = _source(ref_type="tag", match="v{major}.{minor}.{patch}")
        deploy = _source(ref_type="tag", match="deploy@{major}")
        deploy["index"] = {"suffix": "deploy"}
        cfg = _cfg([release, deploy])
        assert len(cfg.repos) == 1
        sels = cfg.repos[0].selectors
        assert sels[0].index_suffix is None and sels[1].index_suffix == "deploy"
