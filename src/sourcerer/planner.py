"""Pure prune planning driven by retain. Given a repo's indexed markers + its
RepoConfig, decide keep/delete/unmanaged. No Elasticsearch here -- unit-testable.

`since` is an index-side inclusion floor and is intentionally ignored here: prune governs
only refs that `match`, via `retain`.
"""

from __future__ import annotations

# Standard packages
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

# App packages
from .config import RepoConfig, Selector
from .version import age_keep, drop_superseded_prereleases, recent_keep, version_keep

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Marker:
    id: str
    ref: str
    ref_type: str
    commit: str
    commit_date: datetime | None
    indexed_at: datetime | None


@dataclass
class Decision:
    marker: Marker
    action: str        # "keep" | "delete" | "unmanaged"
    reason: str
    criteria: tuple[str, ...] = ()  # for action == "delete": which retain criteria excluded it


_LEVEL_TO_PLURAL = {"major": "majors", "minor": "minors", "patch": "patches", "build": "builds"}
_CRITERIA_ORDER = ("age", "count", "version", "prerelease")


def _describe(sel: Selector) -> str:
    pa = sel.retain
    if pa is None:
        return f"{sel.ref_type} {sel.raw_patterns} (keep forever)"
    bits = []
    if pa.version is not None:
        bits.append("version(" + ",".join(f"{_LEVEL_TO_PLURAL[k]}:{v}"
                                           for k, v in pa.version.counts.items()) + ")")
    if pa.prerelease == "superseded":
        bits.append("drop-superseded-pre")
    if pa.count is not None:
        bits.append(f"count:{pa.count}")
    if pa.age is not None:
        bits.append(f"age:{int(pa.age.total_seconds() // 86400)}d")
    return f"{sel.ref_type} {sel.raw_patterns} retain[{' or '.join(bits)}]"


def _selector_keep_ids(
    sel: Selector, cohort, now: datetime, date_independent_only: bool = False,
) -> tuple[set[str], dict[str, set[str]]]:
    """Marker ids this selector protects over its own cohort, plus -- for every marker it does
    NOT protect -- which criterion name(s) ("age"/"count"/"version"/"prerelease") excluded it.
    retain fires on ANY criterion, i.e. a marker is kept only if EVERY criterion keeps it
    (intersection); the exclusion map records which criterion in particular failed, for
    prune's WHY report (commands/prune.py's `policy:<criteria>` tag).

    Grouping: version/prerelease/age evaluate over the whole matched family (a version
    landscape; age is per-marker so grouping is moot). `count` is per ref-name for branches
    -- "keep the newest N commits OF THAT BRANCH" -- but pooled across the family for tags,
    where each tag name is one distinct release point."""
    ids = {m.id for m, _ in cohort}
    exclusions: dict[str, set[str]] = defaultdict(set)
    pa = sel.retain
    if pa is None or pa.is_empty():
        return ids, exclusions  # keep forever
    keep = set(ids)

    def apply(name: str, criterion_keep: set[str]) -> None:
        nonlocal keep
        keep &= criterion_keep
        for mid in ids - criterion_keep:
            exclusions[mid].add(name)

    versions = [v for _, v in cohort]
    if pa.version is not None and pa.version.counts:
        survivors = version_keep(versions, pa.version.counts, sel.levels)
        apply("version", {m.id for m, v in cohort if v in survivors})
    if pa.prerelease == "superseded":
        survivors = drop_superseded_prereleases(versions)
        apply("prerelease", {m.id for m, v in cohort if v in survivors})
    if pa.count is not None and not date_independent_only:
        if sel.ref_type == "branch":
            groups: dict = defaultdict(list)
            for m, _ in cohort:
                groups[m.ref].append(m)
            crit_keep = set()
            for group in groups.values():
                crit_keep |= recent_keep(((m.id, m.commit_date or _EPOCH) for m in group), pa.count)
        else:
            crit_keep = recent_keep(((m.id, m.commit_date or _EPOCH) for m, _ in cohort), pa.count)
        apply("count", crit_keep)
    if pa.age is not None and not date_independent_only:
        apply("age", age_keep(((m.id, m.commit_date or _EPOCH) for m, _ in cohort), pa.age, now))
    return keep, exclusions


def plan_repo(markers: list[Marker], cfg: RepoConfig, now: datetime | None = None,
              date_independent_only: bool = False) -> list[Decision]:
    """A marker is a candidate if >=1 selector matches. Across selectors keeps union
    (protected if ANY keeps it, so a keep-forever selector is an allowlist); a candidate is
    deleted only when every matching selector deletes it. Unmatched markers are 'unmanaged'."""
    now = now or datetime.now(timezone.utc)
    cohorts = {id(s): [] for s in cfg.selectors}
    matched = {m.id: [] for m in markers}
    for m in markers:
        for sel in cfg.selectors:
            v = sel.matches(m.ref_type, m.ref)
            if v is not None:
                cohorts[id(sel)].append((m, v))
                matched[m.id].append(sel)
    results = {id(s): _selector_keep_ids(s, cohorts[id(s)], now, date_independent_only)
               for s in cfg.selectors}

    out = []
    for m in markers:
        sels = matched[m.id]
        if not sels:
            out.append(Decision(m, "unmanaged", "matches no selector"))
            continue
        protectors = [s for s in sels if m.id in results[id(s)][0]]
        if protectors:
            out.append(Decision(m, "keep", _describe(protectors[0])))
        else:
            failed = {c for s in sels for c in results[id(s)][1].get(m.id, ())}
            criteria = tuple(c for c in _CRITERIA_ORDER if c in failed)
            out.append(Decision(m, "delete", _describe(sels[0]), criteria))
    return out


