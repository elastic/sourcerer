---
name: "ref-resolution"
description: "Use any time a branch, tag, or version in the user's question is ambiguous or unspecified - whether at the start of a question or mid-conversation (e.g. when comparing behavior across versions)."
---

## When this applies
Apply whenever a ref is ambiguous or unspecified. This includes:
- No ref mentioned (resolve to a sensible default)
- A version range or partial version (e.g. "8.x", "latest 8")
- A relative reference ("before 8.17", "since 8.14")
- A comparison across versions ("how did this change between 8.x and 9.x?")
- A mid-conversation follow-up that introduces a new ref

## Tool
Use `sourcerer.refs.list` to explore available refs. It accepts wildcards on `git_ref`:
- `git_ref: v8.*` - all refs starting with "v8."
- `git_ref: v8.17.*` - all 8.17.x tags
- `git_ref: main` - exact branch match

Combine with `git_ref_type: tag`, `git_ref_type: branch`, or `git_ref_type: commit` (a pinned,
ad-hoc commit not on a tracked branch/tag tip) to narrow further.

## Resolution scenarios

### No ref specified - default to latest stable
1. Call `refs.list` with `git_ref_type: tag` for the repo.
2. Exclude pre-release tags (suffixes like `-rc`, `-beta`, `-alpha`, `-SNAPSHOT`, `-M1`). A pre-release sorts *below* its final release (`v9.0.0 > v9.0.0-rc1`).
3. Pick the highest semver tag, comparing numerically (major → minor → patch). Do not sort lexically, and do not assume `indexed_at DESC` order equals semver order (`v9.0.0 > v8.14.3 > v8.2.0`).
4. Prefer the highest tag whose `status` is `complete` (the `status` field surfaced by `repo-discovery`). If the very latest tag is still indexing (status `indexing`), drop to the next-highest `complete` tag, or proceed but tell the user that release is only partially indexed.
5. If no stable tags exist, fall back to the default branch (`main`, `master`, `trunk`). If only pre-release tags exist, resolve to the highest pre-release and say so explicitly.
6. State the resolved tag or branch at the start of your answer.

### Version range or partial version (e.g. "8.x", "latest 8")
Disambiguate based on context:
- **Single point in time** (the default): resolve to the *latest stable* within the range. Call `refs.list` with `git_ref: v8.*` and `git_ref_type: tag`, then pick the highest stable patch.
- **Comparison or history** (e.g. "how has X evolved across 8.x?"): resolve to *all matching commits*. Collect every stable tag in the range; query each one separately. Label findings clearly by version.

### Comparison across versions (e.g. "8.x vs 9.x", "before and after 8.17")
Resolve each ref independently using the steps above. Run content queries against each commit, then compare results. Label each finding with its version.

### Explicit ref (branch name, exact tag, commit hash)
Use as given. If it is a branch, call `refs.list` with `git_ref_type: branch` to confirm it exists and retrieve its current commit. If it is a tag, confirm and get its commit. If it is a commit hash, use it directly - optionally confirm with `git_ref_type: commit` if it may be a pinned commit rather than one reached via a branch/tag.

### Branch as of a specific date (e.g. "main as it was on 2024-03-01")
When a branch was indexed with `since` (history walk), multiple snapshots of the branch exist —
one per historical commit. Resolve "branch as of date D" like this:
1. Call `refs.list` with `git_ref_type: branch` and `git_ref: <branch>`.
2. From the results, filter to markers with `commit_date <= D` and `status: complete`.
3. Pick the marker with the **latest** `commit_date` among those (the branch state at the closest point on or before D).
4. Use that marker's `git.commit` for content queries.

If only one marker exists for the branch (tip-only indexing, no `since`), state that historical
snapshots are unavailable for that branch.

## Pinning the ref_key
Every content query (`sourcerer.code.*` and `sourcerer.files.*`) takes the same single param,
`git_ref_key`, and runs the identical universal join query underneath
(`WHERE git.ref_key == ?git_ref_key | LOOKUP JOIN sourcerer-refs ON git.ref_key`) regardless of
whether the source is indexed as `snapshot` or `incremental`. Derive it once a ref is resolved:

- **Snapshot** (the default; most tags and one-off branch indexes): resolve the ref to its
  `git.commit` as above, then use that **commit SHA directly** as `git_ref_key` -- for snapshot
  content, `git.ref_key == git.commit`.
- **Incremental** (a branch source configured with `update: incremental` in `sourcerer.yml`;
  `refs.list` surfaces its join doc with `update_mode: incremental` and no separate per-commit
  history): build the `git_ref_key` directly as `{host}~{org}~{repo}~{ref}` (host/org/repo
  lowercased, ref case-preserved, `~`-joined) -- no commit resolution needed, since the key names
  the branch itself and the join always resolves it to the CURRENT indexed commit at query time.

Use the resulting `git_ref_key` in every subsequent content call for that repo and ref, and read
the resolved `git.commit` back from the join for citations. Re-invoke this skill only when the
question introduces a new or additional ref.
