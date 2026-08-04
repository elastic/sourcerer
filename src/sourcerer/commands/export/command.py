"""`sourcerer export` - package sourcerer's Agent Builder system prompt + skills for local
agent harnesses (Claude Code, Claude Desktop).

`sourcerer setup` pushes the system prompt, skills, and ES|QL tools into Kibana as Agent Builder
objects. Elastic 9.2+/Serverless then re-exposes those same tools through a native MCP endpoint
at ``{KIBANA_URL}/api/agent_builder/mcp`` (streamable HTTP, ``Authorization: ApiKey ...``). So
`export` never builds its own MCP server: it (a) translates the static system prompt + skill
content already on disk into each target's format, and (b) writes the connection config that
points the target's MCP client at Kibana's existing endpoint. This mirrors `setup`'s shape (read
``elastic/agent_builder_*`` YAML, resolve ``hosts:``) but writes to the local filesystem instead
of pushing to a remote API - export makes no HTTP calls at all.
"""

# Standard packages
import json
import pathlib
import re
import sys
import zipfile

# Third-party packages
import click

# App packages
from ..setup.command import (
    AGENT_BUILDER_AGENTS_DIR,
    load_skills,
    load_yaml_dir,
)

# The Agent Builder MCP endpoint renames every tool by replacing dots with underscores
# (`sourcerer.code.grep` -> `sourcerer_code_grep`, `platform.core.search` -> `platform_core_search`).
# The static system prompt and skills refer to tools by their dotted Agent Builder ids, so both
# must be rewritten to the MCP names before an MCP-connected harness will match them. Verified
# empirically against a live cluster's `tools/list` response.
MCP_SERVER_NAME = "sourcerer"
MCP_ENDPOINT_PATH = "/api/agent_builder/mcp"

# Fully-qualified Agent Builder tool ids referenced anywhere in the prompt/skills. Longest forms
# are substituted before their bare shorthands (below) so `sourcerer.code.search` is rewritten
# whole rather than mangled via the `code.search` shorthand rule.
_QUALIFIED_TOOL_IDS = [
    "sourcerer.code.grep",
    "sourcerer.code.search",
    "sourcerer.files.cat",
    "sourcerer.files.head",
    "sourcerer.files.ls",
    "sourcerer.files.read_lines",
    "sourcerer.files.tail",
    "sourcerer.refs.list",
    "platform.core.search",
    "platform.core.list_indices",
    "platform.core.get_index_mapping",
    "platform.core.get_document_by_id",
]

# Bare shorthands the skills use in prose (`code.grep`, `files.cat`, `refs.list`). Under MCP
# these resolve to the `sourcerer_`-prefixed names. Applied only after every qualified form has
# already been rewritten, so a shorthand can never clip a longer id.
_SHORTHAND_TOOL_IDS = [
    "code.grep",
    "code.search",
    "files.cat",
    "files.head",
    "files.ls",
    "files.read_lines",
    "files.tail",
    "refs.list",
]


def _mcp_tool_name(dotted_id: str, *, shorthand: bool = False) -> str:
    """`sourcerer.code.grep` -> `sourcerer_code_grep`; shorthand `code.grep` -> `sourcerer_code_grep`."""
    prefixed = f"sourcerer.{dotted_id}" if shorthand else dotted_id
    return prefixed.replace(".", "_")


# Wildcard prose forms ("every `sourcerer.code.*` and `sourcerer.files.*` call"). These aren't
# tool ids, but their dotted spelling reads as the old naming; rewrite the group prefix so the
# text is consistent with the underscored MCP tool names. `sourcerer.code.*` -> `sourcerer_code_*`.
_WILDCARD_PREFIXES = [
    "sourcerer.code.",
    "sourcerer.files.",
    "sourcerer.refs.",
    "platform.core.",
]


def translate_instructions(text: str) -> str:
    """Rewrite every dotted Agent Builder tool id in `text` to the underscored name the MCP
    endpoint exposes. Word-boundary anchored so substrings inside longer identifiers are left
    alone, and qualified ids are handled before their bare shorthands."""
    for prefix in _WILDCARD_PREFIXES:
        # Only the `<prefix>*` wildcard form, e.g. `sourcerer.code.*`. A concrete id ending in a
        # real name is handled by the exact-id pass below, so anchor on the trailing `*`.
        pattern = re.compile(re.escape(prefix) + r"(?=\*)")
        text = pattern.sub(prefix.replace(".", "_"), text)
    for dotted in _QUALIFIED_TOOL_IDS:
        pattern = re.compile(r"(?<![\w.])" + re.escape(dotted) + r"(?![\w])")
        text = pattern.sub(_mcp_tool_name(dotted), text)
    for shorthand in _SHORTHAND_TOOL_IDS:
        # `(?<![\w.])` keeps us from matching the tail of an already-rewritten qualified id or a
        # dotted path; the trailing `(?![\w.])` avoids `code.search` inside `code.searcher`.
        pattern = re.compile(r"(?<![\w.])" + re.escape(shorthand) + r"(?![\w.])")
        text = pattern.sub(_mcp_tool_name(shorthand, shorthand=True), text)
    return text


