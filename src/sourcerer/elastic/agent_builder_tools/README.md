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
    AND git.commit LIKE ?git_commit
    // other filters

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
    AND git.commit LIKE ?git_commit
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