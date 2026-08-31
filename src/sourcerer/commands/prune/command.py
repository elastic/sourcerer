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
from ...progress import make_prune_reporter
from ...queries import content_indices_for_commit, fetch_markers, resolve_content_commit
from ...utils import ES_ERRORS, make_client
from .execute import (delete_commit_content, execute_deletions, execute_orphan_deletions,
                      execute_stale_marker_deletions, plan_orphans_now, wait_for_deletions)
from .report import _Row, _orphan_rows, _print, _ref_rows, _retention_rows


def _plan_orphans(es, reporter, failures_ref: list[int], host=None, org=None, repo=None):
    """Refresh refs and run the orphan-sweep plan pass. Returns the OrphanPlan (or None on
    error). Read-only: no deletions."""
    try:
        es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)
        with reporter:
            return plan_orphans_now(es, reporter=reporter, host=host, org=org, repo=repo)
    except ES_ERRORS as e:
        failures_ref[0] += 1
        click.echo(f"error scanning for orphans: {e}", err=True)
        return None


def _apply_orphan_plan(es, orphan_plan, failures_ref: list[int]) -> tuple[int, int, int, int, int, list[str]]:
    """Apply an OrphanPlan returned by _plan_orphans. Returns (indices, content, markers, stale,
    empty, submitted_task_ids). No-op if the plan has nothing to delete."""
    has_orphans = bool(
        orphan_plan.orphan_index_names or orphan_plan.orphan_content
        or orphan_plan.orphan_marker_commits or orphan_plan.orphan_stale
        or orphan_plan.empty_index_names
    )
    if not has_orphans:
        return (0, 0, 0, 0, 0, [])
    try:
        return execute_orphan_deletions(es, orphan_plan)
    except ES_ERRORS as e:
        failures_ref[0] += 1
        click.echo(f"error deleting orphans: {e}", err=True)
        return (0, 0, 0, 0, 0, [])


def _summarize_submission(
    total_markers: int, total_stale_markers: int, total_orphan_indices: int, total_empty_indices: int,
    total_commits: int, total_stale_commits: int, total_orphan_content: int,
    total_orphan_stale: int, total_orphan_markers: int, task_ids: list[str],
) -> str:
    """Build the post-run summary, distinguishing work that is genuinely DONE by the time this
    prints (marker deletes -- a real bulk/single DELETE -- and whole-index DELETEs) from content
    (and orphaned-marker) deletions that were only SUBMITTED as async delete_by_query and may
    still be running (see execute.py's task-id plumbing). Re-running prune immediately after
    would otherwise re-report the same orphans, which used to read as a bug."""
    deleted_parts = [f"{total_markers} marker(s)"]
    if total_stale_markers:
        deleted_parts.append(f"{total_stale_markers} stale marker(s)")
    if total_orphan_indices:
        deleted_parts.append(f"{total_orphan_indices} orphaned index(es)")
    if total_empty_indices:
        deleted_parts.append(f"{total_empty_indices} empty index(es)")
    msg = f"Deleted {', '.join(deleted_parts)}."

    submitted_bits = []
    if total_commits:
        submitted_bits.append(f"{total_commits} retention-pruned commit(s)")
    if total_stale_commits:
        submitted_bits.append(f"{total_stale_commits} stale commit(s)")
    if total_orphan_content:
        submitted_bits.append(f"{total_orphan_content} orphaned content commit(s)")
    if total_orphan_stale:
        submitted_bits.append(f"{total_orphan_stale} stale-location content commit(s)")
    if total_orphan_markers:
        submitted_bits.append(f"{total_orphan_markers} orphaned marker commit(s)")
    if submitted_bits:
        msg += (
            f" Submitted async deletion of {', '.join(submitted_bits)} "
            f"({len(task_ids)} task(s); not yet reflected in search results -- pass --wait to "
            f"block until they complete, or check them via GET _tasks/<id>)."
        )
    return msg