def load_agent_and_skills() -> tuple[str, list[dict]]:
    """Load the inputs both targets translate from: the agent's system-prompt instructions
    and the base skills. Per-host citation URL templates are referenced_content on the
    citations skill in Agent Builder (setup's job); local plugin targets will add them
    when that target is implemented. Returns (instructions, skills)."""
    agents = load_yaml_dir(AGENT_BUILDER_AGENTS_DIR)
    if not agents:
        raise FileNotFoundError(f"No agent definitions found in {AGENT_BUILDER_AGENTS_DIR}")
    instructions = agents[0].get("configuration", {}).get("instructions", "")
    if not instructions:
        raise ValueError("agent definition has no configuration.instructions")

    skills = load_skills()
    return instructions, skills


def _yaml_scalar(value: str) -> str:
    """Emit a value safe for a SKILL.md frontmatter line. Skill descriptions can be multi-line;
    collapse to a single line and quote so the frontmatter stays valid YAML without pulling in a
    YAML emitter (the only two keys we write are `name` and `description`)."""
    one_line = " ".join(value.split())
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'


def translate_skill_to_skillmd(skill: dict, char_cap: int | None = None) -> str:
    """One skill dict -> a Claude SKILL.md document (YAML frontmatter + Markdown body).
    `char_cap`, when set, trims the frontmatter `description` to that many characters
    (Desktop caps it at 200; Code's budget is generous enough to pass None). The body is the
    skill's `content` verbatim, with tool ids rewritten to their MCP names.

    The ``name:`` field is taken from ``skill["skill_dir"]``, the canonical unprefixed form
    (e.g. ``code-search``). Skills are stored without the ``sourcerer-`` prefix so they work
    correctly in any harness without translation. ``skill["name"]`` carries the Agent Builder
    id (``sourcerer-code-search``) which is only meaningful to that one target."""
    name = skill.get("skill_dir", skill["name"])
    description = " ".join(skill.get("description", "").split())
    if char_cap is not None and len(description) > char_cap:
        description = description[: char_cap - 1].rstrip() + "…"
    body = translate_instructions(skill.get("content", ""))
    frontmatter = (
        "---\n"
        f"name: {_yaml_scalar(name)}\n"
        f"description: {_yaml_scalar(description)}\n"
        "---\n"
    )
    return frontmatter + "\n" + body.rstrip() + "\n"


# ---------------------------------------------------------------------------
# JSON / CLAUDE.md merge helpers (idempotent, re-run-safe)
# ---------------------------------------------------------------------------

CLAUDE_MD_START = "<!-- sourcerer:export:start -->"
CLAUDE_MD_END = "<!-- sourcerer:export:end -->"


def _merge_mcp_servers(path: pathlib.Path, server_name: str, server_entry: dict) -> None:
    """Set only `mcpServers.<server_name>` in the JSON at `path`, preserving every other key and
    server. Creates the file (and parents) if missing. Pretty-prints the result."""
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = data["mcpServers"] = {}
    servers[server_name] = server_entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _merge_claude_md(path: pathlib.Path, section_body: str) -> None:
    """Replace the delimited sourcerer block in `path` (or append one) with `section_body`,
    leaving all content outside the markers untouched. Creates the file if missing."""
    block = f"{CLAUDE_MD_START}\n{section_body.rstrip()}\n{CLAUDE_MD_END}"
    existing = path.read_text() if path.exists() else ""
    if CLAUDE_MD_START in existing and CLAUDE_MD_END in existing:
        pattern = re.compile(
            re.escape(CLAUDE_MD_START) + r".*?" + re.escape(CLAUDE_MD_END),
            re.DOTALL,
        )
        merged = pattern.sub(lambda _m: block, existing, count=1)
    elif existing.strip():
        merged = existing.rstrip() + "\n\n" + block + "\n"
    else:
        merged = block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(merged)


def _resolve_endpoint(kb_url: str) -> str:
    """`{KIBANA_URL}/api/agent_builder/mcp`, tolerant of a trailing slash on the base url. The
    literal `${KIBANA_URL}` placeholder is preserved for targets that expand env vars."""
    return kb_url.rstrip("/") + MCP_ENDPOINT_PATH


