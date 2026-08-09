# Sourcerer

## CLI

Commands:

- `sourcerer setup [--config <file>]` (reads the config's `hosts:` to generate per-host
  citation skills; without `--config`, uses only the built-in host defaults)
- `sourcerer index <org>/<repo> [-b <branch>] [-t <tag>] [-c <commit>]` (single-repo path
  defaults to `git.host` = `github`)
- `sourcerer index --config <file> [--prune] [--dry-run]`
- `sourcerer prune [--config <file>] [--dry-run]` (config-driven retention prune is skipped
  without `--config`; the orphan sweep always runs)
- `sourcerer mcp-proxy [-e <env>]` (run a stdio MCP proxy that forwards to the Kibana
  Agent Builder MCP endpoint; intended to be launched by Claude Desktop via `mcpServers`)
- `sourcerer help`

The `--config` file is `sourcerer.yml` (see `specs/sourcerer-yml.md` and `sourcerer.example.yml`
for the authoritative schema). Two optional top-level sections: `hosts:` (override/extend the
built-in git-host defaults) and `sources:` (what to index).

### Multi-host support (`git.host`)

Content is namespaced by git host as well as org/repo. Each `sources[i].git` block names a single
concrete `host`, `org`, `repo`, and `ref_type` (no wildcards or arrays). Known hosts have built-in
clone + citation URL templates in `src/sourcerer/hosts.py`; `hosts:` in the config overrides or
adds hosts. For providers whose deployments are instance-scoped (AWS CodeCommit, Azure DevOps, GCP
Secure Source Manager), define one `hosts:` entry per deployment with the region/project/instance
baked into the URL templates; `git.org` then holds only the plain org or project name.

### Indexing multiple repos with a config

`sourcerer index --config sourcerer.yml` indexes many repos, branches, and tags in one run. The
config's `sources:` is a YAML list, one entry per (host, org, repo, ref_type). See
`sourcerer.example.yml`.

#### Source fields (per `sources[i]` entry)

| Field | Required | Description |
|-------|----------|-------------|
| `git.host` | yes | Host id (a `git.host` value); a built-in or a `hosts:`-defined id |
| `git.org` | yes | Org name (may include a `+extra` segment for some providers) |
| `git.repo` | yes | Repo name |
| `git.ref_type` | yes | `branch`, `tag`, or `commit` |
| `match` | yes | For `branch`/`tag`: pattern string or list of patterns matched against ref names (version DSL + glob), a ref matches if any pattern hits. For `commit`: a commit SHA/prefix string or list of them (see below). |
| `since` | no | Index-side inclusion floor: the earliest commit to start indexing from. See below. Not valid for `git.ref_type: commit`. |
| `retain` | no | Retention policy (see below). Omit to keep forever. For `git.ref_type: commit`, only `age` is valid. |

#### `git.ref_type: commit` (pinning an explicit commit)

Pins one or more commits directly, rather than matching named refs. `match` entries are
7-40 hex chars - a full 40-char SHA, or a shorter prefix (git's own "short hash" convention;
a `git.commit` lookup uses a prefix match against the resolved full SHA). There's nothing to
index "from" for a single pinned point, so `since` is rejected; likewise `retain.count`,
`retain.version`, and `retain.prerelease` have no meaning for one commit and are rejected --
only `retain.age` (or omitting `retain` to keep forever) is allowed. A pinned commit must be
reachable from some fetched branch or tag (the clone only contains commits reachable that
way) -- one that's been force-pushed away or only exists on an unfetched ref will fail to
check out, reported as a per-unit error.

```yaml
- git:
    host: github
    org: elastic
    repo: elasticsearch
    ref_type: commit
  match:
  - cfefb3b              # short prefix (>= 7 hex chars) or a full 40-char SHA
  retain:
    age: 2y               # only 'age' is valid for commit sources (or omit = keep forever)
```

#### `since` (inclusion floor)

Sets where indexing starts. Provide **exactly one** of:

| Field | Description |
|-------|-------------|
| `age` | Commit within this age of now (e.g. `1y`). Starting point is the oldest matching commit. |
| `date` | Commit on/after this `YYYY-MM-DD` date. |
| `commit` | Start from this commit hash. |
| `ref` | Start from the commit this tag/branch points to (accepts the full ref name or a bare version). |

#### Pattern syntax

Patterns combine a version DSL with glob syntax:

