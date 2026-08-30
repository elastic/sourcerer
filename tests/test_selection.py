"""Tests for _resolve_entry's delta-mode tag stream collapsing.

A delta-mode tag selector whose match pattern covers multiple remote tags must emit exactly ONE
stream Unit per raw pattern (not one Unit per matching tag name). The Unit's ref is the literal
match pattern string; the concrete newest tag is resolved post-clone via ref_dates.
"""

from unittest.mock import patch

import yaml

from sourcerer.commands.index.selection import _resolve_entry
from sourcerer.config import parse_config


def _resolve(source_yaml: str, remote_tags: dict[str, str], remote_branches: dict[str, str] | None = None):
    """Run _resolve_entry with mocked ls-remote returning the given ref maps."""
    raw = yaml.safe_load(f"""
hosts:
  - id: github
    type: github
    base_url: https://github.com
sources:
{source_yaml}
""")
    cfg = parse_config(raw)
    repo_cfg = cfg.repos[0]
    host = cfg.hosts[repo_cfg.host]

    def _fake_list_remote_refs(url, kind):
        if kind == "tags":
            return remote_tags
        return remote_branches or {}

    with patch("sourcerer.commands.index.selection.list_remote_refs", side_effect=_fake_list_remote_refs):
        return _resolve_entry(repo_cfg, host)


class TestDeltaTagStreamSelection:
    """Delta-mode tag selectors collapse to one stream Unit per pattern."""

    MANY_TAGS = {f"deploy@{i}": f"sha{i}" for i in range(1, 10)}

    def test_delta_tag_glob_emits_one_unit_per_pattern(self):
        """deploy@* matching 9 tags → 1 stream Unit, ref == pattern."""
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "deploy@*"
    mode: delta
""", remote_tags=self.MANY_TAGS)
        assert len(units) == 1
        assert units[0].ref == "deploy@*"
        assert units[0].kind == "tag"
        assert units[0].mode == "delta"
        assert units[0].remote_sha is None  # resolved post-clone, not from ls-remote

    def test_delta_tag_stream_unit_carries_match_pattern(self):
        """Stream unit has ref_pattern == pattern (stream identity, stable across tag promotions)."""
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "deploy@{major}"
    mode: delta
""", remote_tags=self.MANY_TAGS)
        assert len(units) == 1
        u = units[0]
        assert u.ref == "deploy@{major}"
        assert u.ref_pattern == "deploy@{major}", (
            f"Stream unit.ref_pattern must equal the pattern, got {u.ref_pattern!r}"
        )

    def test_delta_tag_version_pattern_emits_one_unit(self):
        """deploy@{major} matching 9 deploy@N tags → 1 stream Unit."""
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "deploy@{major}"
    mode: delta
""", remote_tags=self.MANY_TAGS)
        assert len(units) == 1
        assert units[0].ref == "deploy@{major}"

    def test_delta_tag_multi_pattern_emits_one_unit_per_pattern(self):
        """Two raw patterns → two stream Units with distinct identities."""
        tags = {**{f"deploy@{i}": f"sha{i}" for i in range(1, 5)},
                **{f"release@{i}": f"rsha{i}" for i in range(1, 4)}}
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: ["deploy@*", "release@*"]
    mode: delta
""", remote_tags=tags)
        assert len(units) == 2
        refs = {u.ref for u in units}
        assert refs == {"deploy@*", "release@*"}

    def test_delta_tag_pattern_with_no_matches_emits_nothing(self):
        """If no remote tag matches the pattern, no stream Unit is emitted."""
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "release@*"
    mode: delta
""", remote_tags=self.MANY_TAGS)  # only deploy@* tags, no release@*
        assert units == []

    def test_snapshot_tag_still_emits_one_unit_per_tag(self):
        """snapshot mode is unchanged: one Unit per matching tag name."""
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "deploy@*"
""", remote_tags={f"deploy@{i}": f"sha{i}" for i in range(1, 4)})
        assert len(units) == 3
        assert all(u.ref.startswith("deploy@") and not u.ref.endswith("*") for u in units)
        assert all(u.mode == "snapshot" for u in units)

    def test_delta_branch_still_emits_one_unit_per_branch(self):
        """delta mode on branches (not tags) is unchanged: one Unit per matching branch."""
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: branch}
    match: "main"
    mode: delta
""", remote_tags={}, remote_branches={"main": "abc123"})
        assert len(units) == 1
        assert units[0].ref == "main"
        assert units[0].remote_sha == "abc123"

    def test_snapshot_tag_units_use_raw_match_pattern(self):
        """Snapshot tag units carry the raw match pattern in ref_pattern, not the concrete tag."""
        snapshot_units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "deploy@*"
""", remote_tags={f"deploy@{i}": f"sha{i}" for i in range(1, 4)})
        for u in snapshot_units:
            assert u.ref_pattern == "deploy@*", (
                f"snapshot tag unit.ref_pattern must be the raw pattern, got {u.ref_pattern!r}"
            )
            assert u.ref != u.ref_pattern, (
                f"snapshot tag unit.ref (concrete) should differ from ref_pattern (pattern), got ref={u.ref!r}"
            )

    def test_delta_branch_units_have_ref_pattern_equal_to_ref(self):
        """Delta branch units: branch name is its own pattern, so ref_pattern == ref."""
        # Delta branch: ref_pattern == branch name (literal match, no version components).
        branch_units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: branch}
    match: "main"
    mode: delta
