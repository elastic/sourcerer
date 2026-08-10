---
name: "repo-discovery"
description: "Use at the start of most questions to identify which repos are indexed before querying content. Also use mid-conversation when an answer may require expanding into a related or upstream repo (e.g. tracing from an application repo into a lower-level dependency)."
---

## When this applies
- **At the start of a question**: confirm which repos are indexed and plan where to look before issuing any content queries.
- **Mid-investigation**: when the trail leads into a dependency, upstream library, or sibling repo - expand scope before drawing conclusions.
- **When a dependency boundary is reached**: if the current repo's code delegates to an external dependency (e.g., an import resolves outside the repo, behavior is implemented in an upstream library), always check whether that dependency is indexed before concluding the answer can't be found. Never assume a dependency is unindexed without checking.

## Tools

- `sourcerer.repos.list` lists all indexed repos by git host. Each result is an org/repo that has at least one ref indexed. Start here. Avoid filters; it's cheap to run.
- `sourcerer.repos.search` ranks repos whose code best matches a free-text query. You can improve recall by adding search terms and using snake_case or PascalCase (e.g. "elastic es|ql disk_bbq" matches any hits for "elastic", "disk_bbq", "disk", "bbq"; "DiskBBQ" matches any hits for "diskbbq", "disk", "bbq"; "es|ql" matches any hits for "es|ql", "es", "ql"). Prefer repos with higher scores. Helps with deciding which repo(s) are most relevant to your work, not for finding specific files or code. Run this in parallel with (or right after) `sourcerer.repos.list` and cross-reference to handle false matches or missed matches.
- `sourcerer.refs.list` lists all indexed refs by repo. Each result row is one indexed ref; the distinct `git.repo` values in the results are the repos available to query. Prefer filtering only by git_org and git_repo, using * wildcards as needed.

## Progressive disclosure - narrow first, expand only as needed

Start with the most specific pattern likely to match the question. Broaden only if the result is empty or you need more coverage.

| Scenario | Example call |
|---|---|
| Specific repo, confident | `git_org: elastic, git_repo: elasticsearch` |
| Repo family in one org | `git_org: elastic, git_repo: elasticsearch*` |
| All repos in one org | `git_org: elastic, git_repo: *` |
| Different org or upstream | `git_org: apache, git_repo: lucene*` |
| Cross-org fallback (expensive) | `git_repo: *` (no org filter) |

Avoid `git_repo: *` without an org filter unless you genuinely need all orgs - it returns every indexed ref and consumes many tokens.

## Interpreting results
From the result rows:
- **Distinct `git.repo` values** - the repos available to query.
- **`git.ref_type`** - whether each repo has branches, tags, or both. Useful for knowing whether `ref-resolution` will find tags or needs to fall back to a branch.
- **`status`** - `complete` means fully indexed; `indexing` means an in-progress snapshot (content may be incomplete — prefer `complete` refs).

## Example: expanding scope mid-conversation
A question about Elasticsearch's query parsing leads into how Lucene's `QueryParser` works underneath:
1. Initial discovery: `sourcerer.repos.list` and `sourcerer.refs.list(git_org: elastic, git_repo: elasticsearch)` - confirm the repo and plan the search.
2. Answer the Elasticsearch side. When the trail leads to Lucene, expand:
3. `refs.list(git_org: apache, git_repo: lucene*)` - discover which Lucene repos are indexed.
4. Use `ref-resolution` on the relevant Lucene repo to pin a commit, then query content.

Each discovery call should be targeted - retrieve only what you need to plan the next step.