- **Version placeholders**: `{major}`, `{minor}`, `{patch}`, `{build}` (numeric) plus
  `{prerelease}` - each numeric placeholder matches one numeric segment, enabling the
  version-aware `since` floor and the `retain.version` policy. Example:
  `"v{major}.{minor}.{patch}"` matches `v8.14.3`; add `"v{major}.{minor}.{patch}-{prerelease}"`
  to also match `v9.0.0-rc1`.
- **Glob outside placeholders**: `*` (any chars), `?` (any one char), `[seq]` (character class).
  Example: `"v[89].*"` matches v8.x and v9.x refs without version-aware semantics.
- **Multiple patterns**: pass a list of strings; a ref matches if any pattern matches.
  Example: `match: [ "my-dev-tag", "v{major}.{minor}.{patch}" ]`

A `retain.version` policy requires versioned patterns (containing numeric `{…}` placeholders),
and all versioned patterns in one selector must agree on their level set. Plain glob patterns
(`"*"`, `"v[89].*"`) carry no version levels and cannot drive version-based retention.

#### Retention (`retain` block)

Omitting `retain` keeps every matched ref forever. A `retain` block trims the matched set:
a marker survives only if it satisfies **every** criterion present (intersection). Across
multiple selectors for the same repo, keeps are **unioned** - a marker is kept if any selector
keeps it (so a bare "keep forever" selector acts as an allowlist alongside a trimming rule).
All values are inclusive.

| Field | Applies to | Description |
|-------|-----------|-------------|
| `age` | any | Keep commits within this age; prune older. Duration `<n><unit>` (see below). |
| `count` | any | Keep the newest N commits by commit date (per branch name for branches; pooled across the family for tags). |
| `version` | versioned tags | Value-relative per-level retention (see below). |
| `prerelease` | versioned tags | `keep` (default) or `superseded` (drop a prerelease once its final release ships). Sibling of `version`. |

##### `version` (value-relative)

Each field keeps the newest N **values** at that level within its parent group - a threshold
of `latest − (N − 1)`, **not** a count of existing refs. Omit a field (or set `null`) for no
constraint at that level.

| Field | Description |
|-------|-------------|
| `majors` | Newest N major values. `majors: 2` keeps the latest major and the one behind it (n-1 EOL). |
| `minors` | Newest N minor values per (major). |
| `patches` | Newest N patch values per (major, minor). `patches: 1` = newest patch per minor. |
| `builds` | Newest N build values per (major, minor, patch). |

Because it is value- not count-based, with majors `{2, 9}` indexed, `majors: 2` keeps `{9}`
(threshold 8), not `{9, 2}`.

Duration format (for `age`/`since.age`): `<n><unit>` where unit is `s` (seconds), `h` (hours),
`d` (days), `w` (weeks), `m` (30-day month), `y` (365-day year).

#### Example

```yaml
sources:
- git:
    host: github
    org: elastic
    repo: docs-content
    ref_type: branch
  match: main
  retain:
    count: 1                  # head-only: keep the newest indexed commit

- git:
    host: github
    org: elastic
    repo: elasticsearch
    ref_type: tag
  match:
  - v{major}.{minor}.{patch}
  - v{major}.{minor}.{patch}-{prerelease}
  since:
    ref: v8.17.0              # start indexing from this release
  retain:
    version:
      majors: 2               # keep the latest major + one behind (n-1)
      patches: 1              # newest patch per (major, minor)
    prerelease: superseded    # drop -rc once its final ships

- git:
    host: github
    org: elastic
    repo: elasticsearch
    ref_type: branch
  match: main
  retain:
    count: 5                  # newest 5 indexed commits of main

- git:
    host: github
    org: elastic
    repo: elasticsearch
    ref_type: tag
  match: my-dev-tag           # no retain -> kept forever (allowlist)

- git:
    host: github
    org: elastic
    repo: elasticsearch
    ref_type: commit
  match: cfefb3b              # pin an ad-hoc commit not on any tracked branch/tag tip
  retain:
    age: 2y                   # only 'age' is valid for commit sources
```

Indexing is idempotent - re-running only indexes refs that are new or have moved.

### Scheduling (`schedules:` / `sources[i].schedule`)

One `sourcerer.yml` can declare per-source schedules, replacing the old pattern of multiple
config files each driven by their own cron job. Run `sourcerer index --config` on a **frequent
cron** (e.g. every 5 minutes) and let the schedule config control the actual indexing cadence.

