"""Structural tests over the packaged Agent Builder YAML assets (tools, skills, agents).

These parse every YAML file and assert the v2 (incremental) contract the setup command ships:
  - every content tool exposes an optional exact `git_ref_key` param,
  - each content tool's ES|QL has MUTUALLY EXCLUSIVE snapshot vs ref-key scoping (so an
    unscoped call never scans both schemas),
  - v2 paths attach the completed commit via `LOOKUP JOIN sourcerer-v2-refs ON git.ref_key`,
  - refs.list surfaces update_mode / status / target commit / ref key across both schemas,
  - the ref-resolution + citations skills explain incremental querying and the mixed-revision
    window.
Executing the ES|QL itself is the job of the live end-to-end verification, not this test.
"""

# Standard packages
import pathlib

# Third-party packages
import pytest
import yaml

_ELASTIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "sourcerer" / "elastic"
_TOOLS_DIR = _ELASTIC / "agent_builder_tools"
_SKILLS_DIR = _ELASTIC / "agent_builder_skills"
_AGENTS_DIR = _ELASTIC / "agent_builder_agents"

CONTENT_TOOLS = [
    "sourcerer.code.search",
    "sourcerer.code.grep",
    "sourcerer.files.cat",
    "sourcerer.files.head",
    "sourcerer.files.tail",
    "sourcerer.files.ls",
]


def _load(directory: pathlib.Path, name: str) -> dict:
    return yaml.safe_load((directory / f"{name}.yml").read_text())


def _tool(name: str) -> dict:
    return _load(_TOOLS_DIR, name)


def _query(name: str) -> str:
    return _tool(name)["configuration"]["query"]


class TestEveryYamlParses:
    def test_all_assets_are_valid_yaml_with_ids(self):
        for directory in (_TOOLS_DIR, _SKILLS_DIR, _AGENTS_DIR):
            for path in sorted(directory.glob("*.yml")):
                doc = yaml.safe_load(path.read_text())
                assert isinstance(doc, dict), path
                assert doc.get("id"), f"{path} missing id"


class TestContentToolsHaveRefKeyParam:
    @pytest.mark.parametrize("name", CONTENT_TOOLS)
    def test_has_optional_git_ref_key_default_empty(self, name):
        params = _tool(name)["configuration"]["params"]
        assert "git_ref_key" in params, f"{name} missing git_ref_key param"
        assert params["git_ref_key"]["optional"] is True
        assert params["git_ref_key"]["defaultValue"] == ""


class TestMutuallyExclusiveScoping:
    @pytest.mark.parametrize("name", CONTENT_TOOLS)
    def test_snapshot_and_refkey_paths_are_exclusive(self, name):
        q = _query(name)
        # The empty-ref-key branch (snapshot / git.commit) and the set-ref-key branch (v2 /
        # git.ref_key) both appear, gated on the param, so exactly one runs per call.
        assert '?git_ref_key == ""' in q
        assert '?git_ref_key != ""' in q
        assert "git.commit LIKE ?git_commit" in q  # v1 scoped by commit
        assert "git.ref_key == ?git_ref_key" in q  # v2 scoped by exact ref key

    @pytest.mark.parametrize("name", CONTENT_TOOLS)
    def test_from_targets_both_schemas(self, name):
        q = _query(name)
        assert "sourcerer-v1-" in q and "sourcerer-v2-" in q


class TestV2LookupJoin:
    @pytest.mark.parametrize("name", CONTENT_TOOLS)
    def test_uses_lookup_join_on_ref_key(self, name):
        assert "LOOKUP JOIN sourcerer-v2-refs ON git.ref_key" in _query(name)

    @pytest.mark.parametrize("name", [
        "sourcerer.code.search", "sourcerer.code.grep",
        "sourcerer.files.cat", "sourcerer.files.head", "sourcerer.files.tail",
    ])
    def test_completed_commit_coalesced_for_output(self, name):
        # v2 rows get git.commit from the refs join; v1 rows fall back to their own commit.
        # This is what puts the completed commit on every returned row for citations, even
        # while an incremental branch is still `status: indexing`.
        q = _query(name)
        assert "COALESCE(git.commit, _commit)" in q
        assert "KEEP git.org, git.repo, git.commit" in q


class TestRefsListSurfacesBothSchemas:
    def test_from_both_refs_indices(self):
        q = _query("sourcerer.refs.list")
        assert "sourcerer-v1-refs" in q and "sourcerer-v2-refs" in q

    def test_keeps_incremental_fields(self):
        q = _query("sourcerer.refs.list")
        for field in ("update_mode", "status", "git.ref_key", "git.target_commit", "git.commit"):
            assert field in q, f"refs.list KEEP is missing {field}"

    def test_null_commit_rows_are_not_filtered_out(self):
        # An incremental branch mid-first-index (git.commit == null) must still be listed;
        # the default wildcard must short-circuit before `git.commit LIKE ?git_commit`.
        q = _query("sourcerer.refs.list")
        assert '?git_commit == "*" OR git.commit LIKE ?git_commit' in q


class TestSkillsDocumentIncremental:
    def test_ref_resolution_mentions_ref_key_and_indexing_window(self):
        text = (_SKILLS_DIR / "sourcerer-ref-resolution.yml").read_text()
        assert "git_ref_key" in text
        assert "incremental" in text
        assert "status: indexing" in text
        assert "mixed" in text.lower()

    def test_citations_mentions_incremental_completed_commit(self):
        text = (_SKILLS_DIR / "sourcerer-code-citations.yml").read_text()
        assert "incremental" in text
        assert "completed commit" in text.lower()
