"""Tests for setup's index-template loading and the v2 template JSON contract.

Two concerns:
  1. `load_index_templates` upserts every JSON template (v1 + v2) via a mocked ES client,
     without deleting or mutating v1 indices.
  2. The v2 template JSON matches the schema invariants (INV-003, INV-004, INV-009): lookup
     mode + one shard on the refs index, case-sensitive (no-normalizer) ref fields on all three
     v2 templates, and untouched v1 templates.
"""

# Standard packages
import json
from unittest.mock import MagicMock

# App packages
from sourcerer.commands.setup.command import (
    ELASTICSEARCH_INDEX_TEMPLATES_DIR,
    _QUERY_SEED_INDICES,
    ensure_query_seed_indices,
    load_index_templates,
)
from sourcerer.indices import (
    FILES_INDEX_PREFIX,
    FILES_INDEX_PREFIX_V2,
    LINES_INDEX_PREFIX,
    LINES_INDEX_PREFIX_V2,
    REFS_INDEX_V2,
)
from sourcerer.planner import parse_index_name

_TEMPLATES = ELASTICSEARCH_INDEX_TEMPLATES_DIR


def _load(name: str) -> dict:
    return json.loads((_TEMPLATES / f"{name}.json").read_text())


class TestLoadIndexTemplates:
    def test_loads_all_v1_and_v2_templates(self):
        es = MagicMock()
        loaded = load_index_templates(es)
        # Every template file (v1 + v2) is upserted; nothing is deleted.
        for name in (
            "sourcerer-v1-files", "sourcerer-v1-lines", "sourcerer-v1-refs",
            "sourcerer-v2-files", "sourcerer-v2-lines", "sourcerer-v2-refs",
        ):
            assert name in loaded
        es.indices.delete.assert_not_called()
        es.indices.delete_index_template.assert_not_called()
        assert es.indices.put_index_template.call_count == len(loaded)

    def test_put_uses_stem_as_name_and_passes_template_body(self):
        es = MagicMock()
        load_index_templates(es)
        names = {c.kwargs["name"] for c in es.indices.put_index_template.call_args_list}
        assert "sourcerer-v2-refs" in names


class TestV2RefsTemplate:
    def test_lookup_mode_and_one_shard(self):
        idx = _load("sourcerer-v2-refs")["template"]["settings"]["index"]
        assert idx["mode"] == "lookup"
        assert str(idx["number_of_shards"]) == "1"

    def test_exact_index_pattern(self):
        assert _load("sourcerer-v2-refs")["index_patterns"] == ["sourcerer-v2-refs*"]

    def test_has_all_required_fields(self):
        props = _load("sourcerer-v2-refs")["template"]["mappings"]["properties"]
        git = props["git"]["properties"]
        for f in ("ref_key", "org", "repo", "ref", "ref_type", "commit",
                  "target_commit", "commit_date"):
            assert f in git, f"git.{f} missing from v2-refs"
        for f in ("status", "update_mode", "files_count", "lines_count",
                  "indexed_at", "update_started_at", "failed_at", "error"):
            assert f in props, f"{f} missing from v2-refs"


class TestV2ContentTemplates:
    def test_files_and_lines_patterns(self):
        assert _load("sourcerer-v2-files")["index_patterns"] == ["sourcerer-v2-files*"]
        assert _load("sourcerer-v2-lines")["index_patterns"] == ["sourcerer-v2-lines*"]

    def test_content_has_ref_key_ref_ref_type_and_no_commit(self):
        for name in ("sourcerer-v2-files", "sourcerer-v2-lines"):
            git = _load(name)["template"]["mappings"]["properties"]["git"]["properties"]
            assert "ref_key" in git and "ref" in git and "ref_type" in git
            assert "commit" not in git, f"{name} must not store git.commit"


class TestV2NoNormalizerOnRefFields:
    def test_ref_key_ref_org_repo_have_no_normalizer(self):
        for name in ("sourcerer-v2-files", "sourcerer-v2-lines", "sourcerer-v2-refs"):
            git = _load(name)["template"]["mappings"]["properties"]["git"]["properties"]
            for field in ("ref_key", "ref", "org", "repo"):
                if field not in git:
                    continue
                assert git[field]["type"] == "keyword"
                assert "normalizer" not in git[field], (
                    f"{name} git.{field} must have no normalizer (case-sensitive identity)"
                )


class TestQuerySeedIndices:
    def test_creates_refs_lookup_and_four_anchors_when_absent(self):
        es = MagicMock()
        es.indices.exists.return_value = False
        created = ensure_query_seed_indices(es)
        assert set(created) == {
            REFS_INDEX_V2,
            FILES_INDEX_PREFIX, LINES_INDEX_PREFIX,
            FILES_INDEX_PREFIX_V2, LINES_INDEX_PREFIX_V2,
        }
        made = {c.kwargs["index"] for c in es.indices.create.call_args_list}
        assert made == set(created)

    def test_is_idempotent_when_indices_exist(self):
        es = MagicMock()
        es.indices.exists.return_value = True
        assert ensure_query_seed_indices(es) == []
        es.indices.create.assert_not_called()


class TestAnchorNamesAreOrphanSafe:
    # The four schema-anchor indices are named with the bare prefix (no ~org~repo), which
    # parse_index_name rejects -- so the v1 orphan-prune sweep never classifies them as
    # deletable orphans.
    def test_anchor_prefixes_are_not_parseable_as_content_indices(self):
        for name in (FILES_INDEX_PREFIX, LINES_INDEX_PREFIX,
                     FILES_INDEX_PREFIX_V2, LINES_INDEX_PREFIX_V2):
            assert parse_index_name(name) is None, f"{name} would be seen by the orphan sweep"

    def test_seed_list_is_the_expected_five(self):
        assert _QUERY_SEED_INDICES[0] == REFS_INDEX_V2
        assert len(_QUERY_SEED_INDICES) == 5


class TestV1TemplatesUnchanged:
    def test_v1_org_repo_still_use_lowercase_normalizer(self):
        # A canary that the v1 schema was not accidentally altered by the v2 work.
        for name in ("sourcerer-v1-files", "sourcerer-v1-lines", "sourcerer-v1-refs"):
            git = _load(name)["template"]["mappings"]["properties"]["git"]["properties"]
            assert git["org"]["normalizer"] == "lowercase"
            assert git["repo"]["normalizer"] == "lowercase"

    def test_v1_files_still_store_commit(self):
        git = _load("sourcerer-v1-files")["template"]["mappings"]["properties"]["git"]["properties"]
        assert "commit" in git
