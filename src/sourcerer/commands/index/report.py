# sourcerer/commands/index/report.py
# Dry-run preview for `index [--config] --dry-run`: compute, then print, what a real run would
# index (and, with --prune, what the post-index prune step would then delete) without writing
# anything to Elasticsearch.

# Standard packages
import datetime
import pathlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

# Third-party packages
import click
from elasticsearch import Elasticsearch

# App packages
from ...config import RepoConfig
from ...indices import REFS_INDEX
from ...planner import Marker, content_delete_set, plan_repo
from ...progress import Unit
from ...queries import fetch_markers
from ...utils import ES_ERRORS
from .git import _rev_info, prepared_repo
from .markers import build_ref_id, commit_fully_indexed, should_index
from .runtime import _tuning
from .selection import _effective_since_floor, _resolve_entry


def _dry_run_repo(
    es: Elasticsearch,
    cfg: RepoConfig | None,
    org: str,
    repo: str,
    group: list[Unit],
    cache_root: pathlib.Path | None,
    ephemeral: bool,
    force: bool,
    prune: bool,
    now: datetime.datetime,
) -> dict:
    """Compute one repo's dry-run preview. Clones/fetches its cached clone to resolve each
    planned ref's real commit + date (read-only -- writes nothing to ES), classifying every ref
    exactly as a real run would (index / reuse content / up-to-date / skip), then, when `prune`,
    plans the post-index prune over existing markers merged with the refs this run would add.
    Returns a result dict consumed by `_print_dry_run_repo`."""
    result: dict = {"org": org, "repo": repo, "rows": [], "prune": None, "locked": False}
    with prepared_repo(org, repo, cache_root, ephemeral) as repo_dir:
        if repo_dir is None:
            result["locked"] = True
            return result
        rows = result["rows"]
        synth: list[Marker] = []

        # Pass 1: resolve each ref and classify the already-decided cases (unresolvable,
        # already-indexed, below a `since` floor). Branches resolve to their fetched
        # origin tip -- the same commit checkout_branch would land on -- not a stale local
        # branch; tags dereference to their commit (matching write_ref_marker).
        pending: list[tuple[Unit, str, str, datetime.datetime | None]] = []
        for unit in group:
            ref_type = unit.kind
            rev = f"origin/{unit.ref}" if ref_type == "branch" else unit.ref
            info = _rev_info(repo_dir, rev)
            sha, cd = info if info else ("", None)
            if not sha:
                rows.append((unit, "error", "could not resolve ref", None, None))
                continue
            if not force and not should_index(es, org, repo, ref_type, unit.ref, sha):
                rows.append((unit, "up-to-date", "already indexed", sha, cd))
                continue
            floor = _effective_since_floor(cfg, repo_dir, ref_type, unit.ref, now)
            if floor is not None and cd is not None and cd < floor:
                rows.append((unit, "skip", f"before since floor {floor.date()}", sha, cd))
                continue
            pending.append((unit, ref_type, sha, cd))

        # Prune-aware pre-filter over the fresh candidates, exactly as process_group does before
        # indexing: a ref this run would immediately re-prune (e.g. an older patch superseded by a
        # newer one also new this run) is never indexed. Scored over the candidate set only.
        doomed: set[str] = set()
        if cfg is not None and pending:
            candidates = [
                Marker(id=f"{rt}:{u.ref}", ref=u.ref, ref_type=rt, commit=sha, commit_date=cd, indexed_at=now)
                for (u, rt, sha, cd) in pending
            ]
            doomed = {d.marker.id for d in plan_repo(candidates, cfg, now) if d.action == "delete"}

        for unit, ref_type, sha, cd in pending:
            if f"{ref_type}:{unit.ref}" in doomed:
                rows.append((unit, "skip", "pruned by retain", sha, cd))
                continue
            # Content is keyed by commit: if another ref already fully indexed this exact commit
            # (and we're not forcing a rewrite), a real run only records the marker -- no
            # re-ingest. A commit with only partial content from an aborted run has no complete
            # marker, so it shows as "index" (re-ingest), matching what the real run will do.
            shared = not force and commit_fully_indexed(es, org, repo, sha)
            rows.append((unit, "reuse" if shared else "index", "content shared" if shared else "", sha, cd))
            synth.append(Marker(
                id=build_ref_id(org, repo, ref_type, unit.ref, sha),
                ref=unit.ref, ref_type=ref_type, commit=sha, commit_date=cd, indexed_at=now,
            ))

        # Prune preview: the markers that would exist AFTER this run = existing markers (older
        # refs a policy now retires, already-indexed refs) merged with the refs this run adds
        # (dedup by id: a synthesized marker for an already-present ref collapses onto it). This
        # is exactly the set the post-index `--prune` step plans over.
        if prune and cfg is not None:
            by_id = {m.id: m for m in fetch_markers(es, org, repo)}
            for m in synth:
                by_id.setdefault(m.id, m)
            result["prune"] = plan_repo(list(by_id.values()), cfg, now)
    return result