def _auth_var_name(api_key: str | None, username: str | None, password: str | None) -> str | None:
    """Which env var the generated config should reference for the ApiKey header, and a guard for
    the basic-auth case. Returns the var name to reference, or None if only username/password was
    supplied (the MCP endpoint accepts API key or OAuth only, not basic auth)."""
    if api_key:
        return "ELASTICSEARCH_API_KEY"
    if username or password:
        return None
    # Nothing loaded: still reference ELASTICSEARCH_API_KEY by name; the user fills it in later.
    return "ELASTICSEARCH_API_KEY"


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


def run_claude_code(
    kb_url: str | None,
    config_path: str | None,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    dest: str | None = None,
) -> None:
    """Write `.claude/skills/*/SKILL.md`, `.mcp.json`, and a delimited `CLAUDE.md` block into
    `dest` (default: cwd). Claude Code auto-discovers all three by filesystem convention, so
    there is no install step."""
    if not kb_url:
        click.echo(
            "[ERROR] export requires a Kibana URL (--kb-url or KIBANA_URL): it is the MCP "
            "endpoint the generated config points at.",
            err=True,
        )
        sys.exit(1)

    auth_var = _auth_var_name(api_key, username, password)
    if auth_var is None:
        click.echo(
            "[ERROR] The Agent Builder MCP endpoint accepts an API key (or, on Serverless, "
            "OAuth 2.1) - not basic auth. Set ELASTICSEARCH_API_KEY instead of "
            "ELASTICSEARCH_USERNAME / ELASTICSEARCH_PASSWORD.",
            err=True,
        )
        sys.exit(1)

    try:
        instructions, skills = load_agent_and_skills()
    except (OSError, ValueError) as e:
        click.echo(f"[ERROR] {e}", err=True)
        sys.exit(1)

    root = pathlib.Path(dest) if dest else pathlib.Path.cwd()
    skills_root = root / ".claude" / "skills"

    written_skills = []
    for skill in skills:
        skill_dir = skills_root / skill["skill_dir"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        # Claude Code shares a 1,536-char budget across description + when-to-use; these skills'
        # descriptions fit comfortably, so no per-target cap is applied.
        (skill_dir / "SKILL.md").write_text(translate_skill_to_skillmd(skill, char_cap=None))
        written_skills.append(skill_dir)

    endpoint = _resolve_endpoint(kb_url)
    server_entry = {
        "type": "http",
        "url": endpoint,
        # Claude Code expands ${VAR} in url/headers at connect time, so no literal secret is ever
        # written - the file is safe to commit.
        "headers": {"Authorization": f"ApiKey ${{{auth_var}}}"},
    }
    mcp_path = root / ".mcp.json"
    _merge_mcp_servers(mcp_path, MCP_SERVER_NAME, server_entry)

    section_body = (
        "## Sourcerer\n\n"
        f"The `{MCP_SERVER_NAME}` MCP server exposes Elastic Agent Builder tools for answering "
        "sourced, cited questions about indexed source code. Treat its tools "
        "(`sourcerer_code_search`, `sourcerer_code_grep`, `sourcerer_files_cat`, "
        "`sourcerer_refs_list`, ...) as authoritative for any question about the indexed repos, "
        "and follow the skills under `.claude/skills/` for how to search, resolve refs, and "
        "cite. The instructions below are the sourcerer agent's operating guide.\n\n"
        + translate_instructions(instructions)
    )
    claude_md_path = root / "CLAUDE.md"
    _merge_claude_md(claude_md_path, section_body)

    # ---- report ----
    click.echo(f"Wrote {len(written_skills)} skill(s) under {skills_root}")
    for d in written_skills:
        click.echo(f"  {d.relative_to(root)}/SKILL.md")
    click.echo(f"Merged MCP server '{MCP_SERVER_NAME}' into {mcp_path}")
    click.echo(f"Merged sourcerer section into {claude_md_path}")
    click.echo("")
    click.echo("Next steps:")
    click.echo(
        f"  1. Export KIBANA_URL and {auth_var} in the shell Claude Code runs in (or a .env it "
        "inherits) - .mcp.json references them by name, it never stores the values."
    )
    click.echo(f"       export KIBANA_URL={kb_url.rstrip('/')!r}")
    click.echo(f"       export {auth_var}=<your Elasticsearch API key>")
    click.echo(
        "  2. Open this directory in Claude Code and run `/mcp` (or `claude mcp list`) to "
        f"confirm the '{MCP_SERVER_NAME}' server connected and lists its tools."
    )


# ---------------------------------------------------------------------------
# Claude Desktop
# ---------------------------------------------------------------------------

# Desktop's Skills uploader caps `name` at 64 and `description` at 200 chars.
DESKTOP_SKILL_NAME_CAP = 64
DESKTOP_SKILL_DESC_CAP = 200

DESKTOP_CONFIG_PATHS = {
    "macOS": "~/Library/Application Support/Claude/claude_desktop_config.json",
    "Windows": "%APPDATA%\\Claude\\claude_desktop_config.json",
    "Linux": "~/.config/Claude/claude_desktop_config.json",
}


def run_claude_desktop(
    kb_url: str | None,
    config_path: str | None,
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    dest: str | None = None,
) -> None:
    """Emit Desktop assets into `dest` (default: cwd): a skills ZIP, an instructions Markdown
    file, and a merge-ready `claude_desktop_config.json`. Desktop has no filesystem/API injection
    point for skills or Project instructions, so those two steps are unavoidably manual - the
    printed output says so plainly."""
    if not kb_url:
        click.echo(
            "[ERROR] export requires a Kibana URL (--kb-url or KIBANA_URL): it is the MCP "
            "endpoint the generated config points at.",
            err=True,
        )
        sys.exit(1)

    auth_var = _auth_var_name(api_key, username, password)
    if auth_var is None:
        click.echo(
            "[ERROR] The Agent Builder MCP endpoint accepts an API key (or, on Serverless, "
            "OAuth 2.1) - not basic auth. Set ELASTICSEARCH_API_KEY instead of "
            "ELASTICSEARCH_USERNAME / ELASTICSEARCH_PASSWORD.",
            err=True,
        )
        sys.exit(1)

    try:
        instructions, skills = load_agent_and_skills()
    except (OSError, ValueError) as e:
        click.echo(f"[ERROR] {e}", err=True)
        sys.exit(1)

    root = pathlib.Path(dest) if dest else pathlib.Path.cwd()
    root.mkdir(parents=True, exist_ok=True)

    # ---- skills zip (one folder per skill; Desktop uploads one skill folder at a time) ----
    zip_path = root / "sourcerer-skills.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for skill in skills:
            # Desktop's name cap is tighter than any of our skill ids, but trim defensively.
            folder = skill["skill_dir"][:DESKTOP_SKILL_NAME_CAP]
            skillmd = translate_skill_to_skillmd(skill, char_cap=DESKTOP_SKILL_DESC_CAP)
            zf.writestr(f"{folder}/SKILL.md", skillmd)

    # ---- instructions markdown (paste into the Project's custom instructions) ----
    instructions_path = root / "sourcerer-claude-desktop-instructions.md"
    instructions_path.write_text(translate_instructions(instructions).rstrip() + "\n")

    # ---- claude_desktop_config.json (merge, never clobber) ----
    endpoint = _resolve_endpoint(kb_url)
    # Desktop's config has no ${VAR} expansion, so we bridge to the remote HTTP endpoint via
    # `mcp-remote`, which reads the header value from its own `env` block. Per the project's
    # security choice we leave the env values blank rather than baking secrets in - the user
    # fills in the two names below.
    server_entry = {
        "command": "npx",
        "args": [
            "mcp-remote",
            "${KIBANA_URL}" + MCP_ENDPOINT_PATH,
            "--header",
            "Authorization:${AUTH_HEADER}",
        ],
        "env": {
            "KIBANA_URL": "",
            "AUTH_HEADER": f"ApiKey ${{{auth_var}}}",
            auth_var: "",
        },
    }
    config_out = root / "claude_desktop_config.json"
    _merge_mcp_servers(config_out, MCP_SERVER_NAME, server_entry)

    # ---- report ----
    click.echo(f"Wrote skills bundle: {zip_path}")
    click.echo(f"Wrote system-prompt instructions: {instructions_path}")
    click.echo(f"Wrote merge-ready MCP config: {config_out}")
    click.echo("")
    click.echo("Claude Desktop has no filesystem/API injection for skills or Project")
    click.echo("instructions, so two steps below are unavoidably manual:")
    click.echo("")
    click.echo("  1. Skills (manual): Settings -> Customize -> Skills, and upload each skill")
    click.echo(f"     folder inside {zip_path.name} (Desktop takes one skill folder at a time).")
    click.echo(
        f"  2. Instructions (manual): paste {instructions_path.name} into your Project's "
        "custom instructions."
    )
    click.echo("  3. MCP config: merge the `sourcerer` entry from")
    click.echo(f"     {config_out.name} into your Claude Desktop config, located at:")
    for os_name, p in DESKTOP_CONFIG_PATHS.items():
        click.echo(f"       {os_name}: {p}")
    click.echo(
        f"     Then set KIBANA_URL and {auth_var} in that file's `env` block (left blank so no "
        "secret is written here), and restart Desktop."
    )
    click.echo("     Requires Node >= 18 so `npx mcp-remote` is available.")
