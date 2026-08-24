# Sourcerer

## CLI

Commands:

- `sourcerer setup [--config <file>] [--include-experimental] [CATEGORIES...]`
  (reads the config's `hosts:` to generate per-host citation skills; without `--config`, uses
  only the built-in host defaults). Valid categories: `all` (default), `agents`, `skills`,
  `tools`, `templates`, `dashboards`, `workflows`. Experimental resources live under
  `elastic/*/experimental/` and are excluded by default; pass `--include-experimental` to
  include them.
- `sourcerer index <org>/<repo> [-b <branch>] [-t <tag>] [-c <commit>]` (single-repo path
  defaults to `git.host` = `github`)
- `sourcerer index --config <file> [--prune] [--dry-run] [--no-backfill]`
- `sourcerer prune [--config <file>] [--dry-run]` (config-driven retention prune is skipped
  without `--config`; the orphan sweep always runs)
- `sourcerer mcp-proxy [-e <env>]` (run a stdio MCP proxy that forwards to the Kibana
  Agent Builder MCP endpoint; intended to be launched by Claude Desktop via `mcpServers`)
- `sourcerer help`

`sourcerer --version` (a top-level flag, not a subcommand) prints the installed version.

All commands that communicate with Elasticsearch or Kibana accept `--insecure` (flag, default
off). When set, TLS certificate verification is disabled — useful for locally-hosted clusters
with self-signed certificates that are trusted in your environment. Also configurable via the
`ALLOW_INSECURE_TLS` environment variable (set to `1` or `true`); the env var is read by all
commands including `mcp-proxy` when launched by Claude Desktop via its `env` block.

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
| `mode` | no | `snapshot` (default) or `delta` (branch-only). See below. |

#### `mode` (snapshot vs. delta)

`snapshot` (default): content is commit-addressed. A HEAD advance on a branch
indexes a whole new snapshot under the new commit.

`delta` (branch-only; rejects `since`/`retain` -- there is no per-commit history for either
to apply to): content is ref-addressed instead. A HEAD advance runs `git diff
--name-status` between the previously-completed commit and the new tip and only deletes/
reindexes the paths git reports changed -- add/modify/delete/rename/copy -- instead of
reindexing the whole tree. A missing diff base (force-push, GC'd, or the first index) rebuilds
the whole branch namespace. The refs join doc publishes `status: indexing` before any content
change and `status: complete` (with the new commit) only after the deletes/indexes/refresh all
succeed, so a crash mid-update leaves the prior commit and content in place.

```yaml
- git:
    host: github
    org: elastic
    repo: serverless-gitops
    ref_type: branch
  match: main
  mode: delta
```

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

**Behavior by `ref_type`:**

- **`ref_type: tag`** — `since` is a filter on which tags are *selected*: only tags whose commit date
  is on or after the floor are indexed. Each matching tag is still one indexed snapshot.
- **`ref_type: branch`** — `since` causes the branch's **commit history to be walked** back to the
  floor. Every commit on the first-parent mainline from the branch tip back to (and including) the
  oldest commit whose committer date is >= the floor is indexed as its own snapshot (one set of
  content docs + one refs marker per commit). This turns a single branch into N indexed commits.

  **Important:** pair a branch `since` with `retain.count` or `retain.age` to bound how many
  historical commits are actually indexed. Without a date/count `retain`, every commit the floor
  selects is indexed. The retention pre-filter trims retention-doomed commits *before* ingest —
  so `retain.count: 5` with `since.age: 1y` indexes only the 5 newest commits on the branch,
  not all commits in the past year.

  The history walk uses `--first-parent` semantics: only the branch's own mainline commits are
  enumerated; merged feature-branch commits appear only as the merge commit on the mainline, not
  as individual snapshots. This matches the intuitive "history of main."

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

Duration format (for `age`/`since.age`): `<n><unit>` where unit is `s` (seconds), `m` (minutes),
`h` (hours), `d` (days), `w` (weeks), `M` (30-day month), `y` (365-day year).

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
work, the command queries `sourcerer-v3-refs` to see when each source was last fully indexed
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
template's mappings and index sort. Backing indices are `sourcerer-v3-refs`,
`sourcerer-v3-files~{git.host}~{git.org}~{git.repo}`, and
`sourcerer-v3-lines~{git.host}~{git.org}~{git.repo}` (read via the unchanged `sourcerer-refs` /
`sourcerer-files` / `sourcerer-lines` aliases).

**Index routing (`sources[i].index.level` / `index.suffix`).** A source can override the content
index name: `level` (`host`/`org`/`repo` (default)/`commit`) chooses the granularity
(`sourcerer-v3-*~{host}` … `~{host}~{org}~{repo}~{commit}`), and `suffix` appends `^{suffix}`
(e.g. `~{host}~{org}~{repo}^deploy`). Routing is **per-source**, so two sources of the same
`(host, org, repo)` may target different indices; the read aliases match `sourcerer-v3-files*` /
`sourcerer-v3-lines*`, so every leveled/suffixed index auto-joins them and agents are unaffected.
The name is built by `files_index`/`lines_index` in `src/sourcerer/indices.py`; each ref marker in
`sourcerer-v3-refs` records the source's `index_level`/`index_suffix` (semantic, not the resolved
name — so a future prefix bump stays correct). Changing a source's routing between runs triggers a
**migration** (`sourcerer index` re-ingests at the new index, flips the marker, then deletes the old
copy; `sourcerer prune` sweeps any crash-leftover as `orphan:stale-location`, and deletes any
sourcerer content index drained to zero docs as `orphan:empty-index`). Commit-level indexing
creates one index/shard per commit — see `specs/sourcerer-yml.md` for the caveat.

