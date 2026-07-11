# sourcerer/commands/prune.py
# `sourcerer prune --config repos.yml [--dry-run]`
# Deletes indexed refs that fall outside their repos.yml retention policies.

# Standard packages
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

# Third-party packages
import click
import yaml
from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk
from rich.console import Console
from rich.text import Text

# App packages
from ..config import load_config
from ..planner import OrphanPlan, content_delete_set, plan_orphans, plan_repo
from ..utils import make_client
from .index import (
    ES_ERRORS,
    REFS_INDEX,
    enumerate_ref_tuples,
    fetch_markers,
    files_index,
    gather_content_commit_tuples,
    lines_index,
    list_sourcerer_indices,
)

_console = Console()


@dataclass(frozen=True)
class _Row:
    """One line of the prune report: what would be deleted and why, so a line stands on its
    own when grepped, without needing a header line above it for context.

    WHY is one of a fixed set of tags:
      - "policy:<criteria>" -- a retain-policy deletion, where <criteria> is a comma-joined
        subset of "age", "count", "version", "prerelease" (that fixed order) naming exactly
        which criteria excluded this marker -- e.g. "policy:count" or "policy:age,count".
      - "orphan:index"      -- a whole physical index with no matching refs entry (Class A).
      - "orphan:content"    -- a commit's content docs with no surviving marker (Class B).
      - "orphan:marker"     -- a commit's marker doc with no content at all (Class C).
    (See planner.OrphanPlan for the three orphan classes.)

    WHAT names the actual object being deleted, which differs by tag rather than forcing one
    shape:
      - policy:*                       -> "org/repo@ref (commit)" -- a marker, addressed by
        its ref since two refs can point at the same pruned commit and be indistinguishable
        without it.
      - orphan:content, orphan:marker   -> "org/repo@commit" -- no ref exists for either case.
      - orphan:index                    -> the index's own name (e.g.
        "sourcerer-v1-files~org~repo") -- not commit- or even repo-addressable, since an
        org-level orphan index spans every repo under that org."""

    what: str
    why: str


def _retention_rows(cfg, decisions: list) -> list[_Row]:
    """Rows for markers a repo's retain policy would prune (see _Row for the WHAT/WHY
    shapes). Only 'delete' decisions are reported -- kept and unmanaged markers aren't being
    deleted, so there's nothing actionable to grep for in them."""
    return [_Row(f"{cfg.org}/{cfg.repo}@{d.marker.ref} ({d.marker.commit})",
                 "policy:" + ",".join(d.criteria))
            for d in decisions if d.action == "delete"]


def _orphan_rows(plan: OrphanPlan) -> list[_Row]:
    """Rows for the three orphan classes (see _Row for the WHAT/WHY shapes, and
    planner.OrphanPlan for the classes themselves)."""
    rows = []
    for name in plan.orphan_index_names:
        rows.append(_Row(name, "orphan:index"))
    for (org, repo), commits in sorted(plan.orphan_content.items()):
        for commit in sorted(commits):
            rows.append(_Row(f"{org}/{repo}@{commit}", "orphan:content"))
    for (org, repo), commits in sorted(plan.orphan_marker_commits.items()):
        for commit in sorted(commits):
            rows.append(_Row(f"{org}/{repo}@{commit}", "orphan:marker"))
    return rows


def _print(rows: list[_Row]) -> None:
    """Flat, greppable report: one line per deleted object, sorted lexicographically by WHY
    then WHAT (see _Row for what WHAT/WHY can contain) -- so every tag's rows sit together,
    grouped as if pre-filtered by grep. Nothing is printed for markers being kept; there's
    nothing to act on in something that isn't changing.

    No header row -- WHY leads since it's the shorter, more scannable field. Hand-padded and
    printed with soft_wrap (rather than a rich.Table, which wraps/crops cells to the terminal
    width) so every row stays exactly one physical line."""
    if not rows:
        return
    rows = sorted(rows, key=lambda r: (r.why, r.what))
    w_why = max(len(r.why) for r in rows)

    for r in rows:
        style = "yellow" if r.why.startswith("policy:") else "red"
        line = Text()
        line.append(f"{r.why:<{w_why}} ", style=style)
        line.append(r.what)
        _console.print(line, soft_wrap=True)


# --- Deletion logic ------------------------------------------------------------------------
# Every code path that actually deletes something (retention or orphan-sweep) lives here,
# alongside the planning/printing that decides what to delete. commands/index.py only ever
# reads from Elasticsearch (it builds and writes content during indexing, but never deletes).


