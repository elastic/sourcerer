# sourcerer/commands/prune/command.py
# `sourcerer prune --config repos.yml [--dry-run]`
# Deletes indexed refs that fall outside their repos.yml retention policies, and always sweeps
# for orphans. Row/plan construction and printing live in report.py; the deletions themselves
# (retention and orphan sweep) live in execute.py. This file is the entry point that wires the
# two together.

# Standard packages
import sys
from datetime import datetime, timezone

# Third-party packages
import click
import yaml

# App packages
from ...config import load_config
from ...indices import REFS_INDEX
from ...planner import plan_repo
from ...queries import fetch_markers
from ...utils import ES_ERRORS, make_client
from .execute import execute_deletions, execute_orphan_deletions, plan_orphans_now
from .report import _Row, _orphan_rows, _print, _retention_rows


def run(config_path=None, url=None, api_key=None, username=None, password=None,
        dry_run=False, quiet=False) -> None:
    """--config is optional: the retention pass has nothing to plan without one, but the
    orphan sweep below doesn't depend on any config and always runs."""
    entries = []
    if config_path:
        try:
            entries = load_config(config_path)
        except (OSError, ValueError, yaml.YAMLError) as e:
            click.echo(f"Error: invalid config: {e}", err=True)
            sys.exit(1)

    es = make_client(url, api_key, username, password)
    # refs is tiny; refresh once so planning sees the latest markers.
    es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)
    now = datetime.now(timezone.utc)
    failures = 0
    total_markers = 0
    total_commits = 0
    rows: list[_Row] = []
    repo_decisions = []  # (cfg, decisions) for repos that planned successfully

    for cfg in entries:
        try:
            markers = fetch_markers(es, cfg.org, cfg.repo)
            decisions = plan_repo(markers, cfg, now)
        except ES_ERRORS as e:
            failures += 1
            click.echo(f"{cfg.org}/{cfg.repo}: error planning: {e}", err=True)
            continue
        rows.extend(_retention_rows(cfg, decisions))
        repo_decisions.append((cfg, decisions))

    # Orphan sweep: independent of any one config entry -- it targets indices/markers with no
    # config presence at all (a repo dropped from the config, a marker deleted by hand, content
    # left behind by an interrupted run). Refresh again first so it sees the marker deletes the
    # retention pass above just made (those were fired with refresh=False).
    total_orphan_indices = total_orphan_content = total_orphan_markers = 0
    try:
        es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)
        orphan_plan = plan_orphans_now(es)
    except ES_ERRORS as e:
        failures += 1
        click.echo(f"error scanning for orphans: {e}", err=True)
        orphan_plan = None

    if orphan_plan is not None:
        rows.extend(_orphan_rows(orphan_plan))

    if not quiet or dry_run:
        _print(rows)

    if not dry_run:
        for cfg, decisions in repo_decisions:
            if any(d.action == "delete" for d in decisions):
                try:
                    n_markers, n_commits = execute_deletions(es, cfg.org, cfg.repo, decisions)
                    total_markers += n_markers
                    total_commits += n_commits
                except ES_ERRORS as e:
                    failures += 1
                    click.echo(f"{cfg.org}/{cfg.repo}: error deleting: {e}", err=True)

        if orphan_plan is not None:
            has_orphans = bool(
                orphan_plan.orphan_index_names or orphan_plan.orphan_content or orphan_plan.orphan_marker_commits
            )
            if has_orphans:
                try:
                    total_orphan_indices, total_orphan_content, total_orphan_markers = execute_orphan_deletions(
                        es, orphan_plan
                    )
                except ES_ERRORS as e:
                    failures += 1
                    click.echo(f"error deleting orphans: {e}", err=True)

    if dry_run:
        click.echo("Dry run: no changes made.")
    elif not quiet:
        click.echo(
            f"Pruned {total_markers} marker(s) and {total_commits} commit(s) of content; "
            f"removed {total_orphan_indices} orphaned index(es), "
            f"{total_orphan_content} orphaned content commit(s), "
            f"{total_orphan_markers} orphaned marker commit(s)."
        )
    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)
