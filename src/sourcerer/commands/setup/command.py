# Standard packages
import importlib.resources as resources
import json
import pathlib
import sys

# Third-party packages
import click
import requests
import yaml
from elasticsearch import NotFoundError

# App packages
from ...config import load_config
from ...hosts import Host, resolve_hosts
from ...utils import make_client

_ELASTIC = resources.files("sourcerer") / "elastic"
ELASTICSEARCH_INDEX_TEMPLATES_DIR = _ELASTIC / "index_templates"
AGENT_BUILDER_TOOLS_DIR = _ELASTIC / "agent_builder_tools"
AGENT_BUILDER_AGENTS_DIR = _ELASTIC / "agent_builder_agents"
AGENT_BUILDER_SKILLS_DIR = _ELASTIC / "agent_builder_skills"

# The id/name of the generated per-host citation skills. The base citations skill tells the
# agent to load `sourcerer-code-citations-<git.host>` for a result's host-specific URL scheme.
HOST_CITATION_SKILL_PREFIX = "sourcerer-code-citations-"


def host_citation_skill_id(host_id: str) -> str:
    return f"{HOST_CITATION_SKILL_PREFIX}{host_id}"


def build_host_citation_skill(host: Host) -> dict:
    """One in-memory citation skill for a single git host: its four link templates rendered as a
    small, self-contained URL-templates reference. Generated at setup time from the resolved host
    registry (built-in defaults merged with the config's `hosts:` overrides) and pushed to
    Kibana; not written to disk, so it never goes stale against the registry."""
    content = (
        f"# sourcerer-code-citations-{host.id}\n\n"
        f"URL templates for citing code hosted on {host.name} (`git.host = \"{host.id}\"`). Fill the "
        "tokens from the `sourcerer.code.*` / `sourcerer.files.*` tool output for the row you are "
        "citing.\n\n"
        f"- Directory: `{host.links['directory']}`\n"
        f"- File: `{host.links['file']}`\n"
        f"- Single line: `{host.links['line']}`\n"
        f"- Line range: `{host.links['line_range']}`\n\n"
        "Use the single-line template for a one-line claim and the line-range template for a "
        "multi-line span (its anchor must carry both endpoints). Never invent a different URL "
        "scheme for this host."
    )
    return {
        "id": host_citation_skill_id(host.id),
        "name": f"sourcerer-code-citations-{host.id}",
        "description": (
            f"Host-specific citation URL templates for {host.name} (`git.host = \"{host.id}\"`)"
        ),
        "content": content,
        "tool_ids": [],
        "referenced_content": [],
    }


def make_kb_session(
    api_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "kbn-xsrf": "true",
    })
    if api_key:
        session.headers["Authorization"] = f"ApiKey {api_key}"
    elif username and password:
        session.auth = (username, password)
    return session


def _load_yaml_dir(directory: pathlib.Path) -> list[dict]:
    files = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    return [yaml.safe_load(f.read_text()) for f in files]


def _tool_put_body(tool: dict) -> dict:
    return {k: v for k, v in tool.items() if k not in ("id", "type")}


def _agent_put_body(agent: dict) -> dict:
    return {k: v for k, v in agent.items() if k != "id"}


def _skill_put_body(skill: dict) -> dict:
    return {k: v for k, v in skill.items() if k != "id"}


def load_index_templates(es, templates_dir: pathlib.Path = ELASTICSEARCH_INDEX_TEMPLATES_DIR) -> list[str]:
    template_files = sorted(templates_dir.glob("*.json"))
    if not template_files:
        raise FileNotFoundError(f"No index templates found in {templates_dir}")

    loaded = []
    for path in template_files:
        body = json.loads(path.read_text())
        name = path.stem
        es.indices.put_index_template(
            name=name,
            index_patterns=body.get("index_patterns"),
            template=body.get("template"),
            _meta=body.get("_meta"),
        )
        for alias in body.get("template", {}).get("aliases", {}):
            try:
                es.indices.update_aliases(actions=[{
                    "add": {
                        "alias": alias,
                        "index": ",".join(body["index_patterns"]),
                    },
                }])
            except NotFoundError:
                # Existing installations may not have an index matching this template yet.
                pass
        loaded.append(name)
    return loaded


def load_agent_builder_tools(
    session: requests.Session, kb_url: str, tools_dir: pathlib.Path = AGENT_BUILDER_TOOLS_DIR
) -> list[str]:
    tools = _load_yaml_dir(tools_dir)
    if not tools:
        raise FileNotFoundError(f"No tool definitions found in {tools_dir}")
    base = kb_url.rstrip("/")
    loaded = []
    for tool in tools:
        tool_id = tool["id"]
        item_url = f"{base}/api/agent_builder/tools/{tool_id}"
        get_resp = session.get(item_url)
        if get_resp.status_code == 200:
            resp = session.put(item_url, json=_tool_put_body(tool))
        else:
            resp = session.post(f"{base}/api/agent_builder/tools", json=tool)
        resp.raise_for_status()
        loaded.append(tool_id)
    return loaded


