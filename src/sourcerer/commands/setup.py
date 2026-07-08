# Standard packages
import importlib.resources as resources
import json
import pathlib
import sys

# Third-party packages
import click
import requests
import yaml
from dotenv import find_dotenv, load_dotenv

# App packages
from ..utils import make_client

# Resolve `.env` from the working directory, not this package's install location
# (see cli.py). Matters when sourcerer runs as an installed uv tool.
load_dotenv(find_dotenv(usecwd=True))

_ELASTIC = resources.files("sourcerer") / "elastic"
ELASTICSEARCH_INDEX_TEMPLATES_DIR = _ELASTIC / "index_templates"
AGENT_BUILDER_TOOLS_DIR = _ELASTIC / "agent_builder_tools"
AGENT_BUILDER_AGENTS_DIR = _ELASTIC / "agent_builder_agents"
AGENT_BUILDER_SKILLS_DIR = _ELASTIC / "agent_builder_skills"


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
    session: requests.Session, kb_url: str, agents_dir: pathlib.Path = AGENT_BUILDER_AGENTS_DIR
) -> list[str]:
    agents = _load_yaml_dir(agents_dir)
    if not agents:
        raise FileNotFoundError(f"No agent definitions found in {agents_dir}")
    base = kb_url.rstrip("/")
    loaded = []
    for agent in agents:
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


def load_agent_builder_skills(
    session: requests.Session, kb_url: str, skills_dir: pathlib.Path = AGENT_BUILDER_SKILLS_DIR
) -> list[str]:
    skills = _load_yaml_dir(skills_dir)
    if not skills:
        raise FileNotFoundError(f"No skill definitions found in {skills_dir}")
    base = kb_url.rstrip("/")
    loaded = []
    for skill in skills:
        skill_id = skill["id"]
        item_url = f"{base}/api/agent_builder/skills/{skill_id}"
        get_resp = session.get(item_url)
        if get_resp.status_code == 200:
            resp = session.put(item_url, json=_skill_put_body(skill))
        else:
            resp = session.post(f"{base}/api/agent_builder/skills", json=skill)
        resp.raise_for_status()
        loaded.append(skill_id)
    return loaded


def run(url: str, api_key: str | None, username: str | None, password: str | None, kb_url: str | None) -> None:
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

    session = make_kb_session(api_key, username, password)
    try:
        tool_ids = load_agent_builder_tools(session, kb_url)
        for tid in tool_ids:
            click.echo(f"Upserted tool: {tid}")

        skill_ids = load_agent_builder_skills(session, kb_url)
        for sid in skill_ids:
            click.echo(f"Upserted skill: {sid}")

        agent_ids = load_agent_builder_agents(session, kb_url)
        for aid in agent_ids:
            click.echo(f"Upserted agent: {aid}")
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        click.echo(f"Error: Kibana API request failed: {e}\n{body}", err=True)
        sys.exit(1)
