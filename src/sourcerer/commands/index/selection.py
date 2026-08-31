# sourcerer/commands/index/selection.py
# Turns repos.yml selectors into concrete Units to index: lists a repo's remote branches/tags
# once per ref kind and keeps the ones a selector's version-aware pattern matches, then resolves
# each selector's `since` floor to a commit-date lower bound. A `commit` selector pins explicit
# SHAs/prefixes instead -- there's nothing to list remotely, so it's turned into Units directly.

# Standard packages
import datetime
import pathlib

# Third-party packages
import click

# App packages
from ...config import Config, RepoConfig, load_config
from ...hosts import Host
from ...planner import Marker, plan_repo
from ...progress import Unit
from ...version import match_version
from .git import _commit_date_of, list_remote_ref_names, list_remote_refs


def _resolve_entry(cfg: RepoConfig, host: Host) -> list[Unit]:
    """Resolve one RepoConfig into the Units it selects: list the remote branches/tags once
    per kind and keep those matching a selector's version-aware pattern (+ min/max_version).
    Pure network + filtering with no reporter calls, so it is safe to run concurrently.
    ls-remote failures (after retries) are reported to stderr; that ref type contributes
    no units so the run continues with the repos that did resolve. `host` supplies the clone
    URL and is carried on every emitted Unit."""
    clone_url = host.clone_url(cfg.org, cfg.repo)
    # Maps ref kind ("branch"/"tag") -> {short_name: commit_sha} or None on ls-remote failure.
    # Using list_remote_refs (vs. list_remote_ref_names) so we capture each ref's commit SHA
    # here in Phase 1 and can stash it on the Unit, avoiding a second per-ref ls-remote in
    # Phase 2's pre-clone skip check.
    fetched: dict[str, dict[str, str] | None] = {}
    seen: set[tuple[str, str]] = set()
    # Maps (ref_type, name) -> mode for the winning selector, so we can detect when a second
    # selector of a DIFFERENT mode also claims the same ref. Mixed-mode refs are unsafe: the
    # incremental LOOKUP JOIN ON git.ref requires exactly one refs doc per (host,org,repo,ref),
    # but a snapshot marker and an incremental join doc would both be present (fan-out).
    seen_mode: dict[tuple[str, str], str] = {}
    mode_conflicts: list[tuple[str, str, str, str]] = []  # (ref_type, name, mode_a, mode_b)
    units: list[Unit] = []
    for sel in cfg.selectors:
        rt = sel.ref_type
        if rt == "commit":
            # Pinned commits aren't enumerable via ls-remote (there's no remote listing of
            # commits) -- `match` already holds the literal SHA/prefix strings to index, one
            # Unit per pattern. checkout_ref resolves the (possibly short) SHA at clone time.
            for prefix in sel.raw_patterns:
                if (rt, prefix) in seen:
                    continue
                seen.add((rt, prefix))
                seen_mode[(rt, prefix)] = sel.mode
                units.append(Unit(
                    host=cfg.host, org=cfg.org, repo=cfg.repo, ref=prefix, kind=rt,
                    index_level=sel.index_level, index_suffix=sel.index_suffix,
                    mode=sel.mode, ref_pattern=prefix,
                ))
            continue
        if rt not in fetched:
            fetched[rt] = list_remote_refs(clone_url, "heads" if rt == "branch" else "tags")
        ref_map = fetched[rt]
        if ref_map is None:
            continue  # ls-remote failed for this ref type, skip
        floor = sel.since_version_floor()  # version-based `since: {ref}`, name-only

        # Delta-mode tag selectors are "moving streams": each raw pattern string is a single
        # logical stream whose identity is the pattern itself (not any concrete tag name). One
        # stream unit is emitted per raw pattern that matches at least one remote tag; the
        # concrete newest-committed tag is resolved post-clone (ls-remote lacks dates). The
        # per-name loop below handles all other cases (snapshot tags, all branches).
        if sel.mode == "delta" and rt == "tag":
            for pattern, cp in zip(sel.raw_patterns, sel.compiled):
                key = (rt, pattern)
                # Only emit a stream unit if this specific pattern matches at least one remote tag.
                has_match = any(match_version(cp, name) is not None for name in ref_map)
                if not has_match:
                    continue  # pattern matches nothing remotely -- no stream to emit
                if key in seen:
                    prior_mode = seen_mode[key]
                    if prior_mode != sel.mode:
                        mode_conflicts.append((rt, pattern, prior_mode, sel.mode))
                    continue
                seen.add(key)
                seen_mode[key] = sel.mode
                units.append(Unit(
                    host=cfg.host, org=cfg.org, repo=cfg.repo, ref=pattern, kind=rt,
                    remote_sha=None,  # resolved post-clone via ref_dates
                    index_level=sel.index_level, index_suffix=sel.index_suffix,
                    mode=sel.mode, ref_pattern=pattern,
                ))
            continue

        for name in sorted(ref_map):
            matched = sel.match_pattern(rt, name)
            if matched is None:
                continue
            pattern, v = matched
            if floor is not None and v.components < floor:
                continue  # below the since version floor
            if not sel.range_admits(v):
                continue  # outside this selector's retain.version.range -- let a sibling claim it
            key = (rt, name)
            if key in seen:
                # Already claimed by an earlier selector: check for a mode conflict.
                prior_mode = seen_mode[key]
                if prior_mode != sel.mode:
                    mode_conflicts.append((rt, name, prior_mode, sel.mode))
                continue
            seen.add(key)
            seen_mode[key] = sel.mode
            units.append(Unit(
                host=cfg.host, org=cfg.org, repo=cfg.repo, ref=name, kind=rt,
                remote_sha=ref_map[name],
                # A version-templated index.suffix ("{major}.{minor}.x") is rendered here, from
                # this ref's own version, so the Unit -- and everything it feeds -- carries a
                # concrete suffix ("9.5.x"). The other Unit sites above are commit sources and
                # delta streams, which config parsing forbids from using variables.
                index_level=sel.index_level, index_suffix=sel.resolve_index_suffix(v),
                mode=sel.mode, ref_pattern=pattern,
            ))

    if mode_conflicts:
        conflicts_str = ", ".join(
            f"{rt}/{name} ({mode_a} vs {mode_b})"
            for rt, name, mode_a, mode_b in mode_conflicts
        )
        click.echo(
            f"Warning: {cfg.org}/{cfg.repo}: selectors claim the same ref(s) with different "
            f"modes -- skipping all units for this repo to avoid fan-out: {conflicts_str}",
            err=True,
        )
        return []

    failed_kinds = sorted(k for k, v in fetched.items() if v is None)
    if failed_kinds:
        click.echo(
            f"Warning: {cfg.org}/{cfg.repo}: git ls-remote failed for "
            f"{'/'.join(failed_kinds)} after retries - skipped",
            err=True,
        )

    # Drop refs that retain would delete on version/prerelease grounds alone. These are
    # name-only (no commit dates), so applying them here shrinks the plan itself -- the user
    # sees the true set, and we skip the clone/skip-check churn on doomed refs. count/age/since
    # are date-based and refined post-clone (see command.run_config's process_group). Uses the
    # same planner as `sourcerer prune`, so cross-selector keeps (e.g. a keep-forever selector)
    # still win.
    needs_filter = any(
        sel.retain is not None
        and (sel.retain.version is not None or sel.retain.prerelease == "superseded")
        for sel in cfg.selectors
    )
    if needs_filter and units:
        markers = [
            Marker(id=f"{u.kind}:{u.ref}", ref=u.ref, ref_type=u.kind,
                   commit="", commit_date=None, indexed_at=None)
            for u in units
        ]
        doomed = {
            d.marker.id for d in plan_repo(markers, cfg, date_independent_only=True)
            if d.action == "delete"
        }
        units = [u for u in units if f"{u.kind}:{u.ref}" not in doomed]
    return units


