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
KIBANA_SAVED_OBJECTS_DIR = _ELASTIC / "kibana_saved_objects"
SKILLS_DIR = resources.files("sourcerer") / "skills"

def _parse_skillmd(text: str) -> tuple[dict, str]:
    """Parse a SKILL.md into (frontmatter_dict, body). Returns ({}, text) if no frontmatter."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}, text
    frontmatter = yaml.safe_load(text[3:end]) or {}
    body = text[end + 5:]
    return frontmatter, body


def _read_skillmd(skill_id: str) -> tuple[dict, str]:
    """Read skills/<skill_id>/SKILL.md and return (frontmatter_dict, body)."""
    path = SKILLS_DIR / skill_id / "SKILL.md"
    return _parse_skillmd(path.read_text(encoding="utf-8"))


def _read_skill_content(skill_id: str) -> str:
    """Read and return the Markdown body of skills/<skill_id>/SKILL.md, frontmatter stripped."""
    _, body = _read_skillmd(skill_id)
    return body


def build_host_citation_referenced_content(host: Host) -> dict:
    """One referenced_content item for a single git host: its URL templates as a Markdown block.
    Generated at setup time from the resolved host registry and attached to the
    sourcerer-code-citations skill; not a standalone skill and not written to disk."""
    content = (
        f"# sourcerer-code-citations-{host.id}\n\n"
        f"URL templates for citing code hosted on {host.name} (`git.host = \"{host.id}\"`). "
        "Fill the tokens from the `sourcerer.code.*` / `sourcerer.files.*` tool output.\n\n"
        f"- Directory: `{host.urls['directory']}`\n"
        f"- File: `{host.urls['file']}`\n"
        f"- Single line: `{host.urls['line']}`\n"
        f"- Line range: `{host.urls['line_range']}`\n\n"
        "Use the single-line template for a one-line claim and the line-range template for a "
        "multi-line span. Never invent a different URL scheme for this host."
    )
    return {
        "name": f"sourcerer-code-citations-{host.id}",
        "relativePath": f"./sourcerer-code-citations-{host.id}",
        "content": content,
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


def load_yaml_dir(directory: pathlib.Path) -> list[dict]:
    files = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    return [yaml.safe_load(f.read_text()) for f in files]


# Backwards-compatible alias: this loader used to be setup-private (`_load_yaml_dir`). The
# `export` command reuses it, so it now has a public name; keep the old one referenced below.
_load_yaml_dir = load_yaml_dir


def load_skills(skills_dir: pathlib.Path = AGENT_BUILDER_SKILLS_DIR) -> list[dict]:
    """Load Agent Builder skill templates from `skills_dir`, injecting id, name, description,
    and content from the corresponding SKILL.md in SKILLS_DIR. Returns fully-populated dicts."""
    skill_files = sorted(skills_dir.glob("*.yml")) + sorted(skills_dir.glob("*.yaml"))
    if not skill_files:
        raise FileNotFoundError(f"No skill definitions found in {skills_dir}")
    skills = []
    for path in skill_files:
        template = yaml.safe_load(path.read_text()) or {}
        skill_id = path.stem
        fm, body = _read_skillmd(skill_id)
        skills.append({
            **template,
            "id": fm.get("id", skill_id),
            "name": fm.get("name", skill_id),
            "description": fm.get("description", ""),
            "content": body,
        })
    return skills


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
    session: requests.Session, kb_url: str,
    agents_dir: pathlib.Path = AGENT_BUILDER_AGENTS_DIR,
) -> list[str]:
    """Upsert each agent. The agent's skill_ids are defined statically in the YAML and upserted
    as-is; per-host citation data is now embedded as referenced_content on the citations skill
    rather than as separate skills, so no dynamic patching is needed."""
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
    """Upsert the on-disk base skills. For the sourcerer-code-citations skill, attach
    per-host URL templates as referenced_content items (one per auto_skill=True host) so
    the agent can look up the right URL scheme from the skill's embedded reference data.
    Returns the upserted skill ids."""
    skills = load_skills(skills_dir)
    rc_items = [
        build_host_citation_referenced_content(hosts[h])
        for h in sorted(hosts) if hosts[h].auto_skill
    ]
    base = kb_url.rstrip("/")
    loaded = []
    for skill in skills:
        if skill["id"] == "sourcerer-code-citations":
            skill["referenced_content"] = rc_items
        loaded.append(_upsert_skill(session, base, skill))
    return loaded


def load_kibana_saved_objects(
    session: requests.Session, kb_url: str,
    saved_objects_dir: pathlib.Path = KIBANA_SAVED_OBJECTS_DIR,
) -> list[str]:
    """Idempotently import Kibana saved objects (index patterns, dashboards, lenses, tags, etc.)
    from every .ndjson file in `saved_objects_dir`.

    Uses `POST /api/saved_objects/_import?overwrite=true` so repeated runs are safe.
    Per-object failures are returned as (id, error_message) pairs in a separate list;
    the caller is responsible for reporting them.

    Returns (imported_ids, errors) where errors is a list of (object_id, message) tuples."""
    ndjson_files = sorted(saved_objects_dir.glob("*.ndjson"))
    if not ndjson_files:
        raise FileNotFoundError(f"No saved-objects .ndjson files found in {saved_objects_dir}")
    base = kb_url.rstrip("/")
    url = f"{base}/api/saved_objects/_import?overwrite=true"
    imported: list[str] = []
    errors: list[tuple[str, str]] = []
    for path in ndjson_files:
        raw_lines = path.read_bytes().splitlines(keepends=True)
        # The last line of a Kibana export is a metadata summary (exportedCount/missingRefCount),
        # not a saved object; strip it before importing.
        object_lines = [
            line for line in raw_lines
            if not (line.strip().startswith(b"{") and b"exportedCount" in line)
        ]
        ndjson_bytes = b"".join(object_lines)
        # Kibana import requires multipart/form-data. Omitting the key from `headers` is NOT
        # enough to drop it - requests.Session merges session.headers underneath whatever you
        # pass, so a missing key just falls back to the session's Content-Type: application/json.
        # Setting it to None here is what actually removes it from the merged result, letting
        # requests generate the correct multipart/form-data; boundary=... header itself.
        headers = {k: v for k, v in session.headers.items() if k.lower() != "content-type"}
        headers["Content-Type"] = None
        resp = session.post(
            url,
            files={"file": (path.name, ndjson_bytes, "application/ndjson")},
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json()
        for obj in result.get("successResults", []):
            imported.append(obj.get("id", "unknown"))
        for err in result.get("errors", []):
            obj_id = err.get("id", "unknown")
            msg = err.get("error", {}).get("message") or str(err.get("error", err))
            errors.append((obj_id, msg))
        # When overwrite=true and all objects already exist, successResults may be empty but
        # success=true and no errors - treat the whole file as successfully applied.
        if result.get("success") and not result.get("errors"):
            # Count objects from the file as applied even if successResults is empty
            # (Kibana only populates successResults for objects it actually created/updated).
            pass
    return imported, errors


def run(url: str, api_key: str | None, username: str | None, password: str | None,
        kb_url: str | None, config_path: str | None = None) -> None:
    failed = False
    es = make_client(url, api_key, username, password)
    try:
        loaded = load_index_templates(es)
        for name in loaded:
            click.echo(f"Loaded index template: {name}")
    except FileNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True)
        failed = True
    except Exception as e:
        click.echo(f"[ERROR] Failed to load index templates: {e}", err=True)
        failed = True

    if not kb_url:
        click.echo("Skipping Kibana setup (KIBANA_URL not set).")
        sys.exit(1 if failed else 0)

    if not api_key and not (username and password):
        click.echo(
            "[ERROR] Kibana setup requires either --api-key / ELASTICSEARCH_API_KEY "
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
        click.echo(f"[ERROR] Invalid config: {e}", err=True)
        sys.exit(1)

    session = make_kb_session(api_key, username, password)

    try:
        tool_ids = load_agent_builder_tools(session, kb_url)
        for tid in tool_ids:
            click.echo(f"Upserted tool: {tid}")
    except FileNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True)
        failed = True
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        click.echo(f"[ERROR] Failed to upsert tools: {e}\n{body}", err=True)
        failed = True

    try:
        skill_ids = load_agent_builder_skills(session, kb_url, hosts)
        for sid in skill_ids:
            click.echo(f"Upserted skill: {sid}")
    except FileNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True)
        failed = True
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        click.echo(f"[ERROR] Failed to upsert skills: {e}\n{body}", err=True)
        failed = True

    try:
        agent_ids = load_agent_builder_agents(session, kb_url)
        for aid in agent_ids:
            click.echo(f"Upserted agent: {aid}")
    except FileNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True)
        failed = True
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        click.echo(f"[ERROR] Failed to upsert agents: {e}\n{body}", err=True)
        failed = True

    try:
        imported_ids, so_errors = load_kibana_saved_objects(session, kb_url)
        for oid in imported_ids:
            click.echo(f"Imported saved object: {oid}")
        for oid, msg in so_errors:
            click.echo(f"[ERROR] Failed to import saved object {oid!r}: {msg}", err=True)
            failed = True
    except FileNotFoundError as e:
        click.echo(f"[ERROR] {e}", err=True)
        failed = True
    except requests.HTTPError as e:
        body = e.response.text if e.response is not None else ""
        click.echo(f"[ERROR] Failed to import saved objects: {e}\n{body}", err=True)
        failed = True

    if failed:
        sys.exit(1)