def execute_deletions(
    es: Elasticsearch, org: str, repo: str, decisions,
) -> tuple[int, int]:
    """Apply a retention prune plan. Deletes the marker docs, then fires async delete-by-query
    for each pruned commit's content in both shared indices. Content is dropped ONLY for
    commits that no surviving marker references (the commit-safety guard, in
    content_delete_set). Returns (markers_deleted, commits_content_dropped)."""
    deletes = [d.marker for d in decisions if d.action == "delete"]
    if not deletes:
        return (0, 0)
    drop_commits = content_delete_set(decisions)

    bulk(
        es,
        ({"_op_type": "delete", "_index": REFS_INDEX, "_id": m.id} for m in deletes),
        raise_on_error=False,
        refresh=False,
    )

    for sha in drop_commits:
        for idx in (lines_index(org, repo), files_index(org, repo)):
            try:
                es.delete_by_query(
                    index=idx,
                    query={"bool": {"filter": [
                        {"term": {"git.org": org}},
                        {"term": {"git.repo": repo}},
                        {"term": {"git.commit": sha}},
                    ]}},
                    conflicts="proceed",
                    refresh=False,
                    scroll_size=5000,
                    wait_for_completion=False,
                )
            except NotFoundError:
                pass
    return (len(deletes), len(drop_commits))


def delete_index(es: Elasticsearch, name: str) -> bool:
    """DELETE one physical index by its exact (non-wildcard) name -- near-instant, and safe on
    clusters that reject wildcard index deletes. Returns True if deleted, False if it was
    already gone (e.g. a race with another prune/index run)."""
    try:
        es.indices.delete(index=name)
        return True
    except NotFoundError:
        return False


def plan_orphans_now(es: Elasticsearch) -> OrphanPlan:
    """Take one read-only snapshot of the cluster (index names, ref tuples, content tuples) via
    the read helpers in commands/index.py, and compute the full orphan plan from it via
    planner.plan_orphans. Single-pass by construction: every input is gathered before any
    orphan is computed, so the plan reflects one consistent view rather than re-reading state
    between classes."""
    index_names = list_sourcerer_indices(es)
    ref_tuples = enumerate_ref_tuples(es)
    content_tuples = gather_content_commit_tuples(es, index_names)
    return plan_orphans(index_names, ref_tuples, content_tuples)


def execute_orphan_deletions(es: Elasticsearch, plan: OrphanPlan) -> tuple[int, int, int]:
    """Apply an OrphanPlan in ascending-cost order: index DELETEs first (near-instant), then
    the per-repo content delete_by_query (Class B -- expensive, one call per content index per
    repo), then a single delete_by_query against sourcerer-v1-refs covering every orphaned
    marker tuple across every repo (Class C -- refs is tiny, so one combined query costs one
    merge cycle instead of one per repo). Returns
    (indices_deleted, content_commits_dropped, marker_commits_dropped)."""
    indices_deleted = 0
    for name in plan.orphan_index_names:
        if delete_index(es, name):
            indices_deleted += 1

    commits_dropped = 0
    for (org, repo), commits in plan.orphan_content.items():
        commits_dropped += len(commits)
        for idx in (lines_index(org, repo), files_index(org, repo)):
            try:
                es.delete_by_query(
                    index=idx,
                    query={"bool": {"filter": [
                        {"term": {"git.org": org}},
                        {"term": {"git.repo": repo}},
                        {"terms": {"git.commit": sorted(commits)}},
                    ]}},
                    conflicts="proceed",
                    refresh=False,
                    scroll_size=5000,
                    wait_for_completion=False,
                )
            except NotFoundError:
                pass

    markers_dropped = sum(len(commits) for commits in plan.orphan_marker_commits.values())
    if plan.orphan_marker_commits:
        should = [
            {"bool": {"filter": [
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"terms": {"git.commit": sorted(commits)}},
            ]}}
            for (org, repo), commits in plan.orphan_marker_commits.items()
        ]
        try:
            es.delete_by_query(
                index=REFS_INDEX,
                query={"bool": {"should": should, "minimum_should_match": 1}},
                conflicts="proceed",
                refresh=False,
                scroll_size=5000,
                wait_for_completion=False,
            )
        except NotFoundError:
            pass

    return indices_deleted, commits_dropped, markers_dropped


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