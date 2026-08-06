"""Unit tests for `sourcerer export`: the tool-id translation from Agent Builder's dotted names
to the MCP endpoint's underscored names, the SKILL.md rendering, and the idempotent file merges
for .mcp.json / claude_desktop_config.json.

The dotted -> underscored translation was verified empirically against a live cluster's MCP
`tools/list` (dots become underscores: `sourcerer.code.grep` -> `sourcerer_code_grep`); these
tests pin that contract so a prompt/skill edit can't silently reintroduce a name the MCP
endpoint won't match.
"""

# Standard packages
import json
import zipfile

# Third-party packages
import pytest

# App packages
from sourcerer.commands.export import command as export
from sourcerer.commands.setup.command import (
    _parse_skillmd,
    _read_skill_content,
    build_host_citation_referenced_content,
)
from sourcerer.hosts import Host


class TestBuildHostCitationReferencedContent:
    def _github_host(self) -> Host:
        return Host(
            id="github",
            name="GitHub",
            urls={
                "clone": "https://github.com/{git.org}/{git.repo}.git",
                "directory": "https://github.com/{git.org}/{git.repo}/tree/{git.commit}/{file.directory}",
                "file": "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}",
                "line": "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number}",
                "line_range": "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}",
            },
        )

    def test_returns_required_fields(self):
        item = build_host_citation_referenced_content(self._github_host())
        assert "name" in item
        assert "relativePath" in item
        assert "content" in item

    def test_name_and_relative_path_use_host_id(self):
        item = build_host_citation_referenced_content(self._github_host())
        assert item["name"] == "sourcerer-code-citations-github"
        assert item["relativePath"] == "./sourcerer-code-citations-github"

    def test_content_contains_url_templates(self):
        item = build_host_citation_referenced_content(self._github_host())
        assert "github.com" in item["content"]
        assert "{git.org}" in item["content"]
        assert "{file.path}" in item["content"]
        assert "{line.number_start}" in item["content"]


class TestSkillContentLoading:
    def test_parse_skillmd_strips_frontmatter(self):
        text = "---\nname: foo\ndescription: bar\n---\n\n# Body\nsome content"
        fm, body = _parse_skillmd(text)
        assert fm == {"name": "foo", "description": "bar"}
        assert "---" not in body
        assert "# Body\nsome content" in body

    def test_parse_skillmd_noop_when_no_frontmatter(self):
        text = "# Body\nsome content"
        fm, body = _parse_skillmd(text)
        assert fm == {}
        assert body == text

    def test_read_skill_content_returns_body_without_frontmatter(self):
        content = _read_skill_content("code-search")
        assert content
        assert "---" not in content.splitlines()[0]

    def test_read_skill_content_all_base_skills(self):
        for skill_dir in ("code-search", "code-citations", "ref-resolution", "repo-discovery"):
            content = _read_skill_content(skill_dir)
            assert content, f"{skill_dir} content is empty"
            assert not content.startswith("---"), f"{skill_dir} content still has frontmatter"


class TestTranslateInstructions:
    def test_qualified_ids_rewritten(self):
        text = "Use `sourcerer.code.grep` and `platform.core.search` then `sourcerer.refs.list`."
        out = export.translate_instructions(text)
        assert "sourcerer_code_grep" in out
        assert "platform_core_search" in out
        assert "sourcerer_refs_list" in out
        assert "sourcerer.code.grep" not in out
        assert "platform.core.search" not in out

    def test_shorthand_forms_rewritten_with_sourcerer_prefix(self):
        text = "the `code.grep` / `files.cat` / `refs.list` tools"
        out = export.translate_instructions(text)
        assert "sourcerer_code_grep" in out
        assert "sourcerer_files_cat" in out
        assert "sourcerer_refs_list" in out

    def test_shorthand_does_not_clip_qualified_id(self):
        # `sourcerer.code.search` must become `sourcerer_code_search`, not
        # `sourcerer.sourcerer_code_search` from the `code.search` shorthand firing mid-id.
        out = export.translate_instructions("`sourcerer.code.search`")
        assert out == "`sourcerer_code_search`"

    def test_wildcard_prose_forms_rewritten(self):
        text = "scope every `sourcerer.code.*` and `sourcerer.files.*` call"
        out = export.translate_instructions(text)
        assert "sourcerer_code_*" in out
        assert "sourcerer_files_*" in out
        assert "sourcerer.code.*" not in out

    def test_field_names_left_alone(self):
        # git.host / git.repo / git.commit are ES field references, not tool ids - untouched.
        text = "each row has `git.host`, `git.org`, `git.repo`, and `git.commit`"
        assert export.translate_instructions(text) == text

    def test_similar_but_distinct_identifier_untouched(self):
        # `code.searcher` is not the `code.search` tool; the trailing-boundary guard protects it.
        text = "the code.searcher helper"
        assert export.translate_instructions(text) == text


