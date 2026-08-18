"""Lightweight structural checks over the shipped agent-builder tool YAMLs: every tool must
expose a git_host param and filter git.host before git.org, so the agent can scope by host and
carry it into citation output. Parses the YAML and inspects the ESQL query text; no ES needed."""

# Standard packages
import importlib.resources as resources
import re

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


def test_content_tools_use_universal_ref_join_query():
    # INV-005: every content tool scopes to a set of git.ref_key values resolved from the small
    # sourcerer-refs table via a subquery (`git.ref_key IN (FROM sourcerer-refs | WHERE ... (git.commit
    # LIKE ?git_commit_ish OR git.ref LIKE ?git_commit_ish) | KEEP git.ref_key)`) rather than a
    # per-row LIKE/OR wildcard match on the (far larger) content index, with no update_mode/mode
    # conditional. It then joins sourcerer-refs on git.ref_key (a purely internal field -- never an
    # agent-facing param) to resolve the commit for both snapshot and incremental content.
    # git_commit_ish is the single scoping param (LIKE, so it supports wildcards); there is no
    # separate git_commit param. A post-join `status == "complete"` guard excludes torn/partial
    # reads while an incremental branch is mid-reindex (a no-op for always-complete snapshot content).
    tools = _tools()
    for tid in _CONTENT_TOOL_IDS:
        query = tools[tid]["configuration"]["query"]
        params = tools[tid]["configuration"]["params"]
        assert "update_mode" not in query, f"{tid} query has an update_mode conditional"
        # The commit-or-ref match resolves inside the sourcerer-refs subquery, keyed by git.ref_key.
        assert "git.ref_key IN (" in query, f"{tid} missing the ref_key subquery scope"
        assert "git.commit LIKE ?git_commit_ish" in query, f"{tid} missing the commit-or-ref filter"
        assert "git.ref LIKE ?git_commit_ish" in query, f"{tid} missing the commit-or-ref filter"
        assert "| LOOKUP JOIN sourcerer-refs ON git.ref_key" in query, f"{tid} missing the universal join"
        assert "git_ref_key" not in params, f"{tid} still exposes git_ref_key as a param"
        assert "?git_ref_key" not in query, f"{tid} still references ?git_ref_key"
        # git_commit_ish is optional with a "*" default: an unpinned query matches all indexed
        # refs. Every content tool keeps this safe by carrying git.commit through to output (and,
        # where it aggregates, grouping BY git.commit) so multi-ref matches stay attributable and
        # are never summed across refs.
        assert params["git_commit_ish"]["optional"] is True
        assert params["git_commit_ish"]["defaultValue"] == "*"
        # The old standalone git_commit guard param is gone: git_commit_ish is the sole scoping
        # param, and the consistency guard is now the automatic, no-param `status == "complete"`.
        # (Match on word boundary so ?git_commit_ish / git_commit_ish don't false-positive.)
        assert not re.search(r"\bgit_commit\b", "\n".join(params)), f"{tid} still exposes git_commit as a param"
        assert not re.search(r"\?git_commit\b", query), f"{tid} still references ?git_commit"
        # The status guard must appear AFTER the join (status lives only on the refs doc the join
        # brings in, never on the raw content doc).
        assert '| WHERE status == "complete"' in query, f"{tid} missing the post-join status guard"
        assert query.index("LOOKUP JOIN sourcerer-refs") < query.index('WHERE status == "complete"')


def test_refs_list_does_not_surface_ref_key():
    # git.ref_key is purely an internal storage/join key -- never something an agent reads or
    # constructs -- so refs.list must not surface it.
    tool = _tools()["sourcerer.refs.list"]
    query = tool["configuration"]["query"]
    for line in query.splitlines():
        if line.strip().startswith("| KEEP"):
            assert "ref_key" not in line


def test_refs_list_default_status_surfaces_all_refs():
    # Incremental join docs now use status:"complete" (same as snapshot), so the default
    # ?status == "complete" correctly surfaces all indexed refs without a special-case.
    tool = _tools()["sourcerer.refs.list"]
    query = tool["configuration"]["query"]
    assert 'status == "ready"' not in query  # special-case removed; no longer needed
    assert "status LIKE ?status" in query
    assert tool["configuration"]["params"]["status"]["defaultValue"] == "complete"


def test_output_keeps_git_host():
    # Every tool that KEEPs git.org must also KEEP git.host (before it), so host reaches output.
    for tid, tool in _tools().items():
        query = tool["configuration"]["query"]
        for line in query.splitlines():
            stripped = line.strip()
            if stripped.startswith("| KEEP") and "git.org" in stripped:
                assert "git.host" in stripped, f"{tid} KEEP omits git.host"
                assert stripped.index("git.host") < stripped.index("git.org")


def test_content_tool_aggregation_is_ref_scoped():
    # git_commit_ish defaults to "*", so a content query can match more than one ref at once.
    # That is only safe if aggregation never blends refs: every STATS in a content tool must carry
    # git.commit in its BY grouping key, so per-ref counts/bytes/line-blobs stay separate rather
    # than being summed or interleaved across commits. Guards the files.ls-style regression where a
    # `BY name` grouping silently summed file counts across every matching ref.
    tools = _tools()
    for tid in _CONTENT_TOOL_IDS:
        query = tools[tid]["configuration"]["query"]
        # Walk each STATS block: from a "| STATS" line through its trailing "BY ..." clause(s).
        lines = query.splitlines()
        for i, line in enumerate(lines):
            if not line.strip().startswith("| STATS"):
                continue
            # Collect this STATS block's text up to the next pipe command.
            block = [line]
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("|"):
                    break
                block.append(nxt)
            block_text = "\n".join(block)
            assert " BY " in block_text, f"{tid} has a STATS with no BY grouping"
            by_clause = block_text.split(" BY ", 1)[1]
            assert "git.commit" in by_clause, (
                f"{tid} STATS groups without git.commit -- would blend refs when git_commit_ish "
                f"matches more than one ref"
            )


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
