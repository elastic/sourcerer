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
from ...config import RepoConfig, load_config
from ...planner import Marker, plan_repo
from ...progress import Unit
from .git import _commit_date_of, list_remote_ref_names


def _resolve_entry(cfg: RepoConfig) -> list[Unit]:
    """Resolve one RepoConfig into the Units it selects: list the remote branches/tags once
    per kind and keep those matching a selector's version-aware pattern (+ min/max_version).
    Pure network + filtering with no reporter calls, so it is safe to run concurrently.
    ls-remote failures (after retries) are reported to stderr; that ref type contributes
    no units so the run continues with the repos that did resolve."""
    fetched: dict[str, list[str] | None] = {}  # kind -> names (None = ls-remote failed)
    seen: set[tuple[str, str]] = set()
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
                units.append(Unit(org=cfg.org, repo=cfg.repo, ref=prefix, kind=rt))
            continue
        if rt not in fetched:
            fetched[rt] = list_remote_ref_names(
                cfg.org, cfg.repo, "heads" if rt == "branch" else "tags"
            )
        names = fetched[rt]
        if names is None:
            continue  # ls-remote failed for this ref type, skip
        floor = sel.since_version_floor()  # version-based `since: {ref}`, name-only
        for name in names:
            if (rt, name) in seen:
                continue
            v = sel.matches(rt, name)
            if v is None:
                continue
            if floor is not None and v.components < floor:
                continue  # below the since version floor
            seen.add((rt, name))
            units.append(Unit(org=cfg.org, repo=cfg.repo, ref=name, kind=rt))

    failed_kinds = sorted(k for k, v in fetched.items() if v is None)
    if failed_kinds:
        click.echo(
            f"Warning: {cfg.org}/{cfg.repo}: git ls-remote failed for "
            f"{'/'.join(failed_kinds)} after retries — skipped",
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


def _load_config(config_path: str) -> list[RepoConfig]:
    """Load and validate repos.yml into RepoConfig entries (the new `index:` schema, shared
    with `sourcerer prune`). Raises OSError/ValueError/yaml.YAMLError on a malformed file."""
    return load_config(config_path)
