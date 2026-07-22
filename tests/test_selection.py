"""Unit tests for sourcerer.commands.index.selection._resolve_entry, focused on the
incremental/snapshot mode plumbing: mode propagation onto resolved Units, same-mode
deduplication, and the mixed-mode rejection when one concrete branch is selected in both
modes. Remote ref listing is mocked so no network is touched."""

# Standard packages
from unittest.mock import patch

# Third-party packages
import pytest

# App packages
from sourcerer.config import parse_config
from sourcerer.commands.index import selection


def _resolve(refs, names_by_kind):
    cfg = parse_config([{"org": "acme", "repo": "widgets", "refs": refs}])[0]

    def fake_list(org, repo, kind):
        return names_by_kind.get(kind, [])

    with patch.object(selection, "list_remote_ref_names", side_effect=fake_list):
        return selection._resolve_entry(cfg)


class TestUpdateModePropagation:
    def test_snapshot_default_propagates(self):
        units = _resolve(
            [{"type": "branch", "match": "main"}],
            {"heads": ["main", "dev"]},
        )
        main = next(u for u in units if u.ref == "main")
        assert main.update_mode == "snapshot"

    def test_incremental_propagates(self):
        units = _resolve(
            [{"type": "branch", "match": "main", "update": "incremental"}],
            {"heads": ["main"]},
        )
        assert len(units) == 1
        assert units[0].update_mode == "incremental"
        assert units[0].kind == "branch"


class TestSameModeDedup:
    def test_two_selectors_same_branch_same_mode_dedupe(self):
        units = _resolve(
            [
                {"type": "branch", "match": "main"},
                {"type": "branch", "match": "m*"},
            ],
            {"heads": ["main"]},
        )
        assert len([u for u in units if u.ref == "main"]) == 1
        assert units[0].update_mode == "snapshot"

    def test_two_incremental_selectors_same_branch_dedupe(self):
        units = _resolve(
            [
                {"type": "branch", "match": "main", "update": "incremental"},
                {"type": "branch", "match": "m*", "update": "incremental"},
            ],
            {"heads": ["main"]},
        )
        assert len(units) == 1
        assert units[0].update_mode == "incremental"


class TestMixedModeRejection:
    def test_main_matched_by_exact_snapshot_and_glob_incremental_raises(self):
        with pytest.raises(ValueError, match="both"):
            _resolve(
                [
                    {"type": "branch", "match": "main"},
                    {"type": "branch", "match": "m*", "update": "incremental"},
                ],
                {"heads": ["main", "other"]},
            )

    def test_incremental_first_then_snapshot_raises(self):
        with pytest.raises(ValueError, match="incremental"):
            _resolve(
                [
                    {"type": "branch", "match": "main", "update": "incremental"},
                    {"type": "branch", "match": "main"},
                ],
                {"heads": ["main"]},
            )

    def test_disjoint_branches_in_different_modes_ok(self):
        units = _resolve(
            [
                {"type": "branch", "match": "main"},
                {"type": "branch", "match": "dev", "update": "incremental"},
            ],
            {"heads": ["main", "dev"]},
        )
        modes = {u.ref: u.update_mode for u in units}
        assert modes == {"main": "snapshot", "dev": "incremental"}
