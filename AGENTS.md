# Sourcerer

## CLI

Commands:

- `sourcerer setup`
- `sourcerer index <org>/<repo> [-b <branch>] [-t <tag>] [-c <commit>]`
- `sourcerer index --config <file> [--prune] [--dry-run]`
- `sourcerer prune [--config <file>] [--dry-run]` (config-driven retention prune is skipped
  without `--config`; the orphan sweep always runs)
- `sourcerer help`

### Indexing multiple repos with a config

`sourcerer index --config repos.yml` indexes many repos, branches, and tags in one run. The
config is a YAML list, one entry per repo. See `repos.example.yml`.

#### Top-level fields (per repo entry)

| Field | Required | Description |
|-------|----------|-------------|
| `org` | yes | GitHub org name |
| `repo` | yes | GitHub repo name |
| `refs` | no | List of selectors (see below). Omit or empty = index nothing for this repo. |

#### Selectors (`refs` list entries)

| Field | Required | Description |
|-------|----------|-------------|
| `type` | yes | `branch`, `tag`, or `commit`. |
| `match` | yes | For `branch`/`tag`: pattern string or list of patterns matched against ref names (version DSL + glob) — a ref matches if any pattern hits. For `commit`: a commit SHA/prefix string or list of them (see below). |
| `since` | no | Index-side inclusion floor: the earliest commit to start indexing from. See below. Not valid for `type: commit`. |
| `retain` | no | Retention policy (see below). Omit to keep forever. For `type: commit`, only `age` is valid. |
| `update` | no | `snapshot` (default) or `incremental`. `incremental` opts a `type: branch` selector into the isolated v2 incremental path (see below); it cannot be combined with `since` or `retain`. |

#### `type: commit` (pinning an explicit commit)

Pins one or more commits directly, rather than matching named refs. `match` entries are
7–40 hex chars — a full 40-char SHA, or a shorter prefix (git's own "short hash" convention;
a `git.commit` lookup uses a prefix match against the resolved full SHA). There's nothing to
index "from" for a single pinned point, so `since` is rejected; likewise `retain.count`,
`retain.version`, and `retain.prerelease` have no meaning for one commit and are rejected --
only `retain.age` (or omitting `retain` to keep forever) is allowed. A pinned commit must be
reachable from some fetched branch or tag (a full clone only contains objects reachable that
way) -- one that's been force-pushed away or only exists on an unfetched ref will fail to
check out, reported as a per-unit error.

```yaml
- type: commit
  match:
  - cfefb3b              # short prefix (>= 7 hex chars) or a full 40-char SHA
  retain:
    age: 2y               # only 'age' is valid for commit selectors (or omit = keep forever)
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
  `{prerelease}` — each numeric placeholder matches one numeric segment, enabling the
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
multiple selectors for the same repo, keeps are **unioned** — a marker is kept if any selector
keeps it (so a bare "keep forever" selector acts as an allowlist alongside a trimming rule).
All values are inclusive.

| Field | Applies to | Description |
|-------|-----------|-------------|
| `age` | any | Keep commits within this age; prune older. Duration `<n><unit>` (see below). |
| `count` | any | Keep the newest N commits by commit date (per branch name for branches; pooled across the family for tags). |
| `version` | versioned tags | Value-relative per-level retention (see below). |
| `prerelease` | versioned tags | `keep` (default) or `superseded` (drop a prerelease once its final release ships). Sibling of `version`. |

##### `version` (value-relative)

Each field keeps the newest N **values** at that level within its parent group — a threshold
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
- org: elastic
  repo: docs-content
  refs:
  - type: branch
    match: main
    retain:
      count: 1                # head-only: keep the newest indexed commit

- org: elastic
  repo: elasticsearch
  refs:
  - type: tag
    match:
    - v{major}.{minor}.{patch}
    - v{major}.{minor}.{patch}-{prerelease}
    since:
      ref: v8.17.0            # start indexing from this release
    retain:
      version:
        majors: 2             # keep the latest major + one behind (n-1)
        patches: 1            # newest patch per (major, minor)
      prerelease: superseded  # drop -rc once its final ships
  - type: branch
    match: main
    retain:
      count: 5                # newest 5 indexed commits of main
  - type: tag
    match: my-dev-tag         # no retain -> kept forever (allowlist)
  - type: commit
    match: cfefb3b            # pin an ad-hoc commit not on any tracked branch/tag tip
    retain:
      age: 2y                 # only 'age' is valid for commit selectors
```

Indexing is idempotent — re-running only indexes refs that are new or have moved.

### Incremental branch indexing (`update: incremental`)

By default a branch selector is a **snapshot** selector: each new commit produces a complete,
immutable, commit-addressed file/line snapshot in the `sourcerer-v1-*` indices. That keeps
coherent history, but the indexing cost scales with the whole repository on every move.

`update: incremental` opts a `type: branch` selector into an isolated **v2** evaluation path
for moving branches that need frequent, cheap refreshes:

```yaml
- org: elastic
  repo: elasticsearch
  refs:
  - type: branch
    match: main
    update: incremental      # v2 incremental path; cannot combine with since/retain
```

How it behaves:

