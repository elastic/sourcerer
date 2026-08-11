# sourcerer/commands/prune/command.py
# `sourcerer prune --config repos.yml [--dry-run]`
# `sourcerer prune <host>/<org>/<repo> -b/-t/-c [--dry-run]`
# Deletes indexed refs that fall outside their repos.yml retention policies (or a single
# explicitly-named ref). The orphan sweep runs for the --config and no-arg paths (run()), but
# NOT for a single-ref REPO_SPEC prune (run_ref()) -- that path is strictly scoped to the
# named ref. Row/plan construction and printing live in report.py; the deletions themselves
# (retention and orphan sweep) live in execute.py. This file is the entry point that wires
# the two together.

# Standard packages
import sys
from datetime import datetime, timezone

# Third-party packages
import click
import yaml

# App packages
from ...config import load_config
from ...indices import REFS_INDEX
from ...planner import Decision, Marker, plan_repo
from ...queries import fetch_markers, resolve_content_commit
from ...utils import ES_ERRORS, make_client
from .execute import delete_commit_content, execute_deletions, execute_orphan_deletions, plan_orphans_now
from .report import _Row, _orphan_rows, _print, _ref_rows, _retention_rows


def _plan_orphans(es, rows: list[_Row], failures_ref: list[int]):
    """Refresh refs, run the orphan-sweep plan pass, extend rows with orphan report rows, and
    return the OrphanPlan (or None on error). Read-only: no deletions."""
    try:
        es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)
        orphan_plan = plan_orphans_now(es)
    except ES_ERRORS as e:
        failures_ref[0] += 1
        click.echo(f"error scanning for orphans: {e}", err=True)
        return None
    rows.extend(_orphan_rows(orphan_plan))
    return orphan_plan


def _apply_orphan_plan(es, orphan_plan, failures_ref: list[int]) -> tuple[int, int, int]:
    """Apply an OrphanPlan returned by _plan_orphans. Returns (indices, content, markers)
    counts. No-op if the plan has nothing to delete."""
    has_orphans = bool(
        orphan_plan.orphan_index_names or orphan_plan.orphan_content or orphan_plan.orphan_marker_commits
    )
    if not has_orphans:
        return (0, 0, 0)
    try:
        return execute_orphan_deletions(es, orphan_plan)
    except ES_ERRORS as e:
        failures_ref[0] += 1
        click.echo(f"error deleting orphans: {e}", err=True)
        return (0, 0, 0)


def run(config_path=None, url=None, api_key=None, username=None, password=None,
        dry_run=False, quiet=False, insecure=False) -> None:
    """--config is optional: the retention pass has nothing to plan without one, but the
    orphan sweep below doesn't depend on any config and always runs."""
    entries = []
    if config_path:
        try:
            entries = load_config(config_path).repos
        except (OSError, ValueError, yaml.YAMLError) as e:
            click.echo(f"Error: invalid config: {e}", err=True)
            sys.exit(1)

    es = make_client(url, api_key, username, password, insecure=insecure)
    # refs is tiny; refresh once so planning sees the latest markers.
    es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)
    now = datetime.now(timezone.utc)
    failures = [0]
    total_markers = 0
    total_commits = 0
    rows: list[_Row] = []
    repo_decisions = []  # (cfg, decisions) for repos that planned successfully

    for cfg in entries:
        try:
            markers = fetch_markers(es, cfg.host, cfg.org, cfg.repo)
            decisions = plan_repo(markers, cfg, now)
        except ES_ERRORS as e:
            failures[0] += 1
            click.echo(f"{cfg.host}/{cfg.org}/{cfg.repo}: error planning: {e}", err=True)
            continue
        rows.extend(_retention_rows(cfg, decisions))
        repo_decisions.append((cfg, decisions))

    # Orphan sweep: independent of any one config entry -- it targets indices/markers with no
    # config presence at all (a repo dropped from the config, a marker deleted by hand, content
    # left behind by an interrupted run). Refresh again first so it sees the marker deletes the
    # retention pass above just made (those were fired with refresh=False).
    orphan_plan = _plan_orphans(es, rows, failures)

    if not quiet or dry_run:
        _print(rows)

    total_orphan_indices = total_orphan_content = total_orphan_markers = 0
    if not dry_run:
        for cfg, decisions in repo_decisions:
            if any(d.action == "delete" for d in decisions):
                try:
                    n_markers, n_commits = execute_deletions(es, cfg.host, cfg.org, cfg.repo, decisions)
                    total_markers += n_markers
                    total_commits += n_commits
                except ES_ERRORS as e:
                    failures[0] += 1
                    click.echo(f"{cfg.host}/{cfg.org}/{cfg.repo}: error deleting: {e}", err=True)

        if orphan_plan is not None:
            total_orphan_indices, total_orphan_content, total_orphan_markers = _apply_orphan_plan(
                es, orphan_plan, failures
            )

    if dry_run:
        click.echo("Dry run: no changes made.")
    elif not quiet:
        click.echo(
            f"Pruned {total_markers} marker(s) and {total_commits} commit(s) of content; "
            f"removed {total_orphan_indices} orphaned index(es), "
            f"{total_orphan_content} orphaned content commit(s), "
            f"{total_orphan_markers} orphaned marker commit(s)."
        )
    if failures[0]:
        click.echo(f"Completed with {failures[0]} failure(s)", err=True)
        sys.exit(1)


