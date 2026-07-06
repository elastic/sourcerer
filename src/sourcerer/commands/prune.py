# sourcerer/commands/prune.py
# `sourcerer prune --config repos.yml [--dry-run]`
# Deletes indexed refs that fall outside their repos.yml retention policies.

# Standard packages
import sys
from datetime import datetime, timezone

# Third-party packages
import click
import yaml

# App packages
from ..config import load_config
from ..planner import content_delete_set, plan_repo
from ..utils import make_client
from .index import ES_ERRORS, execute_deletions, fetch_markers


def _print_repo(cfg, decisions, dry_run: bool) -> None:
    deletes = [d for d in decisions if d.action == "delete"]
    keeps = sum(1 for d in decisions if d.action == "keep")
    unmanaged = [d for d in decisions if d.action == "unmanaged"]
    if not deletes and not unmanaged:
        return  # nothing notable for this repo
    click.echo(f"{cfg.org}/{cfg.repo}")
    drop_commits = content_delete_set(decisions)
    verb = "would delete" if dry_run else "delete"
    for d in deletes:
        content = "marker + content" if d.marker.commit in drop_commits else "marker only (commit shared)"
        click.echo(f"  {verb}  {d.marker.ref:20} {d.marker.commit[:10]}  {content}   [{d.reason}]")
    for d in unmanaged:
        click.echo(f"  unmanaged {d.marker.ref:18} {d.marker.commit[:10]}  (matches no selector)")
    click.echo(f"  -> {len(deletes)} marker(s), {len(drop_commits)} commit(s) of content, "
               f"{keeps} kept, {len(unmanaged)} unmanaged")


def run(config_path, url, api_key, username, password,
        dry_run=False, force_merge=True, quiet=False) -> None:
    try:
        entries = load_config(config_path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        click.echo(f"Error: invalid config: {e}", err=True)
        sys.exit(1)

    es = make_client(url, api_key, username, password)
    # refs is tiny; refresh once so planning sees the latest markers.
    es.indices.refresh(index="sourcerer-v1-refs", ignore_unavailable=True)
    now = datetime.now(timezone.utc)
    failures = 0
    total_markers = 0
    total_commits = 0

    for cfg in entries:
        try:
            markers = fetch_markers(es, cfg.org, cfg.repo)
            decisions = plan_repo(markers, cfg, now)
        except ES_ERRORS as e:
            failures += 1
            click.echo(f"{cfg.org}/{cfg.repo}: error planning: {e}", err=True)
            continue

        if not quiet or dry_run:
            _print_repo(cfg, decisions, dry_run)
        if dry_run:
            continue
        if any(d.action == "delete" for d in decisions):
            try:
                n_markers, n_commits = execute_deletions(es, cfg.org, cfg.repo, decisions, force_merge)
                total_markers += n_markers
                total_commits += n_commits
            except ES_ERRORS as e:
                failures += 1
                click.echo(f"{cfg.org}/{cfg.repo}: error deleting: {e}", err=True)

    if dry_run:
        click.echo("Dry run: no changes made.")
    elif not quiet:
        click.echo(f"Pruned {total_markers} marker(s) and {total_commits} commit(s) of content.")
    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)