# How each dry-run row's status renders; the padding keeps the ref columns aligned.
_DRY_RUN_LABELS = {
    "index": "would index",
    "reuse": "reuse content",
    "up-to-date": "up to date",
    "skip": "skip",
    "error": "error",
}


def _print_dry_run_repo(result: dict, prune: bool) -> tuple[int, int, int]:
    """Print one repo's combined preview (what would be indexed, then what would be pruned) and
    return (index_count, prune_markers, prune_commits) for the run summary."""
    org, repo = result["org"], result["repo"]
    if result["locked"]:
        click.echo(f"{org}/{repo}: locked by another sourcerer run — skipped")
        return (0, 0, 0)

    click.echo(f"{org}/{repo}")
    click.echo("  index:")
    index_count = 0
    for unit, status, detail, sha, cd in result["rows"]:
        if status in ("index", "reuse"):
            index_count += 1
        label = _DRY_RUN_LABELS.get(status, status)
        short = sha[:10] if sha else "-"
        suffix = f"   ({detail})" if detail else ""
        click.echo(f"    {label:14} {unit.kind:6} {unit.ref:28} {short}{suffix}")
    if not result["rows"]:
        click.echo("    (no refs selected)")

    markers = commits = 0
    if prune:
        decisions = result["prune"] or []
        deletes = [d for d in decisions if d.action == "delete"]
        drop = content_delete_set(decisions)
        keeps = sum(1 for d in decisions if d.action == "keep")
        markers, commits = len(deletes), len(drop)
        click.echo("  prune (after indexing):")
        if not deletes:
            click.echo("    (nothing to prune)")
        for d in deletes:
            content = "marker + content" if d.marker.commit in drop else "marker only (commit shared)"
            click.echo(f"    would delete   {d.marker.ref_type:6} {d.marker.ref:28} "
                       f"{d.marker.commit[:10]}  {content}   [{d.reason}]")
        if deletes:
            click.echo(f"    -> {markers} marker(s), {commits} commit(s) of content, {keeps} kept")
    return (index_count, markers, commits)


def dry_run_config(
    es: Elasticsearch,
    entries: list[RepoConfig],
    cfg_by_repo: dict[tuple[str, str], RepoConfig],
    cache_root: pathlib.Path | None,
    ephemeral: bool,
    force: bool,
    prune: bool,
) -> None:
    """Preview an `index [--prune]` run without writing anything to Elasticsearch. Resolves the
    plan (ls-remote), then per repo clones/fetches its cached clone to resolve real commits +
    dates and classify each ref, and -- with `prune` -- plans the post-index prune. Prints one
    combined report (what would be indexed, then what would be pruned) and exits non-zero if any
    repo could not be previewed."""
    now = datetime.datetime.now(datetime.timezone.utc)
    # refs is tiny; refresh once so the prune preview sees the latest existing markers.
    es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)

    # Phase 1: resolve the plan, identical to the real run (see command.run_config).
    units: list[Unit] = []
    with ThreadPoolExecutor(max_workers=max(1, min(_tuning().resolve_concurrency, len(entries) or 1))) as pool:
        for fut in [pool.submit(_resolve_entry, entry) for entry in entries]:
            units.extend(fut.result())
    units.sort(key=lambda u: (u.org, u.repo, u.ref or "", u.kind))
    groups: dict[tuple[str, str], list[Unit]] = {}
    for unit in units:
        groups.setdefault((unit.org, unit.repo), []).append(unit)

    click.echo("DRY RUN — no changes will be made to Elasticsearch.\n")
    if not groups:
        click.echo("Nothing selected by the config.")
        return

    failures = 0

    def work(item: tuple[tuple[str, str], list[Unit]]) -> dict:
        (org, repo), group = item
        cfg = cfg_by_repo.get((org, repo))
        try:
            return _dry_run_repo(es, cfg, org, repo, group, cache_root, ephemeral, force, prune, now)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError, *ES_ERRORS) as e:
            return {"org": org, "repo": repo, "error": str(e)}

    # Clone/fetch + resolve repos concurrently (fetches release the GIL); collect all results and
    # print in sorted plan order so the combined report is deterministic and never interleaved.
    with ThreadPoolExecutor(max_workers=max(1, _tuning().index_repo_concurrency)) as pool:
        results = list(pool.map(work, groups.items()))

    tot_index = tot_markers = tot_commits = 0
    for r in results:
        if r.get("error"):
            failures += 1
            click.echo(f"{r['org']}/{r['repo']}: error: {r['error']}", err=True)
            continue
        ci, mk, cm = _print_dry_run_repo(r, prune)
        tot_index += ci
        tot_markers += mk
        tot_commits += cm
        click.echo("")

    summary = f"Summary: would index {tot_index} ref(s)"
    if prune:
        summary += f"; would prune {tot_markers} marker(s) and {tot_commits} commit(s) of content"
    click.echo(summary + ".")
    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)