def content_delete_set(decisions: list[Decision]) -> set[str]:
    """Commits safe to drop: referenced by a deleted marker and by no surviving marker."""
    kept = {d.marker.commit for d in decisions if d.action != "delete"}
    deleted = {d.marker.commit for d in decisions if d.action == "delete"}
    return deleted - kept


# --- Orphan detection -------------------------------------------------------------------
#
# Retention (above) only ever touches refs a repo's config *selects*. Anything outside that
# reach -- a repo dropped from the config entirely, a marker deleted by hand, content left
# behind by an interrupted run whose marker never got written -- is never visited by
# plan_repo/content_delete_set. The functions below are a second, independent sweep: given
# just the physical index names, the (org, repo, commit) tuples recorded in sourcerer-v1-refs,
# and the (org, repo, commit) tuples with actual content docs, they find every one of those
# leftovers. Still pure/no-ES, so a fake index-name list and a couple of tuple sets are enough
# to exercise every case in a test.
#
# Three orphan classes, in the descending-coarseness order deletion should follow (org before
# org~repo before org~repo~commit; whole-index DELETEs before any delete_by_query):
#   A. orphan index    -- a physical files/lines index whose org (or org~repo, or
#                          org~repo~commit) has no corresponding entry in refs at all.
#                          -> DELETE {index} (near-instant).
#   B. orphan content  -- a commit with content docs in a *live* org~repo index, but no
#                          marker in refs references it (manual marker deletion, a config
#                          entry removed, etc). -> delete_by_query on the content index.
#   C. orphan marker   -- a commit marker in refs with no content docs at all in either
#                          content index (content manually deleted, or a whole content
#                          index/repo/org vanished without its markers being cleaned up).
#                          -> delete_by_query on sourcerer-v1-refs.
#
# Today the CLI only ever produces org~repo-granularity indices (see files_index/lines_index
# in commands/index.py); parse_index_name and orphan_indices also recognize the org-only and
# org~repo~commit levels so detection keeps working if a future granularity is introduced,
# even though only one level is exercised in practice right now.

_FILES_PREFIX_DEFAULT = "sourcerer-v1-files"
_LINES_PREFIX_DEFAULT = "sourcerer-v1-lines"


@dataclass(frozen=True)
class ParsedIndex:
    kind: str            # "files" | "lines"
    org: str
    repo: str | None     # None for an org-only index
    commit: str | None   # set only for an org~repo~commit index
    name: str             # the original index name, for reporting/deletion


def parse_index_name(
    name: str,
    files_prefix: str = _FILES_PREFIX_DEFAULT,
    lines_prefix: str = _LINES_PREFIX_DEFAULT,
) -> ParsedIndex | None:
    """Inverse of files_index()/lines_index() (commands/index.py), extended to also recognize
    the org-only and org~repo~commit granularities those builders don't produce today. Returns
    None for anything that doesn't fit the scheme (sourcerer-v1-refs, an unrelated index, or a
    malformed/empty segment) so callers skip it rather than risk misclassifying it as an orphan."""
    for prefix, kind in ((files_prefix, "files"), (lines_prefix, "lines")):
        if not name.startswith(prefix + "~"):
            continue
        parts = name[len(prefix) + 1:].split("~")
        if not (1 <= len(parts) <= 3) or not all(parts):
            return None
        org = parts[0]
        repo = parts[1] if len(parts) >= 2 else None
        commit = parts[2] if len(parts) == 3 else None
        return ParsedIndex(kind=kind, org=org, repo=repo, commit=commit, name=name)
    return None