def load_agent_builder_agents(
    session: requests.Session, kb_url: str, host_skill_ids: list[str] | None = None,
    agents_dir: pathlib.Path = AGENT_BUILDER_AGENTS_DIR,
) -> list[str]:
    """Upsert each agent, patching its `skill_ids` to also reference the generated per-host
    citation skills (their ids are dynamic, so they can't be listed statically in the YAML).
    Existing skill ids are preserved; host skill ids are appended without duplicates."""
    agents = _load_yaml_dir(agents_dir)
    if not agents:
        raise FileNotFoundError(f"No agent definitions found in {agents_dir}")
    base = kb_url.rstrip("/")
    extra = list(host_skill_ids or [])
    loaded = []
    for agent in agents:
        if extra:
            cfg = agent.setdefault("configuration", {})
            existing = list(cfg.get("skill_ids", []))
            for sid in extra:
                if sid not in existing:
                    existing.append(sid)
            cfg["skill_ids"] = existing
        agent_id = agent["id"]
        item_url = f"{base}/api/agent_builder/agents/{agent_id}"
        get_resp = session.get(item_url)
        if get_resp.status_code == 200:
            resp = session.put(item_url, json=_agent_put_body(agent))
        else:
            resp = session.post(f"{base}/api/agent_builder/agents", json=agent)
        resp.raise_for_status()
        loaded.append(agent_id)
    return loaded


def _upsert_skill(session: requests.Session, base: str, skill: dict) -> str:
    """Create-or-update one skill dict via the Agent Builder API. Returns the skill id."""
    skill_id = skill["id"]
    item_url = f"{base}/api/agent_builder/skills/{skill_id}"
    get_resp = session.get(item_url)
    if get_resp.status_code == 200:
        resp = session.put(item_url, json=_skill_put_body(skill))  # update
    else:
        resp = session.post(f"{base}/api/agent_builder/skills", json=skill)  # create
    resp.raise_for_status()
    return skill_id


def load_agent_builder_skills(
    session: requests.Session, kb_url: str, hosts: dict[str, Host],
    skills_dir: pathlib.Path = AGENT_BUILDER_SKILLS_DIR,
) -> list[str]:
    """Upsert the on-disk base skills plus one generated citation skill per resolved git host
    whose auto_skill flag is True. Hosts with auto_skill=False are built-in placeholders that
    require a user-supplied hosts: override before their URLs are usable; their citation skills
    are not pushed until the user defines a concrete per-deployment hosts entry.
    Returns every upserted skill id (the host skill ids are needed to patch the agent)."""
    skills = _load_yaml_dir(skills_dir)
    if not skills:
        raise FileNotFoundError(f"No skill definitions found in {skills_dir}")
    base = kb_url.rstrip("/")
    loaded = []
    for skill in skills:
        loaded.append(_upsert_skill(session, base, skill))
    # Generated per-host citation skills (in-memory, not on disk). Only for hosts with
    # auto_skill=True. Deterministic order by host id so setup output and the agent's skill
    # list are stable.
    for host_id in sorted(hosts):
        if hosts[host_id].auto_skill:
            loaded.append(_upsert_skill(session, base, build_host_citation_skill(hosts[host_id])))
    return loaded


def run(url: str, api_key: str | None, username: str | None, password: str | None,
        kb_url: str | None, config_path: str | None = None) -> None:
    es = make_client(url, api_key, username, password)
    try:
        loaded = load_index_templates(es)
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    for name in loaded:
        click.echo(f"Loaded index template: {name}")

    if not kb_url:
        click.echo("Skipping agent builder setup (KIBANA_URL not set).")
        return

    if not api_key and not (username and password):
        click.echo(
            "Error: Kibana setup requires either --api-key / ELASTICSEARCH_API_KEY "
            "or --username + --password (ELASTICSEARCH_USERNAME / ELASTICSEARCH_PASSWORD).",
            err=True,
        )
        sys.exit(1)

    # Resolve the git-host registry: built-in defaults merged with the config's `hosts:` (if a
    # config was given). One citation skill is generated per resolved host.
    try:
        if config_path:
            hosts = load_config(config_path).hosts
        else:
            hosts = resolve_hosts(None)
    except (OSError, ValueError) as e:
        click.echo(f"Error: invalid config: {e}", err=True)
        sys.exit(1)

    session = make_kb_session(api_key, username, password)
    try:
        tool_ids = load_agent_builder_tools(session, kb_url)
        for tid in tool_ids:
            click.echo(f"Upserted tool: {tid}")

        skill_ids = load_agent_builder_skills(session, kb_url, hosts)
        for sid in skill_ids:
            click.echo(f"Upserted skill: {sid}")

        host_skill_ids = [host_citation_skill_id(h) for h in sorted(hosts) if hosts[h].auto_skill]
        agent_ids = load_agent_builder_agents(session, kb_url, host_skill_ids)
        for aid in agent_ids:
            click.echo(f"Upserted agent: {aid}")
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        click.echo(f"Error: Kibana API request failed: {e}\n{body}", err=True)
        sys.exit(1)
