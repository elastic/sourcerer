# Specification for sourcerer.yml

sourcerer.yml is a configuration file that governs how the `sourcerer` CLI
performs its `setup`, `index`, and `prune` commands.

## Overview

|Field                                  |Allowed type(s)      |Required|Notes|
|---------------------------------------|---------------------|--------|-----|
|`hosts`                                |Array[Object]        |No      ||
|`hosts[i].id`                          |String               |Yes     ||
|`hosts[i].name`                        |String               |No      ||
|`hosts[i].urls`                        |Object               |Yes     ||
|`hosts[i].urls.clone`                  |String               |Yes     ||
|`hosts[i].urls.directory`              |String               |Yes     ||
|`hosts[i].urls.file`                   |String               |Yes     ||
|`hosts[i].urls.line`                   |String               |Yes     ||
|`hosts[i].urls.line_range`             |String               |Yes     ||
|`schedules`                            |Array                |No      ||
|`schedules[i].git`                     |Object               |No      ||
|`schedules[i].git.host`                |String               |No      ||
|`schedules[i].git.org`                 |String               |No      ||
|`schedules[i].git.repo`                |String               |No      ||
|`schedules[i].git.ref_type`            |String               |No      ||
|`schedules[i].git.ref`                 |String               |No      ||
|`schedules[i].schedule`                |String               |Yes     ||
|`sources`                              |Array[Object]        |No      ||
|`sources[i].git`                       |Object               |Yes     ||
|`sources[i].git.host`                  |String               |Yes     ||
|`sources[i].git.org`                   |String               |Yes     ||
|`sources[i].git.repo`                  |String               |Yes     ||
|`sources[i].git.ref_type`              |String               |Yes     ||
|`sources[i].match`                     |String, Array[String]|No      ||
|`sources[i].since`                     |Object               |No      ||
|`sources[i].since.age`                 |String               |No      ||
|`sources[i].since.date`                |String               |No      ||
|`sources[i].since.commit`              |String               |No      ||
|`sources[i].since.ref`                 |String               |No      ||
|`sources[i].retain`                    |Object               |No      ||
|`sources[i].retain.age`                |String               |No      ||
|`sources[i].retain.count`              |Integer              |No      ||
|`sources[i].retain.version`            |Object               |No      ||
|`sources[i].retain.version.majors`     |Integer              |No      ||
|`sources[i].retain.version.minors`     |Integer              |No      ||
|`sources[i].retain.version.patches`    |Integer              |No      ||
|`sources[i].retain.version.builds`     |Integer              |No      ||
|`sources[i].retain.version.prereleases`|Integer              |No      ||
|`sources[i].retain.prerelease`         |String               |No      ||
|`sources[i].schedule`                  |String               |No      ||
|`sources[i].index.level`               |String               |No      |NOT IMPLEMENTED|
|`sources[i].index.suffix`              |String               |No      |NOT IMPLEMENTED|

Notes:
- Fields can be expressed either in nested format or flat dotted format.
- When an optional field has a descendant that is required, it means the descendent is only required if its ancestor exists.

## Fields

### `hosts`

Holds information about git hosts. This is necessary when indexing code from
self-managed git hosts, or when using a provider whose deployments are
instance-scoped (AWS CodeCommit, Azure DevOps, GCP Secure Source Manager).

When omitted, Sourcerer defaults to a hardcoded list of known git hosts
(e.g. GitHub, GitLab, BitBucket).

When given, entries are merged with the hardcoded list of known git hosts.

For providers whose deployments are scoped by region, project, or instance
hostname (i.e. AWS CodeCommit, Azure DevOps, GCP Secure Source Manager), you
should define one entry per deployment, each with its own unique `id` suffixed
with the region, project, instance, or any other values needed to properly
namespace the repo (e.g. `aws-codecommit-us-east-1`). Put the region, project,
or instance hostname directly into that entry's `clone.url` and `links.*`
templates. `git.org` then holds only the plain org or project name, with no
extra scoping embedded in it.