def _resolve_since_floor(since, repo_dir: pathlib.Path, now: datetime.datetime) -> datetime.datetime | None:
    """One selector's `since` -> a commit-date lower bound. age/date resolve directly;
    ref/commit resolve to the committer date of their commit in the local clone."""
    if since.kind == "age":
        return now - since.value            # timedelta
    if since.kind == "date":
        return since.value                  # datetime
    return _commit_date_of(repo_dir, str(since.value))  # ref | commit


def _effective_since_floor(
    cfg: RepoConfig | None, repo_dir: pathlib.Path, ref_type: str, ref: str, now: datetime.datetime,
) -> datetime.datetime | None:
    """The floor a ref must clear to be indexed, or None if unconstrained. A ref can match
    several selectors; inclusion is a union, so a matching selector with no `since` (or the
    most permissive floor) wins. An unresolvable anchor fails open (doesn't exclude)."""
    if cfg is None:
        return None
    floors: list[datetime.datetime] = []
    for sel in cfg.selectors:
        if sel.matches(ref_type, ref) is None:
            continue
        # No since, or a version-based `since: {ref}` already enforced in Phase 1 -> this
        # selector imposes no post-clone date floor, and inclusion is a union, so it wins.
        if sel.since is None or sel.since_version_floor() is not None:
            return None
        floor = _resolve_since_floor(sel.since, repo_dir, now)
        if floor is None:
            return None
        floors.append(floor)
    return min(floors) if floors else None


def _load_config(config_path: str) -> Config:
    """Load and validate sourcerer.yml into a Config (resolved hosts + RepoConfig entries),
    shared with `sourcerer prune`. Raises OSError/ValueError/yaml.YAMLError on a malformed
    file."""
    return load_config(config_path)
