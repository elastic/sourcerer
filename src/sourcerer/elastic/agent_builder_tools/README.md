# Elastic Agent Builder Tools

## Scoping by `git.*` namespace

All queries to `sourcerer-refs`, `sourcerer-files`, and `sourcerer-lines`
must support filtering by `git.host`, `git.org`, `git.repo`, and `git.commit`
with optional wildcard (`"*"`) matching.

Query snippet:

```js
// FROM command
| WHERE git.host LIKE ?git_host
    AND git.org LIKE ?git_org
    AND git.repo LIKE ?git_repo
    AND (
    // Resolve git_commit, git_ref, and git_ref_type against the small
    // sourcerer-refs index first, rather than evaluating the wildcard
    // match per line against this (far larger) content index.
    // Content docs come in two disjoint shapes:
    //   1. Commit snapshots have git.commit and no git.ref
    //   2. Incremental refs have git.ref and no git.commit
    // So resolution yields two membership sets off the same match:
    //   1. Matching commits, checked against snapshot-shaped rows
    //   2. Matching refs, checked against incremental-shaped rows
    // Includes a consistency guard by only retrieving content from refs
    // whose indexing is complete.
    (git.commit IS NOT NULL AND git.commit IN (
        // Commit snapshots
        FROM sourcerer-refs
        | WHERE git.host LIKE ?git_host
            AND git.org LIKE ?git_org
            AND git.repo LIKE ?git_repo
            AND git.commit LIKE ?git_commit
            AND git.ref LIKE ?git_ref
            AND git.ref_type LIKE ?git_ref_type
            AND status == "complete"
        | KEEP git.commit
        ))
    OR
    (git.ref IS NOT NULL AND git.commit IS NULL AND git.ref IN (
        // Incremental refs
        FROM sourcerer-refs
        | WHERE git.host LIKE ?git_host
            AND git.org LIKE ?git_org
            AND git.repo LIKE ?git_repo
            AND git.commit LIKE ?git_commit
            AND git.ref LIKE ?git_ref
            AND git.ref_type LIKE ?git_ref_type
            AND status == "complete"
        | KEEP git.ref
        ))
    )
    // other filters

// Branch by content-doc shape:
//   1. Content for commit snapshots already carry git.commit, and status
//      was confirmed by the membership subquery above (status=="complete"),
//      so the snapshot arm needs no join; it just asserts status to match
//      the incremental arm's column.
//   2. Content for incremental refs carry only git.ref. The join resolves
//      the ref's current status from its join doc. This join assumes at
//      most one doc per (host,org,repo,ref). That assumption requires a
//      "one update mode owns a ref name" invariant (no repo may have both
//      a snapshot and incremental source targeting the same ref name),
//      which must be enforced separately (e.g. at config-validation time);
//      nothing in this query itself enforces it.
| FORK
    ( WHERE git.commit IS NOT NULL
        | EVAL status = "complete" )
    ( WHERE git.ref IS NOT NULL AND git.commit IS NULL
        | LOOKUP JOIN sourcerer-refs ON git.host, git.org, git.repo, git.ref )

// Consistency guard: Only retrieve from a ref whose indexing is complete.
// Excludes torn/partial-read windows when an incrementally indexed ref is
// in the middle of an update. This is a no-op for the commit snapshot arm,
// whose status is always "complete" from the EVAL above.
| WHERE status == "complete"

// rest of query
```

Params configuration:

```yaml
params:
  git_host:
      type: string
      description: Filter by git host(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  git_org:
      type: string
      description: Filter by git org(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  git_repo:
      type: string
      description: Filter by git repo(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  git_commit:
      type: string
      description: Filter by git commit(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
    git_ref:
      type: string
      description: Filter by ref name(s), e.g. "main" or "v1.*" (supports * and ? wildcards)
      optional: true
      defaultValue: "*"
    git_ref_type:
      type: string
      description: Filter by ref type (can be "branch", "tag", "commit", or any with "*")
      optional: true
      defaultValue: "*"
```

## Glob matching `file.path`

Queries to `sourcerer-files` and `sourcerer-lines` should support filtering by
`file.path` with optional glob (`"*"` and `"**"`) matching.

Query snippet:

```js
// FROM command
| WHERE git.host LIKE ?git_host
    AND git.org LIKE ?git_org
    AND git.repo LIKE ?git_repo
    // filter by git_commit, git_ref, and git_ref_type
    AND file.path LIKE ?file_path
    // other filters

// Enforce glob depth for * and ** on file.path.
// Without this, "src/*/Job.java" would match files at any depth,
// not just three path segments, because a wildcard * in ES|QL
// treats "/" as a regular character.
// 
//   _fp_is_recursive  Does the pattern contain "**"?
//   _fp_segs          How many segments does the pattern have? ("src/*/Job.java" = 3)
//   _file_segs        How many segments does this indexed file.path have?
//   
// WHERE _fp_is_recursive OR _file_segs == _fp_segs keeps the file if the
// pattern is recursive (** means any depth is intended) or if the file
// depth exactly matches the pattern depth (equal depths mean a * could only
// have matched within a single segment).
| EVAL _fp_is_recursive = ?file_path != REPLACE(?file_path, "[*][*]", "")
| EVAL _fp_segs = LENGTH(?file_path) - LENGTH(REPLACE(?file_path, "/", "")) + 1
| EVAL _file_segs = MV_COUNT(SPLIT(file.path, "/"))
| WHERE _fp_is_recursive OR _file_segs == _fp_segs

// rest of query
```

Params configuration:

```yaml
params:
  git_host:
      type: string
      description: Filter by git host(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  git_org:
      type: string
      description: Filter by git org(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  git_repo:
      type: string
      description: Filter by git repo(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  git_commit:
      type: string
      description: Filter by git commit(s) (supports * wildcards)
      optional: true
      defaultValue: "*"
  file_path:
      type: string
      description: File path(s) to grep (supports * and ** glob syntax, e.g. src/test, src/*/resources, src/**, src/**/*.xml)
      optional: true
      defaultValue: "**"
```