If a given entry overwrites an entry from the hardcoded list of known git hosts,
only the given fields overwrite their respective fields in the hardcoded entry,
while other fields in the hardcoded entry keep their default values.

- Required: No
- Default: `null`
- Type: Array[Object]

### `hosts[i].id`

Value of `git.host` used in sourcerer.yml, documents, index names, and
_id generation.

Example: `github`

For AWS CodeCommit, Azure DevOps, or GCP Secure Source Manager, you should
suffix this value with the additional namespace values that they require
(e.g. region, project, instance hostname) to prevent collisions.

Example: `aws-codecommit-us-east-1`

- Required: Yes (if `hosts` exists)
- Type: String
- Validation:
  - Cannot contain these characters: `~`, `^`, `\`, `/`, `*`, `?`, `"`, `<`, `>`, `|`, `:`
  - Cannot contain uppercase characters or whitespace characters

### `hosts[i].name`

Human-readable name of the git hosting provider used in citation skills.

Example: `GitHub`

- Required: No
- Type: String
- Default: Uses the value of `hosts[i].id`

### `hosts[i].urls`

Holds URLs templates for cloning and citations used by the citation skills in
Elastic Agent Builder.

- Required: Yes (unless overwriting the field of a hardcoded host)
- Type: Object

### `hosts[i].urls.clone`

URL template for repositories when running `git clone` during indexing.

The `git` fields from the `sourcerer-v2-refs` index can be referenced as
variables with curly braces (e.g. `{git.org}`, `{git.repo}`).

Example: `https://github.com/{git.org}/{git.repo}.git`

- Required: Yes (unless overwriting the field of a hardcoded host)
- Type: String

### `hosts[i].urls.directory`

URL template for citing links to a directory in a repo. Used by the citation
skills in Agent Builder.

Fields from the `sourcerer-v2-files*` or `sourcerer-v2-lines*` indices can be
referenced as variables with curly braces (e.g. `{git.org}`, `{file.directory}`).

Example: `https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.directory}`

- Required: Yes (unless overwriting the field of a hardcoded host)
- Type: String

### `hosts[i].urls.file`

URL template for citing links to a file in a repo. Used by the citation skills
in Elastic Agent Builder.

Fields from the `sourcerer-v2-files*` or `sourcerer-v2-lines*` indices can be
referenced as variables with curly braces (e.g. `{git.org}`, `{file.path}`).

Example: `https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}`

- Required: Yes (unless overwriting the field of a hardcoded host)
- Type: String

### `hosts[i].urls.line`

URL template for citing links to a line of code in a repo. Used by the citation
skills in Elastic Agent Builder.

Fields from the `sourcerer-v2-files*` or `sourcerer-v2-lines*` indices can be
referenced as variables with curly braces (e.g. `{git.org}`, `{file.path}`, `{line.number}`).

Example: `https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number}`

- Required: Yes (unless overwriting the field of a hardcoded host)
- Type: String

### `hosts[i].urls.line_range`

URL template for citing links to a range of lines of code in a repo. Used by the
citation skills in Elastic Agent Builder.

Fields from the `sourcerer-v2-files*` or `sourcerer-v2-lines*` indices can be
referenced as variables with curly braces (e.g. `{git.org}`, `{file.path}`) as
well as fields returned by the `sourcerer.code.*` and `sourcerer.files.*` tools
in Elastic Agent Builder (e.g. `{line.number_start}`, `{line.number_end}`).

Example: `https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}`

- Required: Yes (unless overwriting the field of a hardcoded host)
- Type: String

### `schedules`

Defines coarse, scope-based default schedules. A schedule rule specifies an
optional git scope (`host`/`org`/`repo`/`ref_type`/`ref`) and a schedule
expression. The most specific matching rule wins; if no rule matches a source,
the source is always due (equivalent to `"* * * * *"`).