**How the gate works**: on each invocation of `index --config`, before any ls-remote or clone
work, the command queries `sourcerer-v2-refs` to see when each source was last fully indexed
(`status: complete`) and whether any ref in its scope is actively being indexed (`status:
indexing`). Only sources whose schedule has fired since their last indexed run proceed to the
expensive pipeline. Sources where another run is actively indexing are skipped.

- **Schedule syntax**: a 5-field cron expression (e.g. `"0 */3 * * *"` = every 3 hours) or a
  duration (`"3h"`, `"1d"` — same syntax as `retain.age`).
- **Precedence**: `sources[i].schedule` > most-specific `schedules[i]` rule > default (always due).
  Schedule rule scope fields (`host`, `org`, `repo`, `ref_type`, `ref`) support fnmatch glob
  wildcards; `ref_type` accepts exact values or bare `*` only. Specificity weights: exact = 2,
  glob = 1, omitted = 0, summed across all five fields — so an exact field always beats a glob field
  at the same level. `ref` is matched against the source's configured `match` pattern string(s)
  (not live ref names — the gate runs pre-network).
- **In-progress guard**: if a ref in scope has `status: indexing` with `indexing_started_at`
  newer than 6 hours ago, the whole source is skipped. After 6 hours (stuck-retry interval),
  the source is treated as due regardless.
- **Two-phase marker**: before content ingest, a ref doc is written with `status: indexing` and
  `indexing_started_at`. On successful completion, it is overwritten with `status: complete` and
  `indexed_at`. A killed/crashed run leaves behind an `indexing` marker that the gate detects;
  after 6 hours it retries automatically.
- **No-schedule fallback**: a config with no `schedules` section and no `sources[i].schedule`
  fields behaves identically to before (all sources always due — the gate is transparent).
- **`--dry-run`**: when schedules are configured, `--dry-run` prints a schedule gate report
  (which sources are due / not-due / in-progress and why) before the normal ref-level preview.

### Clone cache