class TestTranslateSkillToSkillmd:
    def _skill(self):
        return {
            "id": "sourcerer-code-search",
            "name": "code-search",
            "skill_dir": "code-search",
            "description": "Use when you need to find where something is defined.",
            "content": "# body\nUse `code.grep` to search.",
        }

    def test_frontmatter_and_body(self):
        out = export.translate_skill_to_skillmd(self._skill())
        assert out.startswith("---\n")
        assert 'name: "code-search"' in out
        assert "description:" in out
        # body content is present and tool ids are translated
        assert "sourcerer_code_grep" in out
        assert "code.grep" not in out

    def test_multiline_description_collapsed_to_one_line(self):
        skill = self._skill()
        skill["description"] = "line one\nline two\nline three"
        out = export.translate_skill_to_skillmd(skill)
        desc_line = [ln for ln in out.splitlines() if ln.startswith("description:")][0]
        assert "line one line two line three" in desc_line

    def test_description_capped(self):
        skill = self._skill()
        skill["description"] = "x" * 400
        out = export.translate_skill_to_skillmd(skill, char_cap=200)
        desc_line = [ln for ln in out.splitlines() if ln.startswith("description:")][0]
        # quoted value length is capped (<= cap + quotes + ellipsis), well under the raw 400
        assert len(desc_line) < 220

    def test_description_with_quotes_escaped(self):
        skill = self._skill()
        skill["description"] = 'has a "quote" in it'
        out = export.translate_skill_to_skillmd(skill)
        # the SKILL.md frontmatter must remain parseable: the inner quote is escaped
        assert '\\"quote\\"' in out

    def test_referenced_content_appended_to_body(self):
        skill = self._skill()
        skill["referenced_content"] = [
            {"name": "host-github", "relativePath": "./host-github", "content": "# github-urls\n\nTemplate here."},
            {"name": "host-gitlab", "relativePath": "./host-gitlab", "content": "# gitlab-urls\n\nOther template."},
        ]
        out = export.translate_skill_to_skillmd(skill)
        assert "# github-urls" in out
        assert "Template here." in out
        assert "# gitlab-urls" in out
        assert "Other template." in out
        # body content still present
        assert "sourcerer_code_grep" in out

    def test_no_referenced_content_unchanged(self):
        # Skills without referenced_content produce identical output to before.
        skill = self._skill()
        out_without = export.translate_skill_to_skillmd(skill)
        skill["referenced_content"] = []
        out_empty = export.translate_skill_to_skillmd(skill)
        assert out_without == out_empty


class TestMergeMcpServers:
    def test_creates_file_with_server(self, tmp_path):
        p = tmp_path / ".mcp.json"
        export._merge_mcp_servers(p, "sourcerer", {"type": "http", "url": "u"})
        data = json.loads(p.read_text())
        assert data["mcpServers"]["sourcerer"]["url"] == "u"

    def test_preserves_other_servers_and_keys(self, tmp_path):
        p = tmp_path / ".mcp.json"
        p.write_text(json.dumps({
            "mcpServers": {"other": {"type": "http", "url": "x"}},
            "topLevel": 1,
        }))
        export._merge_mcp_servers(p, "sourcerer", {"type": "http", "url": "u"})
        data = json.loads(p.read_text())
        assert data["mcpServers"]["other"]["url"] == "x"
        assert data["mcpServers"]["sourcerer"]["url"] == "u"
        assert data["topLevel"] == 1

    def test_idempotent_replace(self, tmp_path):
        p = tmp_path / ".mcp.json"
        export._merge_mcp_servers(p, "sourcerer", {"url": "first"})
        export._merge_mcp_servers(p, "sourcerer", {"url": "second"})
        data = json.loads(p.read_text())
        assert data["mcpServers"]["sourcerer"] == {"url": "second"}
        assert list(data["mcpServers"]) == ["sourcerer"]

    def test_malformed_existing_json_is_replaced(self, tmp_path):
        p = tmp_path / ".mcp.json"
        p.write_text("{ not valid json")
        export._merge_mcp_servers(p, "sourcerer", {"url": "u"})
        data = json.loads(p.read_text())
        assert data["mcpServers"]["sourcerer"]["url"] == "u"


class TestAuthVarName:
    def test_api_key_referenced(self):
        assert export._auth_var_name("key", None, None) == "ELASTICSEARCH_API_KEY"

    def test_basic_auth_only_rejected(self):
        assert export._auth_var_name(None, "user", "pass") is None
        assert export._auth_var_name(None, "user", None) is None

    def test_no_creds_defaults_to_api_key_name(self):
        assert export._auth_var_name(None, None, None) == "ELASTICSEARCH_API_KEY"


class TestResolveEndpoint:
    @pytest.mark.parametrize("base", [
        "https://kb.example.com",
        "https://kb.example.com/",
    ])
    def test_trailing_slash_tolerated(self, base):
        assert export._resolve_endpoint(base) == "https://kb.example.com/api/agent_builder/mcp"


class TestRunClaudeDesktop:
    def test_writes_zip_instructions_and_config(self, tmp_path):
        export.run_claude_desktop(
            kb_url="https://kb.example.com",
            config_path=None,
            api_key="s3cr3t-value",
            dest=str(tmp_path),
        )
        zip_path = tmp_path / "sourcerer-skills.zip"
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            # exactly the 4 base skills with canonical unprefixed folder names
            skill_md_names = {n for n in names if n.endswith("/SKILL.md")}
            assert skill_md_names == {
                "code-citations/SKILL.md",
                "code-search/SKILL.md",
                "ref-resolution/SKILL.md",
                "repo-discovery/SKILL.md",
            }
            # code-citations skill body must contain per-host URL templates embedded inline
            # (the default host registry includes github, so github.com must appear)
            citations_md = zf.read("code-citations/SKILL.md").decode()
            assert "github.com" in citations_md, (
                "code-citations/SKILL.md is missing per-host URL templates; "
                "build_host_citation_referenced_content() blocks should be embedded in the body"
            )
        assert (tmp_path / "sourcerer-claude-desktop-instructions.md").exists()
        cfg = json.loads((tmp_path / "claude_desktop_config.json").read_text())
        s = cfg["mcpServers"]["sourcerer"]
        assert s["command"] == "npx"
        assert "mcp-remote" in s["args"]
        assert s["env"]["AUTH_HEADER"] == "ApiKey ${ELASTICSEARCH_API_KEY}"
        # env value blanks - no literal secret written
        assert s["env"]["ELASTICSEARCH_API_KEY"] == ""
        assert "s3cr3t-value" not in (tmp_path / "claude_desktop_config.json").read_text()
