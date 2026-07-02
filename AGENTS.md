# Sourcerer

## CLI

Commands:

- `sourcerer setup`
- `sourcerer index <org>/<repo> [-b <branch>] [-t <tag>] [-c <commit>]`
- `sourcerer index --config <file>`
- `sourcerer help`

### Indexing multiple repos with a config

`sourcerer index --config repos.yml` indexes many repos, branches, and tags in one run. The
config is a YAML list, one entry per repo. `branches` and `tags` are lists of **glob patterns**
(`*`, `?`, `[seq]` — not regex) matched against the remote's ref names, so newly published tags
matching a pattern are discovered and indexed automatically on each run.

```yaml
- org: elastic
  repo: elasticsearch
  branches: [ "main" ]
  tags: [ "v[789].*" ]   # v7.x–v9.x; excludes older majors

- org: elastic
  repo: kibana
  branches: []           # empty or omitted = no branch refs
  tags: [ "v*" ]         # all version tags
```

There is no separate date/version cutoff field — bound the range with the pattern itself (e.g.
`v[789].*`). An omitted or empty list selects nothing for that ref type; `"*"` matches all.
Indexing is idempotent (see `files._id` semantics), so re-running only indexes refs that are
new or have moved. See `repos.example.yml`.

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