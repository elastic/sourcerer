# Sourcerer

Sourcerer answers questions about your software from the source.

## Quickstart

Make sure you have [uv](https://docs.astral.sh/uv/) and [git](https://git-scm.com/downloads/) on your machine, and [Elasticsearch and Kibana](https://www.elastic.co/cloud/serverless) running somewhere.

1. Install the `sourcerer` CLI:
   ```sh
   uv tool install "git+https://github.com/elastic/sourcerer.git@v2.0.0"
   ```
2. Add connection details. Create a `.env` in your working directory, then fill it in:
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
3. Define the sources to index. Create `sourcerer.yml`, then edit it. Each source requires `git.host`, `git.org`, `git.repo`, and `git.ref_type`, and each `match` is a pattern over the names of remote tags, branches, or commit hashes:
   ```sh
   cat > sourcerer.yml <<'EOF'
   sources:
   # Example: Keep the latest commit from the main branch
   - git: { host: "github", org: "elastic", repo: "docs-content", ref_type: "branch" }
     match: "main"
     retain.count: 1

   # Example: Keep the latest patch release for the last two major releases
   - git: { host: "github", org: "elastic", repo: "elasticsearch", ref_type: "tag" }
     match: "v{major}.{minor}.{patch}"
     since.ref: "v8.17.0"
     retain.version: { majors: 2, patches: 1 }
   EOF
   ```
   The [`sourcerer.yml` specification](specs/sourcerer-yml.md) has the full reference of fields supported by the configuration file.
4. Set up the indices and agent: `sourcerer setup --config sourcerer.yml`
5. Index the repos: `sourcerer index --config sourcerer.yml`
6. Chat about your software with the Sourcerer agent in Kibana under "Agents".

## Upgrades

To upgrade, reinstall from the desired release tag, replacing `v2.0.0` with the release you want:

```sh
uv tool install --reinstall "git+https://github.com/elastic/sourcerer.git@v2.0.0"
```

Git tag installations remain pinned to that release. `uv tool upgrade sourcerer` does not automatically discover a newer GitHub tag.

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
uv run sourcerer index --config sourcerer.yml
```

The project is installed in editable mode, so edits under `src/` take effect immediately -
no reinstall needed. Because the environment is isolated in `./.venv`, this never conflicts
with a globally installed `sourcerer` (e.g. from `uv tool install`).

Equivalently, you can invoke the module directly: `uv run python -m sourcerer.cli <command>`.

### Tests

```sh
uv run pytest tests/
```

### Releases

The version in `pyproject.toml` is the source of truth. Prepare a release through
a normal pull request:

1. Run `uv version <major>.<minor>.<patch>` (without the `v` prefix).
2. Review and commit the resulting `pyproject.toml` and `uv.lock` changes.
3. Merge the pull request to `main` after its tests pass.

From an up-to-date `main` with no tracked changes, publish the corresponding tag:

```sh
./scripts/release.sh v2.0.0
```

The script requires a strict `vMAJOR.MINOR.PATCH` tag, verifies that it matches
`pyproject.toml`, confirms that `main` matches `origin/main` and that the tag is
new, checks the lockfile, runs the tests, and builds the package. After
confirmation, it creates and pushes an annotated tag. The tag-triggered GitHub
Actions workflow repeats the checks and creates the GitHub release only if they
pass.
