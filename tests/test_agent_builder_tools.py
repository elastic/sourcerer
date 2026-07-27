"""Lightweight structural checks over the shipped agent-builder tool YAMLs: every tool must
expose a git_host param and filter git.host before git.org, so the agent can scope by host and
carry it into citation output. Parses the YAML and inspects the ESQL query text; no ES needed."""

# Standard packages
import importlib.resources as resources

# Third-party packages
import yaml

_TOOLS_DIR = resources.files("sourcerer") / "elastic" / "agent_builder_tools"


def _tools():
    out = {}
    for f in sorted(_TOOLS_DIR.glob("*.yml")):
        tool = yaml.safe_load(f.read_text())
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


def test_output_keeps_git_host():
    # Every tool that KEEPs git.org must also KEEP git.host (before it), so host reaches output.
    for tid, tool in _tools().items():
        query = tool["configuration"]["query"]
        for line in query.splitlines():
            stripped = line.strip()
            if stripped.startswith("| KEEP") and "git.org" in stripped:
                assert "git.host" in stripped, f"{tid} KEEP omits git.host"
                assert stripped.index("git.host") < stripped.index("git.org")