- **Isolated schema.** Incremental content lives in `sourcerer-v2-files~<org>~<repo>` and
  `sourcerer-v2-lines~<org>~<repo>`, with one mutable lookup document per branch in
  `sourcerer-v2-refs`. The `sourcerer-v1-*` snapshot indices are never touched. Content is
  **ref-addressed** (`git.ref_key` / `git.ref`), not commit-addressed — no commit SHA in the
  document id — so a branch keeps exactly one live view.
- **First run rebuilds.** The initial incremental run (or any run where the previous completed
  commit is no longer available locally, e.g. after a force-push) rebuilds the whole branch
  namespace into v2. It never migrates existing v1 data.
- **Changed-file updates.** Subsequent runs diff the last completed commit against the new
  remote tip and touch only the changed paths: deleted/modified/rename-source paths have their
  prior file and line docs deleted, then added/modified/rename-destination files are re-indexed.
  For a Customer Zero profile that changes roughly **10–20 files** per update, the work is
  proportional to those paths, not the whole repo.
- **No branch history.** Only the current view is retained — there are no per-commit snapshots
  for an incremental branch, which is why `retain` (nothing to trim) and `since` (no inclusion
  floor) are rejected at config parse time.
- **Temporary mixed revisions.** Elasticsearch is eventually consistent during an update by
  explicit design. While an update runs, the branch's `sourcerer-v2-refs` document reports
  `status: indexing`, `git.commit` stays at the last completed commit, and `git.target_commit`
  advertises the candidate. Queries stay available throughout and may briefly return a **mixed
  revision**; the completed `git.commit` used for citations only advances to `status: ready`
  after all deletes + indexing + a content refresh succeed.
- **Retry / fallback.** A failed update leaves `status: indexing` with the old completed commit
  intact plus a bounded `error`/`failed_at`; the next run retries old→current and clears those
  on success. A missing diff base falls back to a full branch rebuild rather than trusting an
  empty diff.
- **Agents query by ref key.** `sourcerer.refs.list` returns each ref's `update_mode`. For an
  incremental branch, pass its exact `git_ref_key` (not `git_commit`) to the code/file tools;
  they attach the completed commit via a `LOOKUP JOIN` on `sourcerer-v2-refs` for citations.

> Requires an Elasticsearch/ES|QL version that supports `index.mode: lookup` and `LOOKUP JOIN`.

#### Local evaluation

A repeatable way to measure the incremental win against a real cluster:

1. `sourcerer setup` — loads the v1 and v2 index templates (idempotent; leaves v1 data alone).
2. Index a branch once in incremental mode:
   `sourcerer index --config repos.yml` with an `update: incremental` branch selector. This is
   the full first-run rebuild — note the reported processed-file count and duration.
3. Push a commit to that branch changing 10–20 files (add/modify/delete/rename).
4. Re-run `sourcerer index --config repos.yml`. Compare the reported processed-file count and
   duration to the first run — only the changed paths should be processed.
5. Verify with the tools: `sourcerer.refs.list` shows one v2 refs doc for the branch with
   `update_mode: incremental`, `status`, and the completed commit; query the changed content
   with a code/file tool using the branch's `git_ref_key`, and confirm deleted/renamed paths
   return nothing.

Schedule incremental runs (e.g. via cron) at an interval comfortably longer than a single
update's duration, so consecutive runs never overlap on the same branch.

### Clone cache

`index` keeps each repo cloned under a persistent cache directory and refreshes it with
`git fetch` on later runs, rather than re-cloning every time. A regularly-scheduled run (e.g.
nightly) then transfers only the new commits since the last run instead of a full clone of a
large repo's history. Combined with the cheap pre-clone skip (a repo with no moved refs isn't
even fetched) and immutable-tag dedup, repeated runs stay fast.

- **Location** (precedence): `--cache-dir` flag → `SOURCERER_CACHE_DIR` env → `$XDG_CACHE_HOME/sourcerer` → `~/.cache/sourcerer`. Clones live at `<cache>/repos/<org>/<repo>`.
- **Safe to delete**: the cache is a pure derived artifact (all index state lives in Elasticsearch). Removing it just forces a fresh clone on the next run.
- **`--ephemeral`**: skip the cache and clone into a throwaway temp dir (good for one-off or CI runs).
- **Concurrency**: a per-repo advisory lock prevents two overlapping runs from corrupting the same clone; if a repo is already locked by another run, it is skipped for that run.

## Index fields

Content is addressed by **commit**, not by ref name. A file's bytes are fully determined
by `(git.org, git.repo, git.commit, file.path)`, so the same file reached via any
ref — branch, tag, or commit hash — collapses to a single doc (no per-ref duplication).

- **Tags** are *not* stored on content docs. Each tag is one tiny doc in `sourcerer-v1-refs`
  mapping the tag to its commit. To search a tagged release, resolve it to a commit via the
  refs index (the `sourcerer.resolveref` tool), then filter content by `git.commit`.
- **Branches** are *not* stored on content docs (a branch moves; keeping it there would
  force expensive rewrites of the lines index on every move). Each branch is one tiny doc
  in `sourcerer-v1-refs` mapping the branch to its current commit. To search a branch,
  resolve it to a commit via the refs index (the `sourcerer.resolveref` tool), then filter
  content by `git.commit`.