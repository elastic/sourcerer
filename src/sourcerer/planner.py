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


_LEVEL_TO_PLURAL = {"major": "majors", "minor": "minors", "patch": "patches", "build": "builds"}


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


def _selector_keep_ids(sel: Selector, cohort, now: datetime, date_independent_only: bool = False) -> set[str]:
    """Marker ids this selector protects over its own cohort. retain fires on ANY
    criterion, i.e. a marker is kept only if EVERY criterion keeps it (intersection).

    Grouping: version/prerelease/age evaluate over the whole matched family (a version
    landscape; age is per-marker so grouping is moot). `count` is per ref-name for branches
    -- "keep the newest N commits OF THAT BRANCH" -- but pooled across the family for tags,
    where each tag name is one distinct release point."""
    ids = {m.id for m, _ in cohort}
    pa = sel.retain
    if pa is None or pa.is_empty():
        return ids  # keep forever
    keep = set(ids)
    versions = [v for _, v in cohort]
    if pa.version is not None and pa.version.counts:
        survivors = version_keep(versions, pa.version.counts, sel.levels)
        keep &= {m.id for m, v in cohort if v in survivors}
    if pa.prerelease == "superseded":
        survivors = drop_superseded_prereleases(versions)
        keep &= {m.id for m, v in cohort if v in survivors}
    if pa.count is not None and not date_independent_only:
        if sel.ref_type == "branch":
            groups: dict = defaultdict(list)
            for m, _ in cohort:
                groups[m.ref].append(m)
            kept = set()
            for group in groups.values():
                kept |= recent_keep(((m.id, m.commit_date or _EPOCH) for m in group), pa.count)
            keep &= kept
        else:
            keep &= recent_keep(((m.id, m.commit_date or _EPOCH) for m, _ in cohort), pa.count)
    if pa.age is not None and not date_independent_only:
        keep &= age_keep(((m.id, m.commit_date or _EPOCH) for m, _ in cohort), pa.age, now)
    return keep


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
    keep_ids = {id(s): _selector_keep_ids(s, cohorts[id(s)], now, date_independent_only)
                for s in cfg.selectors}

    out = []
    for m in markers:
        sels = matched[m.id]
        if not sels:
            out.append(Decision(m, "unmanaged", "matches no selector"))
            continue
        protectors = [s for s in sels if m.id in keep_ids[id(s)]]
        if protectors:
            out.append(Decision(m, "keep", _describe(protectors[0])))
        else:
            out.append(Decision(m, "delete", _describe(sels[0])))
    return out


def content_delete_set(decisions: list[Decision]) -> set[str]:
    """Commits safe to drop: referenced by a deleted marker and by no surviving marker."""
    kept = {d.marker.commit for d in decisions if d.action != "delete"}
    deleted = {d.marker.commit for d in decisions if d.action == "delete"}
    return deleted - kept