def _summarize_confirmed(
    total_markers: int, total_stale_markers: int, total_orphan_indices: int,
    total_empty_indices: int, task_results: dict[str, dict],
) -> str:
    """Build the post-run summary for a --wait run, once every submitted task has actually
    finished: reports real outcomes (from es.tasks.get's status) instead of submission targets."""
    confirmed_deleted = sum(status.get("deleted", 0) for status in task_results.values())
    confirmed_conflicts = sum(status.get("version_conflicts", 0) for status in task_results.values())
    parts = [f"{total_markers} marker(s)"]
    if total_stale_markers:
        parts.append(f"{total_stale_markers} stale marker(s)")
    if total_orphan_indices:
        parts.append(f"{total_orphan_indices} orphaned index(es)")
    if total_empty_indices:
        parts.append(f"{total_empty_indices} empty index(es)")
    msg = f"Deleted {', '.join(parts)} and confirmed {confirmed_deleted} content doc(s) deleted"
    if confirmed_conflicts:
        msg += f" ({confirmed_conflicts} version conflict(s) skipped)"
    return msg + "."


def run(config_path=None, url=None, api_key=None, username=None, password=None,
        dry_run=False, quiet=False, insecure=False, wait=False,
        scope_host=None, scope_org=None, scope_repo=None) -> None:
    """--config is optional: the retention pass has nothing to plan without one, but the
    orphan sweep below doesn't depend on any config and always runs.

    `scope_host`/`scope_org`/`scope_repo` narrow the orphan sweep to one host/org/repo instead
    of the full cluster (see planner.index_in_scope); the retention pass is unaffected since it
    already only ever touches the repos named in `config_path`."""
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
    retention_rows: list[_Row] = []
    repo_decisions = []  # (cfg, decisions) for repos that planned successfully

    for cfg in entries:
        try:
            markers = fetch_markers(es, cfg.host, cfg.org, cfg.repo)
            decisions = plan_repo(markers, cfg, now)
        except ES_ERRORS as e:
            failures[0] += 1
            click.echo(f"{cfg.host}/{cfg.org}/{cfg.repo}: error planning: {e}", err=True)
            continue
        retention_rows.extend(_retention_rows(cfg, decisions))
        repo_decisions.append((cfg, decisions))

    # Print retention rows as soon as the retention pass finishes -- it's a handful of scans, one
    # per configured repo, so this is fast; printing it immediately gives the operator real
    # output in the first second instead of waiting for the (much slower) orphan sweep below.
    if retention_rows and (not quiet or dry_run):
        _print(retention_rows)

    if scope_host or scope_org or scope_repo:
        if not quiet:
            click.echo(
                f"Orphan sweep scoped to {scope_host or '*'}/{scope_org or '*'}/{scope_repo or '*'}"
            )

    # Orphan sweep: independent of any one config entry -- it targets indices/markers with no
    # config presence at all (a repo dropped from the config, a marker deleted by hand, content
    # left behind by an interrupted run). Refresh again first so it sees the marker deletes the
    # retention pass above just made (those were fired with refresh=False).
    reporter = make_prune_reporter(quiet)
    orphan_plan = _plan_orphans(es, reporter, failures, scope_host, scope_org, scope_repo)
    orphan_rows = _orphan_rows(orphan_plan) if orphan_plan is not None else []
    if orphan_rows and (not quiet or dry_run):
        _print(orphan_rows)

    total_orphan_indices = total_orphan_content = total_orphan_markers = 0
    total_orphan_stale = total_empty_indices = 0
    total_stale_markers = total_stale_commits = 0
    submitted_tasks: list[str] = []
    if not dry_run:
        for cfg, decisions in repo_decisions:
            if any(d.action == "delete" for d in decisions):
                try:
                    n_markers, n_commits, task_ids = execute_deletions(es, cfg.host, cfg.org, cfg.repo, decisions)
                    total_markers += n_markers
                    total_commits += n_commits
                    submitted_tasks.extend(task_ids)
                except ES_ERRORS as e:
                    failures[0] += 1
                    click.echo(f"{cfg.host}/{cfg.org}/{cfg.repo}: error deleting: {e}", err=True)

        # Reclaim stale snapshot markers from mode-switches (snapshot → incremental). These are
        # markers flipped to status="stale" by index_incremental_branch_in_dir before the new
        # incremental join doc was published, so they are never visible to content tools but do
        # hold snapshot content that may no longer be needed.
        try:
            total_stale_markers, total_stale_commits, stale_task_ids = execute_stale_marker_deletions(es)
            submitted_tasks.extend(stale_task_ids)
        except ES_ERRORS as e:
            failures[0] += 1
            click.echo(f"error reclaiming stale markers: {e}", err=True)

        if orphan_plan is not None:
            (total_orphan_indices, total_orphan_content, total_orphan_markers,
             total_orphan_stale, total_empty_indices, orphan_task_ids) = _apply_orphan_plan(es, orphan_plan, failures)
            submitted_tasks.extend(orphan_task_ids)

    if dry_run:
        click.echo("Dry run: no changes made.")
    elif not quiet:
        if wait and submitted_tasks:
            wait_reporter = make_prune_reporter(quiet)
            with wait_reporter:
                task_results = wait_for_deletions(es, submitted_tasks, reporter=wait_reporter)
            click.echo(_summarize_confirmed(
                total_markers, total_stale_markers, total_orphan_indices, total_empty_indices, task_results,
            ))
        else:
            click.echo(_summarize_submission(
                total_markers, total_stale_markers, total_orphan_indices, total_empty_indices,
                total_commits, total_stale_commits, total_orphan_content,
                total_orphan_stale, total_orphan_markers, submitted_tasks,
            ))
            if submitted_tasks:
                click.echo("Task ID(s): " + ", ".join(submitted_tasks))
    elif wait and submitted_tasks:
        # Quiet + --wait still blocks (the caller asked for completion), just without the report.
        wait_for_deletions(es, submitted_tasks)
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
    wait: bool = False,
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
    task_ids: list[str] = []

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
                # No marker to reconstruct routing from -> discover the actual index(es) holding
                # this commit's content (may be a commit-level or suffixed index, not repo-level).
                located = content_indices_for_commit(es, host, org, repo, sha)
                task_ids = delete_commit_content(es, host, org, repo, sha, index_names=located or None)
                total_commits = 1
            except ES_ERRORS as e:
                failures[0] += 1
                click.echo(f"{host}/{org}/{repo}: error deleting content: {e}", err=True)

        if dry_run:
            click.echo("Dry run: no changes made.")
        elif not quiet:
            click.echo(_ref_summary(0, total_commits, task_ids, es, wait))
            if task_ids and not wait:
                click.echo("Task ID(s): " + ", ".join(task_ids))
        elif wait and task_ids:
            wait_for_deletions(es, task_ids)
        if failures[0]:
            click.echo(f"Completed with {failures[0]} failure(s)", err=True)
            sys.exit(1)
        return

    rows.extend(_ref_rows(host, org, repo, decisions))

    if not quiet or dry_run:
        _print(rows)

    if not dry_run:
        try:
            total_markers, total_commits, task_ids = execute_deletions(es, host, org, repo, decisions)
        except ES_ERRORS as e:
            failures[0] += 1
            click.echo(f"{host}/{org}/{repo}: error deleting: {e}", err=True)

    if dry_run:
        click.echo("Dry run: no changes made.")
    elif not quiet:
        click.echo(_ref_summary(total_markers, total_commits, task_ids, es, wait))
        if task_ids and not wait:
            click.echo("Task ID(s): " + ", ".join(task_ids))
    elif wait and task_ids:
        wait_for_deletions(es, task_ids)
    if failures[0]:
        click.echo(f"Completed with {failures[0]} failure(s)", err=True)
        sys.exit(1)


def _ref_summary(total_markers: int, total_commits: int, task_ids: list[str], es, wait: bool) -> str:
    """Summary line for run_ref's two delete paths (marker-driven and content-only), reworded
    the same way run()'s is: distinguish the synchronous marker DELETE from the submitted (or,
    with --wait, confirmed) async content delete_by_query."""
    if not task_ids:
        return f"Deleted {total_markers} marker(s) and {total_commits} commit(s) of content."
    if wait:
        results = wait_for_deletions(es, task_ids)
        confirmed = sum(status.get("deleted", 0) for status in results.values())
        return f"Deleted {total_markers} marker(s); confirmed {confirmed} content doc(s) deleted."
    return (
        f"Deleted {total_markers} marker(s); submitted async deletion of {total_commits} commit(s) "
        f"of content ({len(task_ids)} task(s); pass --wait to block until they complete)."
    )
