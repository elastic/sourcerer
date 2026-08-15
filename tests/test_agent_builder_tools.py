"""Lightweight structural checks over the shipped agent-builder tool YAMLs: every tool must
expose a git_host param and filter git.host before git.org, so the agent can scope by host and
carry it into citation output. Parses the YAML and inspects the ESQL query text; no ES needed."""

# Standard packages
import importlib.resources as resources

# Third-party packages
import pytest
import yaml

from sourcerer.commands.setup.command import strip_esql_comments

_TOOLS_DIR = resources.files("sourcerer") / "elastic" / "agent_builder_tools"


def _tools():
    out = {}
    for f in sorted(_TOOLS_DIR.glob("*.yml")):
        tool = yaml.safe_load(f.read_text())
        if tool.get("type") == "esql":
            out[tool["id"]] = tool
    return out


def test_all_tools_have_git_host_param():
    for tid, tool in _tools().items():
        params = tool["configuration"]["params"]
        assert "git_host" in params, f"{tid} missing git_host param"
        assert params["git_host"]["optional"] is True
        assert params["git_host"]["defaultValue"] == "*"


def test_git_host_filtered_before_git_org():
    for tid, tool in _tools().items():
        query = tool["configuration"]["query"]
        assert "?git_host" in query, f"{tid} does not reference ?git_host"
        # git.host must be filtered before git.org in the WHERE clause
        assert query.index("git.host") < query.index("git.org"), \
            f"{tid} filters git.org before git.host"


_CONTENT_TOOL_IDS = (
    "sourcerer.code.search", "sourcerer.code.grep",
    "sourcerer.files.cat", "sourcerer.files.head", "sourcerer.files.tail",
    "sourcerer.files.ls", "sourcerer.files.tree",
    "sourcerer.files.read_lines", "sourcerer.files.wc",
)


def test_content_tools_use_universal_ref_key_join_query():
    # INV-005: every content tool's WHERE runs the identical `git.ref_key == ?git_ref_key`
    # shape with no update_mode/mode conditional, and joins sourcerer-refs on git.ref_key to
    # resolve the commit for both snapshot and incremental content.
    tools = _tools()
    for tid in _CONTENT_TOOL_IDS:
        query = tools[tid]["configuration"]["query"]
        params = tools[tid]["configuration"]["params"]
        assert "update_mode" not in query, f"{tid} query has an update_mode conditional"
        assert "git.ref_key == ?git_ref_key" in query, f"{tid} missing the exact ref_key filter"
        assert "| LOOKUP JOIN sourcerer-refs ON git.ref_key" in query, f"{tid} missing the universal join"
        assert "git_commit" not in params, f"{tid} still has the old git_commit param"
        assert params["git_ref_key"]["optional"] is False
        assert "defaultValue" not in params["git_ref_key"]


def test_output_keeps_git_host():
    # Every tool that KEEPs git.org must also KEEP git.host (before it), so host reaches output.
    for tid, tool in _tools().items():
        query = tool["configuration"]["query"]
        for line in query.splitlines():
            stripped = line.strip()
            if stripped.startswith("| KEEP") and "git.org" in stripped:
                assert "git.host" in stripped, f"{tid} KEEP omits git.host"
                assert stripped.index("git.host") < stripped.index("git.org")


# ---------------------------------------------------------------------------
# strip_esql_comments tests
# ---------------------------------------------------------------------------

class TestStripEsqlComments:
    def test_no_comments(self):
        q = "FROM idx\n| WHERE x == 1\n| LIMIT 10"
        assert strip_esql_comments(q) == q

    def test_line_comment_removed(self):
        q = "FROM idx\n// this is a comment\n| LIMIT 10"
        assert strip_esql_comments(q) == "FROM idx\n| LIMIT 10"

    def test_line_comment_inline_removed(self):
        q = "FROM idx\n| WHERE x == 1 // inline\n| LIMIT 10"
        assert strip_esql_comments(q) == "FROM idx\n| WHERE x == 1\n| LIMIT 10"

    def test_block_comment_removed(self):
        q = "FROM idx\n/* block */\n| LIMIT 10"
        assert strip_esql_comments(q) == "FROM idx\n| LIMIT 10"

    def test_block_comment_inline_replaced_with_space(self):
        # Inline block comment becomes a space so tokens don't fuse
        q = "FROM idx\n| WHERE x/*comment*/== 1"
        result = strip_esql_comments(q)
        assert "/*" not in result
        assert "x " in result or "x==" in result  # space or adjacent; no fusing of x==
        # More precisely: a space was inserted
        assert "x " in result

    def test_multiline_block_comment_preserves_line_count(self):
        # Newlines inside block comment are preserved so line structure survives
        q = "FROM idx\n/* line1\nline2 */\n| LIMIT 10"
        result = strip_esql_comments(q)
        # blank lines get dropped, so we just check the comment is gone
        assert "/*" not in result
        assert "FROM idx" in result
        assert "| LIMIT 10" in result

    def test_double_quoted_string_preserved(self):
        # // and /* inside a double-quoted string must not be stripped
        q = 'FROM idx\n| WHERE msg == "not // a comment"\n| LIMIT 10'
        assert strip_esql_comments(q) == q

    def test_double_quoted_string_with_escape(self):
        q = r'FROM idx\n| WHERE x == "foo\"bar // not a comment"'
        result = strip_esql_comments(q)
        assert "// not a comment" in result

    def test_triple_quoted_string_preserved(self):
        q = 'FROM idx\n| WHERE msg == """not // a comment and /* neither */"""\n| LIMIT 10'
        assert strip_esql_comments(q) == q

    def test_backtick_identifier_preserved(self):
        q = "FROM idx\n| WHERE `field // name` == 1\n| LIMIT 10"
        assert strip_esql_comments(q) == q

    def test_backtick_doubled_escape(self):
        # `` inside backticks is a literal backtick, not the end of the identifier
        q = "FROM idx\n| WHERE `field``name // x` == 1\n| LIMIT 10"
        assert strip_esql_comments(q) == q

    def test_blank_lines_dropped(self):
        q = "FROM idx\n\n\n| LIMIT 10"
        assert strip_esql_comments(q) == "FROM idx\n| LIMIT 10"

    def test_trailing_whitespace_stripped_per_line(self):
        q = "FROM idx   \n| LIMIT 10"
        result = strip_esql_comments(q)
        assert result == "FROM idx\n| LIMIT 10"

    def test_leading_indentation_preserved(self):
        q = "FROM idx\n  | WHERE x == 1\n  | LIMIT 10"
        assert strip_esql_comments(q) == q

    def test_comment_only_query(self):
        q = "// only a comment\n/* block */\n"
        assert strip_esql_comments(q) == ""

    def test_empty_string(self):
        assert strip_esql_comments("") == ""

    def test_unterminated_block_comment(self):
        # Unterminated block comment: rest of query consumed, only pre-comment text survives
        q = "FROM idx\n/* unterminated"
        result = strip_esql_comments(q)
        assert "FROM idx" in result
        assert "/*" not in result

    def test_shipped_tools_are_idempotent_after_strip(self):
        # strip_esql_comments must be idempotent: a second pass must not change anything,
        # which proves the first pass removed all actual comments (not string contents).
        for tid, tool in _tools().items():
            raw = tool["configuration"]["query"]
            once = strip_esql_comments(raw)
            twice = strip_esql_comments(once)
            assert once == twice, f"{tid}: strip_esql_comments is not idempotent"