`index` keeps each repo cloned under a persistent cache directory and refreshes it with
`git fetch` on later runs, rather than re-cloning every time. A frequently-scheduled run (e.g.
every 5 minutes via the scheduling feature above) then transfers only the new commits since the
last run instead of a full clone of a large repo's history. Combined with the cheap pre-clone
skip (a repo with no moved refs isn't even fetched) and immutable-tag dedup, repeated runs stay fast.

- **Blobless clone**: clones use `git clone --filter=blob:none` - every commit, tree, and ref is
  present (so any branch/tag/pinned commit stays reachable and checkoutable), but file contents
  are not downloaded up front. A blob is faulted in from `origin` the first time a commit that
  needs it is checked out, so disk usage tracks the working set actually indexed, not the repo's
  full history.
- **Location** (precedence): `--cache-dir` flag → `SOURCERER_CACHE_DIR` env → `$XDG_CACHE_HOME/sourcerer` → `~/.cache/sourcerer`. Clones live at `<cache>/repos/<host>/<org>/<repo>`.
- **Safe to delete**: the cache is a pure derived artifact (all index state lives in Elasticsearch). Removing it just forces a fresh (blobless) clone on the next run - this is also how a cache directory populated by an older, full-clone version of sourcerer gets converted to blobless: delete it once and let the next run re-create it.
- **`--ephemeral`**: skip the cache and clone into a throwaway temp dir (good for one-off or CI runs).
- **Concurrency**: a per-repo advisory lock prevents two overlapping runs from corrupting the same clone; if a repo is already locked by another run, it is skipped for that run.
- **Garbage collection**: after each fetch, a best-effort `git gc` expires reflogs and prunes
  objects that are no longer reachable - chiefly blobs faulted in for commits that fell out of a
  branch's retained window since the last run. A gc failure never fails the index run.

## Local stdio MCP proxy (`sourcerer mcp-proxy`)

Runs a local stdio↔streamable-HTTP MCP proxy so Claude Desktop can reach the Agent Builder MCP
endpoint (`{KIBANA_URL}/api/agent_builder/mcp`) without Node or `npx`. Claude Desktop launches
`sourcerer mcp-proxy` as a subprocess and communicates over stdio; the command forwards
every request to the remote endpoint with the `Authorization` header injected from the
environment.

- **Auth**: accepts `ELASTICSEARCH_API_KEY` (`Authorization: ApiKey <key>`) or both
  `ELASTICSEARCH_USERNAME` + `ELASTICSEARCH_PASSWORD` (`Authorization: Basic <base64>`).
- **Env loading**: `-e/--env <file>` loads a `.env` before options resolve, same as other
  commands. In Desktop's config, set env vars directly in the `env` block of `mcpServers`.
- **Stderr only**: all diagnostics (errors, startup messages) go to stderr; stdout carries
  the JSON-RPC stream. Desktop never shows stderr to the user, so errors appear in its logs.
- **No TLS**: stdio transport needs no TLS on the proxy side; the upstream connection is plain
  HTTPS to `KIBANA_URL` (system CA bundle).
- **Implementation**: uses `fastmcp.FastMCP.as_proxy(ProxyClient(StreamableHttpTransport(...)))`,
  which handles SSE responses, `Mcp-Session-Id` session management, and server→client
  notifications automatically.

Typical `claude_desktop_config.json` entry:

```json
{
  "mcpServers": {
    "sourcerer": {
      "command": "sourcerer",
      "args": ["mcp-proxy"],
      "env": {
        "KIBANA_URL": "<your-kibana-url>",
        "ELASTICSEARCH_API_KEY": "<your-api-key>"
      }
    }
  }
}
```

Requires `sourcerer` installed and on PATH (e.g. `uv tool install sourcerer`).

## Index fields

Content is addressed by **host + commit**, not by ref name. A file's bytes are fully determined
by `(git.host, git.org, git.repo, git.commit, file.path)`, so the same file reached via any ref
collapses to a single doc (no per-ref duplication), while the same org/repo on two different git
hosts stays distinct. `git.host` is a lowercase keyword, placed before `git.org` in every index
template's mappings and index sort. Backing indices are `sourcerer-v2-refs`,
`sourcerer-v2-files~{git.host}~{git.org}~{git.repo}`, and
`sourcerer-v2-lines~{git.host}~{git.org}~{git.repo}` (read via the unchanged `sourcerer-refs` /
`sourcerer-files` / `sourcerer-lines` aliases).

- **Tags** are *not* stored on content docs. Each tag is one tiny doc in `sourcerer-refs`
  mapping the tag to its commit. To search a tagged release, resolve it to a commit via the
  refs index (the `sourcerer.refs.list` tool), then filter content by `git.host` + `git.commit`.
- **Branches** are *not* stored on content docs (a branch moves; keeping it there would
  force expensive rewrites of the lines index on every move). Each branch is one tiny doc
  in `sourcerer-refs` mapping the branch to its current commit. To search a branch,
  resolve it to a commit via the refs index (the `sourcerer.refs.list` tool), then filter
  content by `git.host` + `git.commit`.

## Releases

`pyproject.toml` is the source of truth for the project version. Release version changes
must be made with `./scripts/release.sh prepare vMAJOR.MINOR.PATCH`, which bumps
`pyproject.toml`, `uv.lock`, `.claude-plugin/marketplace.json`, and `README.md` together
and commits the result. Review and merge that commit to `main` before publishing.

Publish releases only by running `./scripts/release.sh publish vMAJOR.MINOR.PATCH` from an
up-to-date `main`. Do not create or push release tags manually, modify an existing release
tag, or bypass the script's lockfile, test, build, branch, version, or remote-tag checks.
Pushing a valid tag triggers `.github/workflows/release.yml`, which repeats the quality
checks and creates the GitHub release.

### Upgrading from v1 to v2

v2.0.0 adds a `git.host` dimension so the same org/repo can be indexed and cited across
different git hosting providers. This is a breaking change:

- **Config**: `repos.yml` becomes `sourcerer.yml` with a new schema. The old flat list of
  `{org, repo, refs: [{type, ...}]}` entries becomes a `sources:` list where each entry names a
  single `git: { host, org, repo, ref_type }` plus top-level `match`/`since`/`retain`. See the
  Quickstart above and `sourcerer.example.yml`.
- **Indices**: backing indices are renamed `sourcerer-v1-*` to `sourcerer-v2-*` and content is
  keyed by `(git.host, git.org, git.repo, git.commit, file.path)`. There is no automatic
  migration - run `sourcerer setup` to create the v2 templates, then re-index. The old
  `sourcerer-v1-*` indices can be deleted once you have re-indexed.
- **Citations**: `sourcerer setup --config sourcerer.yml` reads the config's `hosts:` section
  and generates one citation skill per host so the agent formats links correctly for each
  provider. Run `setup` again whenever you add or customize a host.