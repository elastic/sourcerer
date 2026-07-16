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
3. Choose repos to index — create a `repos.yml`, then edit it (each `match` is a glob over remote branch/tag names):
   ```sh
   cat > repos.yml <<'EOF'
   # https://github.com/elastic/docs-content
   - org: elastic
     repo: docs-content
     refs:
     - type: branch
       match: main
       retain:
         count: 1  # Keep only the latest commit from the main branch

   # https://github.com/elastic/elasticsearch
   - org: elastic
     repo: elasticsearch
     refs:
     - type: tag
       match:
         - v{major}.{minor}.{patch}
         - v{major}.{minor}.{patch}-{prerelease}
       since:
         ref: v8.17.0  # Index everything from this point on
       retain:
         version:
           majors: 2   # Keep only the latest 2 major release tags
           patches: 1  # Keep only the most recent patch release tag for any minor release tag
         prerelease: superseded  # Keep release candidate tags until their major tags are released
   EOF
   ```
   See `repos.example.yml` (or [Indexing multiple repos with a config](AGENTS.md#indexing-multiple-repos-with-a-config)) for version-aware matching, `since` floors, and `retain` policies.
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

## Benchmarks

Sourcerer includes a benchmark harness that measure how well the agent locates the right
source code for a task. The current benchmark, `swe_explore_bench`
([SWE-Explore-Bench](https://github.com/Qiushao-E/SWE-Explore-Bench)), scores
whether, given an issue, the agent cites the correct file regions.

List what's available:

```sh
sourcerer benchmark list
```

Run a benchmark in three steps (from a directory with a `.env`, as in the
Quickstart):

1. Download and build the dataset into `./benchmarks/<name>/` (needs `git`, `uv`,
   and network access to GitHub + HuggingFace):
   ```sh
   sourcerer benchmark get swe_explore_bench
   ```
2. Index the benchmark's base commits into Elasticsearch (uses the benchmark's
   packaged `repos.yml`):
   ```sh
   sourcerer benchmark index swe_explore_bench
   ```
3. Run the eval against the Sourcerer agent (needs `KIBANA_URL` and
   `ELASTICSEARCH_API_KEY`):
   ```sh
   sourcerer benchmark run swe_explore_bench
   ```

Useful `run` options: `-k/--top-k` (comma-separated cutoffs, e.g. `-k 5,10,20`;
default `5`), `-j/--concurrency` (instances explored in parallel; default `1`),
`--connector-id` (pick the Agent Builder LLM connector), and `--resume` (skip
instances already completed). `run` will download the dataset automatically if
step 1 was skipped.

### Results and traces

Each run writes to a timestamped directory:

```
./benchmarks/<name>/results/sourcerer-<YYYYMMDDHHMMSS>/
├── top5.jsonl      # one file per --top-k value: per-instance regions + metrics
└── traces.jsonl    # full request/response trace for every instance
```

Aggregate metric averages for each `top_k` also print to the console when the run
finishes.


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

### Tests

```sh
uv run pytest tests/
```