- **Tags** are *not* stored on content docs. Each tag is one tiny doc in `sourcerer-refs`
  mapping the tag to its commit. To search a tagged release, resolve it to a commit via the
  refs index (the `sourcerer.refs.list` tool), then filter content by `git.host` + `git.commit`.
- **Branches** are *not* stored on content docs (a branch moves; keeping it there would
  force expensive rewrites of the lines index on every move). Each branch is one tiny doc
  in `sourcerer-refs` mapping the branch to its current commit. To search a branch,
  resolve it to a commit via the refs index (the `sourcerer.refs.list` tool), then filter
  content by `git.host` + `git.commit`.

### Universal join query

Content docs come in two disjoint shapes depending on how they were indexed:

- **Snapshot** (`mode: snapshot`): content docs carry `git.commit` and no `git.ref`. The ref-name
  marker in `sourcerer-v3-refs` (keyed by `build_ref_id`, one per snapshot source) carries the commit
  and status. `git.commit` on the content row is already the answer; no join is needed to resolve it.
- **Delta** (`mode: delta`): content docs carry `git.ref` and no `git.commit`. A
  dedicated refs join doc at `_id = build_ref_key(host,org,repo,ref)` (one per branch) holds the
  live HEAD commit and is advanced two-phase (INV-006). The join resolves `git.commit` from this doc.

Every Agent Builder content tool (`sourcerer.code.*`, `sourcerer.files.*`) uses
the same shape that handles both modes without fan-out:

```esql
FROM sourcerer-lines
| WHERE git.host LIKE ?git_host AND ...
    AND (
      // Resolve git_commit, git_ref, and git_ref_type against the small sourcerer-refs
      // index first (content docs carry no git.ref_type); the two membership sets handle
      // both content-doc shapes in one pass.
      (git.commit IS NOT NULL AND git.commit IN (
          FROM sourcerer-refs
          | WHERE git.host LIKE ?git_host AND ...
              AND git.commit LIKE ?git_commit
              AND git.ref LIKE ?git_ref
              AND git.ref_type LIKE ?git_ref_type
          | KEEP git.commit
          ))
      OR
      (git.ref IS NOT NULL AND git.commit IS NULL AND git.ref IN (
          FROM sourcerer-refs
          | WHERE git.host LIKE ?git_host AND ...
              AND git.commit LIKE ?git_commit
              AND git.ref LIKE ?git_ref
              AND git.ref_type LIKE ?git_ref_type
          | KEEP git.ref
          ))
      )
// Branch by content-doc shape to resolve git.commit for incremental refs:
//   Snapshot rows already carry git.commit (no join needed).
//   Incremental rows carry only git.ref; the join resolves git.commit from the refs join doc.
//   Safety of the incremental join (one doc per (host,org,repo,ref)) is enforced by
//   the "one mode owns a ref name" invariant at index time.
| FORK
    ( WHERE git.commit IS NOT NULL )
    ( WHERE git.ref IS NOT NULL AND git.commit IS NULL
        | LOOKUP JOIN sourcerer-refs ON git.host, git.org, git.repo, git.ref )
```

**Snapshot rows** carry `git.commit` directly; they are matched by the first IN subquery and pass
through the FORK unchanged. Critically, the snapshot arm never touches `sourcerer-refs` at query
time, so two complete markers sharing the same commit (branch + same-named tag) do NOT fan out —
the commit set is resolved once and deduplicated naturally.