Scope fields support glob patterns: `host`, `org`, `repo`, and `ref` accept
fnmatch wildcards (`*`, `?`, `[seq]`); `ref_type` accepts exact values or bare
`*`. Specificity ranks exact fields (2 points) above glob fields (1 point) above
omitted fields (0 points) — so a rule with `org: "elastic"` beats one with
`org: "elastic-*"` for an org named `elastic`.

Sources without any `schedule` field and configs without a `schedules` section
behave identically to before: every source is treated as always due (the gate is
transparent).

Intended use: run `sourcerer index --config` on a frequent cron (e.g. every 5 minutes)
and let the schedules control the actual indexing cadence. Each run only indexes
sources whose schedule has fired since they were last indexed.

- Required: No
- Default: `null` (omitted)
- Type: Array[Object]

### `schedules[i].git`

Optional scope filter for this schedule rule. Five scope fields are available:
`host`, `org`, `repo`, `ref_type`, and `ref`. A rule with no `git` (or an empty
`git` block) matches all sources.

**Glob wildcards**: `host`, `org`, `repo`, and `ref` all support
[fnmatch](https://docs.python.org/3/library/fnmatch.html) glob patterns
(`*`, `?`, `[seq]`). A plain string with no glob characters still requires exact
equality. `ref_type` supports only an exact value (`branch`/`tag`/`commit`) or
the bare wildcard `*` — partial globs on an enum are rejected.

**Precedence / specificity**: the most-specific matching rule wins (see
`sources[i].schedule` for the full precedence chain). Specificity is computed
per field: an exact value counts 2, a glob pattern counts 1, omitted counts 0.
Sums are compared, so a rule with two exact fields (4) beats one with two glob
fields (2) beats a catch-all (0).

- Required: No
- Default: `null` (matches all sources)
- Type: Object

### `schedules[i].git.host`

Match sources with this `git.host`. Glob patterns are allowed.
When omitted, the rule matches sources on any host.

- Required: No
- Default: `null` (matches any host)
- Type: String
- Validation:
  - A plain string (no glob chars) must be a concrete host id; globs may match any host.
  - No arrays.

### `schedules[i].git.org`

Match sources with this `git.org`. Glob patterns are allowed.
When omitted, the rule matches sources in any org.

- Required: No
- Default: `null` (matches any org)
- Type: String
- Validation:
  - Globs (e.g. `elastic-*`) are accepted; a plain string requires exact equality.
  - No arrays.

### `schedules[i].git.repo`

Match sources with this `git.repo`. Glob patterns are allowed.
When omitted, the rule matches sources in any repo.

- Required: No
- Default: `null` (matches any repo)
- Type: String
- Validation:
  - Globs (e.g. `docs-*`) are accepted; a plain string requires exact equality.
  - No arrays.

### `schedules[i].git.ref_type`

Match sources with this `git.ref_type`. When omitted (or set to `*`), the rule
matches sources regardless of ref type.

Because `ref_type` is a fixed enum, only exact values or the bare wildcard `*`
are accepted — partial globs (e.g. `bra*`) are rejected with an error.

- Required: No
- Default: `null` (matches any ref_type)
- Type: String
- Validation:
  - Must be one of `branch`, `tag`, `commit`, or `*`.

### `schedules[i].git.ref`

Match sources whose configured `match` pattern(s) are matched by this glob.
Glob patterns are allowed.

Because the schedule gate runs **before any ls-remote or clone** (to avoid
network work for not-due sources), actual ref names are not yet available. `ref`
therefore scopes by the configured `match` pattern string, not by live ref names.
A rule matches a source if its `ref` glob hits at least one of the source's
`match` entries.

Examples:
- `ref: "v*"` matches sources with `match: v{major}.{minor}.{patch}` (the literal
  pattern text starts with `v`).
- `ref: "main"` matches a source with `match: main`.
- `ref: "feat-*"` matches sources whose match pattern starts with `feat-`.

When omitted, the rule matches any source regardless of its `match` patterns.

- Required: No
- Default: `null` (matches any source)
- Type: String
- Validation:
  - Globs are accepted; a plain string requires the source's match pattern to equal it exactly.
  - No arrays.

### `schedules[i].schedule`

The schedule expression for this rule. Accepts:

- **Cron**: a 5-field cron expression (e.g. `"0 */3 * * *"` = every 3 hours, `"0 2 * * *"` = daily at 2am).
- **Duration**: a duration string in the same syntax as `retain.age` / `since.age` (e.g. `"3h"`, `"1d"`). Due when `now - last_indexed_at >= duration`.

- Required: Yes (if `schedules[i]` exists)
- Type: String
- Validation:
  - Must be a valid 5-field cron expression (e.g. `"0 * * * *"`) or a duration string (e.g. `"3h"`, `"1d"`, `"30m"`)

### `sources`

Holds information on which refs to index and how long to retain them until they
qualify for pruning.

- Required: No
- Type: Array[Object]
- Default: `null` (omitted)

### `sources[i].git`

Scopes the selection of refs to match for indexing.

Each source names exactly one `(host, org, repo, ref_type)` to index. Every
`sources[i].git.*` field is a single, required, concrete string (no wildcards
and no arrays). Sources that share the same `(host, org, repo)` are grouped
together, so their retention policies combine (see `sources[i].retain`).

- Required: Yes (if the source exists)
- Type: Object

### `sources[i].git.host`

The `git.host` of the refs to index.

- Required: Yes
- Type: String
- Validation:
  - Must be a single concrete host id (no wildcards, no arrays)
  - Must be a known built-in host id, or a host id defined under `hosts:`
  - Same character rules as `hosts[i].id`

### `sources[i].git.org`

The `git.org` of the refs to index. This should be the plain org or project name
on the hosting provider.

- Required: Yes
- Type: String
- Validation:
  - Must be a single concrete value (no wildcards, no arrays)

### `sources[i].git.repo`

The `git.repo` of the refs to index.

- Required: Yes
- Type: String
- Validation:
  - Must be a single concrete value (no wildcards, no arrays)

### `sources[i].git.ref_type`

The `git.ref_type` of the refs to index.

- Required: Yes
- Type: String
- Validation:
  - Must be exactly one of `"tag"`, `"branch"`, or `"commit"` (no arrays)

### `sources[i].match`

Pattern(s) to match against the ref names of branches, tags, or commits within
the scope defined in `sources[i].git`.

Can be a static string (e.g. `"main"`, `"master"`) or a string containing
semantic version (SemVer) components expressed with curly braces.

Supported SemVer variables:

- `{major}` (e.g. the `1` in `"v1.2.3.4-rc1"`)
- `{minor}` (e.g. the `2` in `"v1.2.3.4-rc1"`)
- `{patch}` (e.g. the `3` in `"v1.2.3.4-rc1"`)
- `{build}` (e.g. the `4` in `"v1.2.3.4-rc1"`)
- `{prerelease}` (e.g. the `rc1` in `"v1.2.3.4-rc1"`)

Example: `"v{major}.{minor}.{patch}"` will match `"v1.2.3"` but not
`"1.2.3"`, `"v1.2"`. `"v1.2.3-rc1"`.

Multiple patterns can be given as an array of strings
(e.g. `[ "v{major}.{minor}.{patch}", "v{major}.{minor}.{patch}-{prerelease}" ]`).

When omitted, all refs within the scope defined in `sources[i].git` will qualify
for indexing if they don't also qualify for pruning.

- Required: No
- Type: String or Array[String]
- Default: `null` (omitted)

### `sources[i].since`

Defines the starting point for indexing relative to the most recent point in the
commit history within the scope defined in `sources[i].git`.  The starting point
is an inclusion floor.

Exactly one child field can be given (`age`, `date`, `commit`, or `ref`).

When `null`, empty (`{}`), or omitted, the entire history is considered for
indexing, and filtered only by `sources[i].match` and `sources[i].retain`.

- Required: No
- Type: Object
- Default: `null` (omitted)

### `sources[i].since.age`

An age relative to the present from which to begin indexing (e.g. `"30d"`, `"1y"`).

- Required: No
- Type: String
- Default: `null` (omitted)

### `sources[i].since.date`

A specific date from which to begin indexing (e.g. `"2026-01-01"`).

- Required: No
- Type: String
- Default: `null` (omitted)
- Validation:
  - Must match the date pattern `YYYY-MM-DD`

### `sources[i].since.commit`

A specific commit hash from which to begin indexing (e.g. `"2d8cdecce194d40c2b5d6a0270a13f7ec125941f"`).

- Required: No
- Type: String
- Default: `null` (omitted)

### `sources[i].since.ref`

A specific ref name from which to begin indexing (e.g. `"v1.2.3"`).

- Required: No
- Type: String
- Default: `null` (omitted)

### `sources[i].retain`

Defines how long to retain indexed refs before they qualify for pruning.
Also prevents indexing refs that already qualify for pruning.

When multiple criteria (`age`, `count`, `version`) are specified, the strictest
criteria that matches at runtime will be used.

If omitted, indexed refs will never qualify for pruning and will be kept forever.

All values are inclusive.

- Required: No
- Type: Object
- Default: `null` (omitted)

### `sources[i].retain.age`

The age of refs to retain since the present (e.g. `90d`).

- Required: No
- Type: String
- Default: `null` (omitted)

### `sources[i].retain.count`

The number of refs to retain.

- Required: No
- Type: Integer
- Default: `null` (omitted)

### `sources[i].retain.version`

The semantic version of refs to retain.

Each field keeps the newest *n* values at that level within its parent group
(a threshold of `latest − (n−1)`), not a count of existing refs.

Omit a field (or set `null`) for no constraint at that level.

- Required: No
- Type: Object
- Default: `null` (omitted)

### `sources[i].retain.version.majors`

The number of major versions to retain before a ref qualifies for pruning.

- Required: No
- Type: Integer
- Default: `null` (omitted)

### `sources[i].retain.version.minors`

The number of minor versions to retain for each retained major version before a
ref qualifies for pruning.

- Required: No
- Type: Integer
- Default: `null` (omitted)

### `sources[i].retain.version.patches`

The number of patch versions to retain for each retained major/minor version
(whichever is most specific) before a ref qualifies for pruning.

- Required: No
- Type: Integer
- Default: `null` (omitted)

### `sources[i].retain.version.builds`

The number of build versions to retain for each retained major/minor/build
version (whichever is most specific) before a ref qualifies for pruning.

- Required: No
- Type: Integer
- Default: `null` (omitted)

### `sources[i].retain.version.prereleases`

The number of prerelease versions to retain for each retained major/minor/build/patch
version (whichever is most specific) before a ref qualifies for pruning.

- Required: No
- Type: Integer
- Default: `null` (omitted)

### `sources[i].retain.prerelease`

Whether to keep or drop prelease versions once its respective version is
generally available.

Example: `"v1.2.3-rc1"` qualifies for pruning if `"v1.2.3"` exists and
`sources[i].retain.prerelease` is `"superseded"`.

- Required: No
- Type: String
- Default: `"keep"`
- Validation:
  - Must be either `"keep"` or `"superseded"`

### `sources[i].schedule`

Configures the indexing schedule for refs that match `sources[i].match`.
Overrides anything in `schedules` that might have otherwise applied to this source.

Schedule precedence (highest to lowest):

1. `sources[i].schedule` (per-source override)
2. Most specific matching `schedules[i]` rule. Specificity is the sum of per-field
   weights: exact value = 2, glob pattern = 1, omitted = 0, across all five scope
   fields (`host`, `org`, `repo`, `ref_type`, `ref`). Ties are broken by the order
   the rules appear in the config file (first matching rule wins).
3. Default: always due (`"* * * * *"`)

When omitted and no `schedules` rule matches, the source is always due for
indexing (if it doesn't also qualify for pruning).

Accepts a 5-field cron expression or a duration string (see `schedules[i].schedule`).

The "due" check compares against the **most recently completed** indexing run for
this source's scope `(host, org, repo, ref_type)` as recorded in `sourcerer-v2-refs`.
A source is **not due** if another run has `status: indexing` for a ref in scope
with `indexing_started_at` newer than 6 hours ago. This prevents redundant
parallel work when the same config is invoked on a tight cron schedule.

If a run was interrupted and left a ref with `status: indexing` older than
6 hours, the source is retried automatically on the next invocation.

- Required: No
- Type: String
- Default: `null` (omitted, source inherits from `schedules` or is always due)
- Validation:
  - Must be a valid 5-field cron expression (e.g. `"0 * * * *"`) or a duration (e.g. `"3h"`)

### `sources[i].index`

NOT IMPLEMENTED

Configures the name of the `files` and `lines` indices that this source will be
indexed to.

- Required: No
- Type: Object
- Default: `null` (omitted)

### `sources[i].index.level`

NOT IMPLEMENTED

Defines the namespacing level for the names of the `files` and `lines` indices.

This provides finer control over the size of indices and shards. An org with
many small repos can have its files and lines collocated in a single org-level
index for each to keep the shard count low. A repo with large commits can have
its files and lines separated into commit-level indices to control shard size.

Values of `level` and their effects on index names:

|`level`   |Index name                                                   |
|----------|-------------------------------------------------------------|
|`"host"`  |`sourcerer-v2-*~{git.host}`                                  |
|`"org"`   |`sourcerer-v2-*~{git.host}~{git.org}`                        |
|`"repo"`  |`sourcerer-v2-*~{git.host}~{git.org}~{git.repo}`             |
|`"commit"`|`sourcerer-v2-*~{git.host}~{git.org}~{git.repo}~{git.commit}`|

- Required: No
- Type: String
- Default: `"repo"`
- Validation:
  - Must be one of: `"host"`, `"org"`, `"repo"`, or `"commit"`

### `sources[i].index.suffix`

NOT IMPLEMENTED

If not `null`, this suffix is appended to the end of the index name separated by
a caret (`^`).

Example index naming pattern where `sources[i].index.level` is `"repo"` and
`sources[i].index.suffix` is `"@deploy"`:

`sourcerer-v2-*~{git.host}~{git.org}~{git.repo}^@deploy`

For instance:

`sourcerer-v2-files~github~elastic~kibana^@deploy`
`sourcerer-v2-lines~github~elastic~kibana^@deploy`

- Required: No
- Type: String
- Default: Omitted (`null`)
- Validation:
  - Cannot contain these characters: `~`, `^`, `\`, `/`, `*`, `?`, `"`, `<`, `>`, `|`, `:`
  - Cannot contain uppercase characters or whitespace characters
  - An empty string (`""`) is treated as omitted (`null`)

## Example

Here are the full example contents of sourcerer.yml that will replace repos.yml
and remain referenced by the --config argument of commands:

```yaml
hosts:
- id: github
  name: GitHub
  clone:
    protocol: "https"
    url: "https://github.com/{git.org}/{git.repo}.git"
  links:
    directory: "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.directory}"
    file:  "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}"
    line: "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number}"
    line_range: "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}"

# Coarse schedules by scope. The most specific matching rule wins;
# if no rule matches a source, it is always due.
schedules:
- git: { host: "github", org: "elastic" }
  schedule: "0 */3 * * *"   # elastic org: every 3 hours
- git: { host: "github", org: "elastic", repo: "docs-content" }
  schedule: "0 2 * * *"     # docs repo: nightly at 2am (more specific -> wins)

sources:
- git: { host: "github", org: "elastic", repo: "elasticsearch", ref_type: "tag" }
  match: [ "v{major}.{minor}.{patch}", "v{major}.{minor}.{patch}-{prerelease}" ]
  since.ref: "v1.0.0"
  retain.version: { majors: 2, patches: 1 }
  retain.prerelease: superseded
  schedule: "0 0 * * *"   # per-source override: nightly (overrides the 3h schedules rule above)
```