# Sourcerer

Sourcerer answers questions about your software from the source.

## Quickstart

Make sure you have [uv](https://docs.astral.sh/uv/) and [git](https://git-scm.com/downloads/) on your machine, and [Elasticsearch and Kibana](https://www.elastic.co/cloud/serverless) running somewhere.

1. Install the `sourcerer` CLI:
   ```sh
   uv tool install git+https://github.com/elastic/sourcerer
   ```
2. Add connection details — create a `.env` in your working directory, then fill it in:
   ```sh
   cat > .env <<'EOF'
   ELASTICSEARCH_URL=
   KIBANA_URL=

   # Authentication (use either the API Key or both Username and Password)
   ELASTICSEARCH_API_KEY=
   #ELASTICSEARCH_USERNAME=
   #ELASTICSEARCH_PASSWORD=
   EOF
   ```
3. Choose repos to index — create a `repos.yml`, then edit it (globs match remote branch/tag names):
   ```sh
   cat > repos.yml <<'EOF'
   - org: elastic
     repo: docs-content
     branches: [ "main" ]
     tags: []
   - org: elastic
     repo: elasticsearch
     branches: []
     tags: [ "v9.*" ]
   - org: elastic
     repo: kibana
     branches: []
     tags: [ "v9.*" ]
   EOF
   ```
4. Set up the indices and agent: `sourcerer setup`
5. Index the repos: `sourcerer index --config repos.yml`
6. Chat about your software with the Sourcerer agent in Kibana under "Agents".

Upgrading the `sourcerer` CLI:

```sh
uv tool upgrade sourcerer --reinstall
```


## How it works

The `sourcerer` CLI indexes the files of remote git repositories so that AI agents
can generate authoritative responses to questions about the software and
provide inline citations for verification.

Sourcerer itself a configuration for Elastic Agent Builder, which lets you ask
questions about your software using an agent that analyzes the code.

Its value shines for questions that span multiple repositories or multiple
versions of software.

## Philosophy

**Code is the primary source of truth for its own behavior.** Always authoriative,
never outdated. While documentation and tribal knowledge offers context, they
can never be the primary source of truth for its implementation.

**Go with the grain on how model are trained.** LLMs used by coding agents are
trained to use terminal commands, and [grep has worked exceptionally well](https://arxiv.org/abs/2605.15184). Sourcerer searches code with the same semantics (e.g. grep, ls, cat, head, tail)
over multiple code repositories indexed in Elasticsearch for greater speed,
scale, relevance, security, collaboration, and customization.

## Development

To run the CLI from a local checkout without installing it globally, use `uv run` from
the repo root. uv reads `pyproject.toml`, provisions a matching Python, and syncs the
dependencies into an isolated `./.venv` (gitignored) on first run:

```sh
uv run sourcerer help
uv run sourcerer setup
uv run sourcerer index elastic/elasticsearch -b main
uv run sourcerer index --config repos.yml
```

The project is installed in editable mode, so edits under `src/` take effect immediately —
no reinstall needed. Because the environment is isolated in `./.venv`, this never conflicts
with a globally installed `sourcerer` (e.g. from `uv tool install`).

Equivalently, you can invoke the module directly: `uv run python -m sourcerer.cli <command>`.