**Incremental rows** carry `git.ref` but no `git.commit`; they are matched by the second IN
subquery and then joined in the FORK incremental arm. The LOOKUP JOIN resolves `git.commit` from the
refs join doc so incremental rows carry a citable commit SHA in the output. The join is safe
(no fan-out) because there is always exactly one incremental join doc per `(host,org,repo,ref)`:
`_id = build_ref_key(...)` (overwrite-in-place) and the runtime mode-conflict guard in
`selection.py` prevent multiple concurrent join docs for the same ref.

**Scoping params** (`git_commit`, `git_ref`, `git_ref_type`) are all optional (default `"*"`) and
support `*`/`?` wildcards (filters use `LIKE`). For a normal content question, resolve a ref first
(see `src/sourcerer/skills/ref-resolution/SKILL.md`), then pass the result through the appropriate
param: a commit SHA goes to `git_commit`; a branch or tag name goes to `git_ref` (optionally narrow
further with `git_ref_type: branch` or `git_ref_type: tag`). Leaving all three at `"*"` matches
content across all refs at once; because every content tool carries `git.commit` through to output
(and aggregations group `BY git.commit`), unpinned results stay attributable per commit rather than
being blended — but a version-specific answer should still pin a ref.

#### `status` field values

Every `sourcerer-v3-refs` document — snapshot ref-name markers and incremental join docs alike
— carries a `status` field drawn from a three-value vocabulary:

| Value | Meaning |
|---|---|
| `indexing` | A run is mid-flight. `indexing_started_at` is set; `indexed_at` is absent/null. Present on snapshot ref-name markers (written by `write_indexing_marker` just before ingest) and incremental join docs (written by `write_incremental_indexing`). A stale `indexing` doc whose `indexing_started_at` is older than the retry window (default 6 h) marks a crashed run and is treated as due for re-indexing. |
| `complete` | Fully indexed and ready to query. `indexed_at` is set; `indexing_started_at` is absent/null (the terminal write drops it). Written by `write_ref_marker` (snapshot markers) and `write_incremental_ready` (incremental join docs). The scheduler's "last indexed" aggregation and `sourcerer.refs.list`'s default `?status == "complete"` filter both use this value. |
| `stale` | A snapshot marker superseded by a mode switch to `delta`. Written by `mark_snapshot_markers_stale` (called BEFORE the incremental join doc is published as `complete`). The prune command reclaims their content and deletes the marker via `execute_stale_marker_deletions`. |

#### Uniqueness gate (INV-011 backstop)

`_run_uniqueness_gate` (`commands/index/command.py`) runs after each index pass and calls
`check_join_uniqueness` (`queries.py`) to verify:

- **Snapshot** (git.commit IS NOT NULL in content): each distinct commit must have ≥1 complete refs
  doc (presence check — multi-ref-per-commit is legal).
- **Delta** (git.ref IS NOT NULL in content): each distinct ref must have **exactly one**
  incremental join doc with `mode == "delta"` (anti-fan-out guard for the surviving join).

The gate is non-fatal (logs a warning, does not block): with the flip-status switchover in place,
violations should only occur if a stale-flip was skipped or crashed mid-way; the next prune run
reclaims the stale marker and resolves the violation automatically.

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

### Dependencies

Always pin dependencies to an exact version (`pkg==X.Y.Z`) in `pyproject.toml` — for both
runtime and dev dependencies. Do not use range constraints (`>=`, `~=`, `^`). Exact pins make
builds reproducible and let Dependabot recognize a fixed version directly from `pyproject.toml`
(a `>=` lower bound leaves the alert open even after the lockfile resolves to a safe version).
When bumping a dependency, set the pin to the version `uv lock` resolves and re-run the tests.

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

### Upgrading from v2 to v3

v3.0.0 adds incremental (ref-addressed) branch indexing (`update: incremental`, see above). This
is a breaking change to the backing indices, with no config schema change:

- **Indices**: backing indices are renamed `sourcerer-v2-*` to `sourcerer-v3-*`. There is no
  automatic migration - run `sourcerer setup` to create the v3 templates, then re-index every
  source. The old `sourcerer-v2-*` indices can be deleted once you have re-indexed.
- **Agent Builder tools**: content tools (`sourcerer.code.*`, `sourcerer.files.*`) replace their
  `git_commit` param with `git_ref` (a commit SHA or a branch/tag name); `git_commit` survives
  as an optional filter alongside `git_ref` and `git_ref_type`. Run `sourcerer setup` again to push
  the updated tool definitions.