def orphan_indices(
    index_names: list[str],
    ref_orgs: set[str],
    ref_repos: set[tuple[str, str]],
    ref_commits: set[tuple[str, str, str]],
) -> list[str]:
    """Class-A orphans: physical files/lines indices at any granularity whose identity has no
    matching entry in refs. Subsumption: once an org's index is orphaned, its org~repo and
    org~repo~commit siblings are skipped (they're going away with the coarser DELETE); once an
    org~repo index is orphaned, its org~repo~commit children are skipped the same way.

    Subsumption is scoped per index *kind* (files vs. lines): they are separate physical
    indices with independent lifecycles (e.g. a repo can have already migrated its `files`
    index to org~repo granularity while `lines` still has legacy org~repo~commit indices per
    commit -- deleting the orphaned `files~org~repo` index does nothing to the `lines`
    indices, so they must still be judged and deleted on their own). ref_orgs/ref_repos/
    ref_commits are kind-agnostic (a ref marker doesn't distinguish files from lines), so only
    the subsumption bookkeeping needs the kind; the orphan test itself does not.

    The returned order is org, then org~repo, then org~repo~commit -- descending coarseness,
    the order deletion should proceed in."""
    parsed = [p for p in (parse_index_name(n) for n in index_names) if p is not None]

    orphans: list[str] = []
    orphan_kind_orgs: set[tuple[str, str]] = set()            # (kind, org)
    orphan_kind_repos: set[tuple[str, str, str]] = set()      # (kind, org, repo)

    for p in parsed:
        if p.repo is None and p.org not in ref_orgs:
            orphans.append(p.name)
            orphan_kind_orgs.add((p.kind, p.org))

    for p in parsed:
        if p.repo is not None and p.commit is None and (p.kind, p.org) not in orphan_kind_orgs:
            if (p.org, p.repo) not in ref_repos:
                orphans.append(p.name)
                orphan_kind_repos.add((p.kind, p.org, p.repo))

    for p in parsed:
        if (
            p.commit is not None
            and (p.kind, p.org) not in orphan_kind_orgs
            and (p.kind, p.org, p.repo) not in orphan_kind_repos
        ):
            if (p.org, p.repo, p.commit) not in ref_commits:
                orphans.append(p.name)

    return orphans


def orphan_content_commits(
    content_commits_by_repo: dict[tuple[str, str], set[str]],
    ref_commits_by_repo: dict[tuple[str, str], set[str]],
    skip_repos: set[tuple[str, str]],
) -> dict[tuple[str, str], set[str]]:
    """Class-B orphans: commits with content docs in a live org~repo index but no marker
    referencing them. `skip_repos` excludes repos whose whole index is already a Class-A
    orphan -- their content goes away with the index DELETE, not a separate delete_by_query."""
    out: dict[tuple[str, str], set[str]] = {}
    for repo_key, commits in content_commits_by_repo.items():
        if repo_key in skip_repos:
            continue
        orphaned = commits - ref_commits_by_repo.get(repo_key, set())
        if orphaned:
            out[repo_key] = orphaned
    return out


def orphan_markers(
    ref_commits_by_repo: dict[tuple[str, str], set[str]],
    content_commits_by_repo: dict[tuple[str, str], set[str]],
    skip_repos: set[tuple[str, str]],
) -> dict[tuple[str, str], set[str]]:
    """Class-C orphans: commit markers in refs with no content docs in either content index.
    This single per-commit check also covers a whole repo/org's content having vanished
    entirely (every one of that repo's ref commits shows up as "missing"), so it subsumes what
    would otherwise be separate org- and repo-level refs sweeps -- one combined delete_by_query
    against sourcerer-v1-refs handles all of it. `skip_repos` excludes repos already going away
    via a Class-A index DELETE (their markers are dropped as part of that, not here)."""
    out: dict[tuple[str, str], set[str]] = {}
    for repo_key, commits in ref_commits_by_repo.items():
        if repo_key in skip_repos:
            continue
        missing = commits - content_commits_by_repo.get(repo_key, set())
        if missing:
            out[repo_key] = missing
    return out


@dataclass
class OrphanPlan:
    orphan_index_names: list[str]                        # Class A -> DELETE {index}
    orphan_content: dict[tuple[str, str], set[str]]       # Class B -> delete_by_query on content
    orphan_marker_commits: dict[tuple[str, str], set[str]]  # Class C -> delete_by_query on refs


def plan_orphans(
    index_names: list[str],
    ref_commit_tuples: set[tuple[str, str, str]],
    content_commit_tuples: set[tuple[str, str, str]],
) -> OrphanPlan:
    """Combine the three orphan classes into one plan from three cheap snapshots: the physical
    index names, the distinct (org, repo, commit) tuples in refs, and the distinct (org, repo,
    commit) tuples with content docs (already unioned across the files and lines indices
    present). Pure -- no ES calls -- so this is the one seam orphan-sweep tests need to hit."""
    ref_orgs = {org for org, _, _ in ref_commit_tuples}
    ref_repos = {(org, repo) for org, repo, _ in ref_commit_tuples}

    orphan_index_names = orphan_indices(index_names, ref_orgs, ref_repos, ref_commit_tuples)

    orphaned_names = set(orphan_index_names)
    skip_repos = {
        (p.org, p.repo)
        for p in (parse_index_name(n) for n in index_names)
        if p is not None and p.repo is not None and p.commit is None and p.name in orphaned_names
    }

    content_by_repo: dict[tuple[str, str], set[str]] = defaultdict(set)
    for org, repo, commit in content_commit_tuples:
        content_by_repo[(org, repo)].add(commit)
    ref_by_repo: dict[tuple[str, str], set[str]] = defaultdict(set)
    for org, repo, commit in ref_commit_tuples:
        ref_by_repo[(org, repo)].add(commit)

    orphan_content = orphan_content_commits(content_by_repo, ref_by_repo, skip_repos)
    orphan_marker_commits = orphan_markers(ref_by_repo, content_by_repo, skip_repos)

    return OrphanPlan(orphan_index_names, orphan_content, orphan_marker_commits)