""", remote_tags={}, remote_branches={"main": "abc123"})
        for u in branch_units:
            assert u.ref_pattern == u.ref, f"branch unit.ref_pattern must == unit.ref, got ref_pattern={u.ref_pattern!r} ref={u.ref!r}"

    def test_two_delta_tag_sources_on_same_repo_produce_distinct_streams(self):
        """Two delta-tag selectors on the same repo with different patterns are two streams."""
        tags = {**{f"deploy@{i}": f"sha{i}" for i in range(1, 4)},
                **{f"v1.{i}.0": f"vsha{i}" for i in range(1, 4)}}
        units = _resolve("""
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "deploy@*"
    mode: delta
  - git: {host: github, org: elastic, repo: kibana, ref_type: tag}
    match: "v{major}.{minor}.{patch}"
    mode: delta
""", remote_tags=tags)
        assert len(units) == 2
        refs = {u.ref for u in units}
        assert "deploy@*" in refs
        assert "v{major}.{minor}.{patch}" in refs


class TestRangeGatedSelection:
    """retain.version.range gates which selector claims a ref, enabling version-windowed routing.

    Regression tests for the bug where all tags went to the first selector's index.suffix because
    match_pattern was the only selection gate (range was pruning-only).  With the fix, a ranged
    selector only claims refs whose version falls inside its range, letting sibling selectors claim
    the rest.
    """

    # Two adjacent windows with distinct suffixes — the minimal repro of the bug report.
    WINDOWED_CONFIG = """
  - git: {host: github, org: elastic, repo: elasticsearch, ref_type: tag}
    match: "v{major}.{minor}.{patch}"
    index: {suffix: "10.x"}
    retain:
      version:
        range: ">=10.0.0 <11.0.0"
  - git: {host: github, org: elastic, repo: elasticsearch, ref_type: tag}
    match: "v{major}.{minor}.{patch}"
    index: {suffix: "9.x"}
    retain:
      version:
        range: ">=9.0.0 <10.0.0"
"""

    TAGS = {
        "v10.1.0": "sha10_1_0",
        "v10.0.5": "sha10_0_5",
        "v9.9.0":  "sha9_9_0",
        "v9.0.0":  "sha9_0_0",
        # outside both windows: should produce no Unit
        "v8.99.0": "sha8_99_0",
    }

    def test_each_tag_gets_its_windows_suffix(self):
        """v10.x tags → suffix '10.x'; v9.x tags → suffix '9.x'."""
        units = _resolve(self.WINDOWED_CONFIG, remote_tags=self.TAGS)
        by_ref = {u.ref: u for u in units}

        assert by_ref["v10.1.0"].index_suffix == "10.x"
        assert by_ref["v10.0.5"].index_suffix == "10.x"
        assert by_ref["v9.9.0"].index_suffix == "9.x"
        assert by_ref["v9.0.0"].index_suffix == "9.x"

    def test_tag_outside_all_windows_produces_no_unit(self):
        """v8.99.0 matches the glob but falls outside both ranges → no Unit emitted."""
        units = _resolve(self.WINDOWED_CONFIG, remote_tags=self.TAGS)
        refs = {u.ref for u in units}
        assert "v8.99.0" not in refs

    def test_total_unit_count(self):
        """Exactly one Unit per in-window tag (4 of 5 tags qualify)."""
        units = _resolve(self.WINDOWED_CONFIG, remote_tags=self.TAGS)
        assert len(units) == 4

    def test_range_gate_is_order_independent(self):
        """Swapping window order in the config must not change which suffix each tag gets."""
        swapped_config = """
  - git: {host: github, org: elastic, repo: elasticsearch, ref_type: tag}
    match: "v{major}.{minor}.{patch}"
    index: {suffix: "9.x"}
    retain:
      version:
        range: ">=9.0.0 <10.0.0"
  - git: {host: github, org: elastic, repo: elasticsearch, ref_type: tag}
    match: "v{major}.{minor}.{patch}"
    index: {suffix: "10.x"}
    retain:
      version:
        range: ">=10.0.0 <11.0.0"
"""
        units = _resolve(swapped_config, remote_tags=self.TAGS)
        by_ref = {u.ref: u for u in units}
        assert by_ref["v10.1.0"].index_suffix == "10.x"
        assert by_ref["v9.9.0"].index_suffix == "9.x"

    def test_rangeless_catch_all_claims_outside_windowed_tags(self):
        """A selector with no range still claims everything its match covers (it's not windowed)."""
        config_with_catchall = self.WINDOWED_CONFIG + """
  - git: {host: github, org: elastic, repo: elasticsearch, ref_type: tag}
    match: "v{major}.{minor}.{patch}"
    index: {suffix: "old"}
"""
        units = _resolve(config_with_catchall, remote_tags=self.TAGS)
        by_ref = {u.ref: u for u in units}
        # Tags in the two windows still go to their window suffix.
        assert by_ref["v10.1.0"].index_suffix == "10.x"
        assert by_ref["v9.9.0"].index_suffix == "9.x"
        # The tag outside all windows is now claimed by the rangeless catch-all.
        assert by_ref["v8.99.0"].index_suffix == "old"