def run_ref(
    repo_spec: str,
    branch: str | None,
    tag: str | None,
    commit: str | None,
    url=None,
    api_key=None,
    username=None,
    password=None,
    dry_run: bool = False,
    quiet: bool = False,
    insecure: bool = False,
) -> None:
    """Prune a single, explicitly-named ref (branch/tag/commit) from the index. Fetches ALL
    markers for the repo so the commit-safety guard (content_delete_set) sees every surviving
    ref before deciding whether to drop content. Only the targeted ref (and any content
    exclusively owned by it) is deleted -- no orphan sweep is performed."""
    parts = repo_spec.split("/", 2)
    if len(parts) != 3 or not all(parts):
        click.echo(f"Error: repo_spec must be '<host>/<org>/<repo>', got: {repo_spec!r}", err=True)
        sys.exit(1)
    host, org, repo = parts

    # Derive ref_type and the canonical ref name from the single provided flag.
    if branch:
        ref_type, ref = "branch", branch
    elif tag:
        ref_type, ref = "tag", tag
    else:
        ref_type, ref = "commit", commit

    es = make_client(url, api_key, username, password, insecure=insecure)
    es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)

    try:
        # Fetch ALL markers for the repo -- the safety guard requires the full set.
        markers = fetch_markers(es, host, org, repo)
    except ES_ERRORS as e:
        click.echo(f"{host}/{org}/{repo}: error fetching markers: {e}", err=True)
        sys.exit(1)

    # Build decisions: mark only the targeted ref as delete; keep everything else.
    # For commit refs, match by prefix (>=7 hex chars) against the stored full marker.commit
    # value -- across ALL ref types (branch/tag/commit).  This lets `-c <sha>` target any
    # marker (regardless of how it was indexed) that sits on that commit.  Reject if the prefix
    # resolves to >1 distinct SHA (ambiguous), counting distinct commit SHAs rather than marker
    # count so that a branch + tag legitimately sharing one SHA doesn't false-trigger.
    def _matches_commit(marker: Marker, target: str) -> bool:
        return marker.commit.startswith(target)

    decisions = []
    for m in markers:
        if ref_type != "commit":
            hit = m.ref_type == ref_type and m.ref == ref
        else:
            hit = _matches_commit(m, ref)
        decisions.append(Decision(m, "delete" if hit else "keep", "explicit" if hit else "not targeted"))

    target_decisions = [d for d in decisions if d.action == "delete"]
    if ref_type == "commit" and target_decisions:
        distinct_shas = {d.marker.commit for d in target_decisions}
        if len(distinct_shas) > 1:
            matches = ", ".join(sorted(distinct_shas))
            click.echo(
                f"Error: ambiguous commit prefix {ref!r} matches {len(distinct_shas)} distinct commits "
                f"({matches}); use the full SHA.", err=True
            )
            sys.exit(1)

    rows: list[_Row] = []
    failures = [0]
    total_markers = total_commits = 0

    if not target_decisions:
        if ref_type != "commit":
            # Branch/tag not found in index — nothing to do.
            if not quiet:
                click.echo(f"{host}/{org}/{repo}@{ref} (ref_type={ref_type}): not indexed, nothing to prune.")
            return

        # -c <sha> with no matching marker: the commit may still have content docs (e.g. an old
        # commit a branch has moved past).  Resolve the prefix to a concrete SHA via the content
        # indices, then delete the content directly if found.
        try:
            resolved = resolve_content_commit(es, host, org, repo, ref)
        except ES_ERRORS as e:
            click.echo(f"{host}/{org}/{repo}: error resolving commit in content indices: {e}", err=True)
            sys.exit(1)

        if not resolved:
            if not quiet:
                click.echo(f"{host}/{org}/{repo}@{ref}: not indexed, nothing to prune.")
            return

        if len(resolved) > 1:
            matches = ", ".join(sorted(resolved))
            click.echo(
                f"Error: ambiguous commit prefix {ref!r} matches {len(resolved)} distinct commits "
                f"in content indices ({matches}); use the full SHA.", err=True
            )
            sys.exit(1)

        # Exactly one content commit matches — content-only path (no marker to delete).
        (sha,) = resolved
        rows.append(_Row(f"{host}/{org}/{repo}@{sha}", "content-only"))

        if not quiet or dry_run:
            _print(rows)

        if not dry_run:
            try:
                delete_commit_content(es, host, org, repo, sha)
                total_commits = 1
            except ES_ERRORS as e:
                failures[0] += 1
                click.echo(f"{host}/{org}/{repo}: error deleting content: {e}", err=True)

        if dry_run:
            click.echo("Dry run: no changes made.")
        elif not quiet:
            click.echo(f"Pruned 0 marker(s) and {total_commits} commit(s) of content.")
        if failures[0]:
            click.echo(f"Completed with {failures[0]} failure(s)", err=True)
            sys.exit(1)
        return

    rows.extend(_ref_rows(host, org, repo, decisions))

    if not quiet or dry_run:
        _print(rows)

    if not dry_run:
        try:
            total_markers, total_commits = execute_deletions(es, host, org, repo, decisions)
        except ES_ERRORS as e:
            failures[0] += 1
            click.echo(f"{host}/{org}/{repo}: error deleting: {e}", err=True)

    if dry_run:
        click.echo("Dry run: no changes made.")
    elif not quiet:
        click.echo(
            f"Pruned {total_markers} marker(s) and {total_commits} commit(s) of content."
        )
    if failures[0]:
        click.echo(f"Completed with {failures[0]} failure(s)", err=True)
        sys.exit(1)
