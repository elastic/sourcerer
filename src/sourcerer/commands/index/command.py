# sourcerer/commands/index/command.py
# `sourcerer index <org>/<repo> [-b <branch>] [-t <tag>] [-c <commit>]`
# `sourcerer index --config <file> [--prune] [--dry-run]`
# CLI entry points for indexing: a single ref of one repo (`run`), or every ref a repos.yml
# config selects (`run_config`). The concerns this used to hold directly now live in sibling
# modules: git.py (clone/checkout/remote resolution), documents.py (doc building + bulk
# ingest), markers.py (refs-index idempotency guards + marker write), selection.py (config
# selector -> Unit resolution + since-floors), report.py (--dry-run preview), and runtime.py
# (env tuning, the Ctrl-C abort flag, bulk-indexing ES settings). This file wires them together
# into the two orchestration paths `cli.py` calls.

# Standard packages
import datetime
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-party packages
import click
import yaml
from elasticsearch import Elasticsearch

# App packages
from ...indices import files_index, lines_index
from ...planner import Marker, plan_repo
from ...progress import ProgressReporter, Unit, make_reporter
from ...utils import ES_ERRORS, make_client
from ..prune import command as prune_cmd
from .documents import index_repo
from .git import (
    checkout_branch,
    checkout_ref,
    commit_date,
    count_tracked_files,
    default_branch,
    prepared_repo,
    ref_dates,
    resolve_cache_root,
    resolve_commit,
    _rev_info,
)
from .markers import commit_fully_indexed, count_commit_docs, pre_clone_skip, should_index, write_ref_marker
from .report import dry_run_config
from .runtime import _aborted, _tuning, bulk_indexing_settings, handle_interrupts
from .selection import _effective_since_floor, _load_config, _resolve_entry


def index_ref_in_dir(
    es: Elasticsearch,
    org: str,
    repo: str,
    repo_dir,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
) -> None:
    """
    Index a single ref of one repo into an already-cloned `repo_dir`. Checks out the ref
    (a full clone holds every branch/tag, so one clone serves any number of refs), then
    runs the authoritative SHA guard and indexes, tags, or records the ref. At most one of
    branch/tag/commit should be set; none means the remote's default branch.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
        unit = Unit(org=org, repo=repo, ref=branch or tag or commit, kind=kind)

    # The repo is already cloned/fetched (callers clone once, then reuse this dir for every ref);
    # this only checks out the ref, so the stage is "checkout", not "cloning". Branches (and the
    # default branch) are checked out at their fetched remote tip so a reused clone can't land on
    # a stale local branch; tags/commits are immutable and checked out directly.
    reporter.set_stage(unit, "checkout")
    if branch:
        checkout_branch(repo_dir, branch)
    elif tag or commit:
        checkout_ref(repo_dir, tag or commit)
    else:
        branch = default_branch(repo_dir)
        checkout_branch(repo_dir, branch)

    commit_sha = resolve_commit(repo_dir)
    if branch:
        ref_for_id = branch
    elif tag:
        ref_for_id = tag
    else:  # commit
        ref_for_id = commit_sha
    ref_type = "branch" if branch else "tag" if tag else "commit"
    commit_date_iso = commit_date(repo_dir)
    unit.ref = ref_for_id

    # Post-clone guard: authoritative SHA check (covers -c and any ls-remote
    # peeling mismatch).
    if not force and not should_index(es, org, repo, ref_type, ref_for_id, commit_sha):
        reporter.finish(unit, "skipped")
        return

    if not force and commit_fully_indexed(es, org, repo, commit_sha):
        # Content is keyed by commit, so another ref already fully indexed this exact
        # snapshot. Don't rewrite the (large) content docs -- just record the ref marker.
        # "Fully" is load-bearing: an aborted run leaves partial content but no complete
        # marker, so it falls through to the else branch and re-indexes rather than
        # recording a half-populated commit as done.
        status = "tagged" if tag else "recorded"
        files_count = count_commit_docs(es, files_index(org, repo), org, repo, commit_sha)
        lines_count = count_commit_docs(es, lines_index(org, repo), org, repo, commit_sha)
    else:
        reporter.set_total_files(unit, count_tracked_files(repo_dir))
        reporter.set_stage(unit, "indexing")
        files_count, lines_count = index_repo(
            es, org, repo, repo_dir, commit_sha,
            on_progress=lambda f, l: reporter.update_counts(unit, f, l),
        )
        status = "indexed"
    write_ref_marker(es, org, repo, ref_type, ref_for_id, commit_sha, commit_date_iso, files_count, lines_count)
    reporter.finish(unit, status, files_count, lines_count)


def index_one(
    es: Elasticsearch,
    org: str,
    repo: str,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
    cache_root=None,
    ephemeral: bool = False,
) -> None:
    """
    Index a single ref (branch, tag, or commit) of one repo into an existing ES client.
    At most one of branch/tag/commit should be set; none means the remote's default branch.
    Used by the single-repo CLI path (`run`); the config-driven batch path (`run_config`)
    clones once per repo and calls `index_ref_in_dir` directly for each ref.

    With `cache_root` set and `ephemeral` false, the clone is kept under the cache dir and
    fetched on later runs; otherwise it is a throwaway temp clone.

    Progress is reported through `reporter`/`unit`; when omitted, a quiet no-op reporter
    and a fresh unit are used so the function is still callable standalone.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
        unit = Unit(org=org, repo=repo, ref=branch or tag or commit, kind=kind)

    reporter.start(unit)

    # Pre-clone skip: if the ref is already fully indexed, skip before paying the clone cost.
    skip, ref_for_id, _ = pre_clone_skip(es, org, repo, branch, tag, commit, force)
    if skip:
        unit.ref = ref_for_id
        reporter.finish(unit, "skipped")
        return

    reporter.set_stage(unit, "cloning")
    with prepared_repo(org, repo, cache_root, ephemeral) as repo_dir:
        if repo_dir is None:
            reporter.finish(unit, "locked", detail="another sourcerer run holds this repo's cache lock")
            return
        index_ref_in_dir(es, org, repo, repo_dir, branch, tag, commit, force, reporter, unit)


def run(
    repo_spec: str,
    branch: str | None,
    tag: str | None,
    commit: str | None,
    url: str,
    api_key: str | None,
    username: str | None,
    password: str | None,
    force: bool = False,
    quiet: bool = False,
    cache_dir: str | None = None,
    ephemeral: bool = False,
) -> None:
    parts = repo_spec.split("/", 1)
    if len(parts) != 2 or not all(parts):
        click.echo(f"Error: repo_spec must be '<org>/<repo>', got: {repo_spec!r}", err=True)
        sys.exit(1)
    org, repo = parts

    refs = {k: v for k, v in [("branch", branch), ("tag", tag), ("commit", commit)] if v}
    if len(refs) > 1:
        click.echo("Error: specify at most one of -b/--branch, -t/--tag, -c/--commit", err=True)
        sys.exit(1)

    es = make_client(url, api_key, username, password)
    cache_root = None if ephemeral else resolve_cache_root(cache_dir)

    kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
    unit = Unit(org=org, repo=repo, ref=branch or tag or commit, kind=kind)
    reporter = make_reporter(quiet)
    with handle_interrupts(), reporter, bulk_indexing_settings(es):
        reporter.set_plan([unit])
        try:
            index_one(
                es, org, repo, branch, tag, commit, force,
                reporter=reporter, unit=unit, cache_root=cache_root, ephemeral=ephemeral,
            )
        except FileNotFoundError as e:
            reporter.finish(unit, "error", detail=str(e))
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            reporter.finish(unit, "error", detail=f"git command failed (exit {e.returncode}): {e.stderr or ''}")
            sys.exit(1)
        except ValueError as e:
            reporter.finish(unit, "error", detail=str(e))
            sys.exit(1)
        except ES_ERRORS as e:
            reporter.finish(unit, "error", detail=f"Elasticsearch request failed: {e}")
            sys.exit(1)


def run_config(
    config_path: str,
    url: str,
    api_key: str | None,
    username: str | None,
    password: str | None,
    force: bool = False,
    quiet: bool = False,
    cache_dir: str | None = None,
    ephemeral: bool = False,
    prune: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Index every (repo, ref) the config selects. First list the remote branches and tags for
    every entry and keep the ones matching its glob patterns (an omitted or empty list selects
    nothing for that ref type), building the full plan; then index each selected ref. One bad
    ref or repo is reported and the batch continues; the command exits non-zero if any failed.

    With `prune` set, once ALL indexing is complete, run the prune step over the same config
    (equivalent to a following `sourcerer prune --config`), deleting already-indexed refs that
    now fall outside their retention policies. Skipped if the run was aborted (Ctrl-C).

    With `dry_run` set, nothing is written to Elasticsearch: the cached repos are cloned/fetched
    to resolve real commits + dates, and a combined report shows what would be indexed and (with
    `prune`) what the post-index prune step would delete. See `report.dry_run_config`.
    """
    try:
        entries = _load_config(config_path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        click.echo(f"Error: invalid config: {e}", err=True)
        sys.exit(1)

    # Look up a repo's selectors when resolving each ref's `since` floor below.
    cfg_by_repo = {(c.org, c.repo): c for c in entries}

    es = make_client(url, api_key, username, password)
    cache_root = None if ephemeral else resolve_cache_root(cache_dir)

    if dry_run:
        dry_run_config(es, entries, cfg_by_repo, cache_root, ephemeral, force, prune)
        return

    reporter = make_reporter(quiet)
    failures = 0

    with handle_interrupts(), reporter:
        # Phase 1: resolve the full plan (what will be indexed) up front so overall progress
        # has an accurate total and the user sees every org/repo/ref. Each entry needs two
        # `git ls-remote` round-trips, which are independent, so resolve up to
        # RESOLVE_CONCURRENCY entries at once. The completion counter is driven from this (main)
        # thread via as_completed, and results are gathered in submission order so the plan
        # order -- and Phase 2's grouping -- is identical to a serial resolve.
        units: list[Unit] = []
        with ThreadPoolExecutor(
            max_workers=max(1, min(_tuning().resolve_concurrency, len(entries) or 1))
        ) as pool:
            futures = [pool.submit(_resolve_entry, entry) for entry in entries]
            done = 0
            for _ in as_completed(futures):
                done += 1
                reporter.planning(f"resolving refs — {done}/{len(entries)} repos")
            for fut in futures:
                units.extend(fut.result())
        # Order the plan lexicographically by (org, repo, ref) regardless of config-file order,
        # so e.g. every elastic/elasticsearch ref precedes elastic/kibana, and a repo's refs go
        # by name. This drives Phase 2's grouping (dict insertion order) and the concurrent
        # dispatch order below -- repos are still indexed up to INDEX_REPO_CONCURRENCY at a time,
        # but they are started in lexicographical order. kind breaks ties between a same-named
        # branch and tag.
        units.sort(key=lambda u: (u.org, u.repo, u.ref or "", u.kind))
        reporter.set_plan(units)

        # Phase 2: index each selected ref, cloning each repo at most once. Group units by
        # (org, repo) -- preserving plan order, and collapsing a repo that appears across
        # multiple config entries. Each group is independent (its own clone + checkouts), so up
        # to INDEX_REPO_CONCURRENCY groups run concurrently, overlapping one repo's clone
        # (network/disk, GIL-releasing) with another's indexing. refresh is disabled across the
        # whole indexing phase (best-effort) for index-side bulk throughput.
        groups: dict[tuple[str, str], list[Unit]] = {}
        for unit in units:
            groups.setdefault((unit.org, unit.repo), []).append(unit)

        failures_lock = threading.Lock()

        def process_group(item: tuple[tuple[str, str], list[Unit]]) -> None:
            nonlocal failures
            # These run in pool worker threads, which never receive Ctrl-C themselves -- so they
            # bail by polling the abort flag. A group not yet started just returns; one in flight
            # stops at the next ref boundary (and index_repo's own poll stops a ref mid-stream).
            if _aborted.is_set():
                return
            (org, repo), group = item
            # 2a. Cheap per-ref skip for the whole group (no clone yet). A transient ES error
            # here fails just that ref (the skip check hits the cluster) and the batch goes on.
            pending: list[tuple[Unit, str | None, str | None, str | None]] = []
            for unit in group:
                if _aborted.is_set():
                    return
                reporter.start(unit)
                branch = unit.ref if unit.kind == "branch" else None
                tag = unit.ref if unit.kind == "tag" else None
                commit = unit.ref if unit.kind == "commit" else None
                try:
                    skip, ref_for_id, _ = pre_clone_skip(es, org, repo, branch, tag, commit, force)
                except ES_ERRORS as e:
                    with failures_lock:
                        failures += 1
                    reporter.finish(unit, "error", detail=str(e))
                    continue
                if skip:
                    unit.ref = ref_for_id
                    reporter.finish(unit, "skipped")
                else:
                    pending.append((unit, branch, tag, commit))

            if not pending:
                return  # whole repo already indexed -> no clone at all

            # 2b. Clone/fetch once, then check out and index each pending ref. A failure on one
            # ref -- git, bad value, or a transient ES timeout/connection drop -- is reported and
            # the remaining refs (and other repos) continue. If the persistent cache dir is locked
            # by another run, prepared_repo yields None and the whole repo is skipped this round.
            try:
                reporter.set_stage(pending[0][0], "cloning")
                with prepared_repo(org, repo, cache_root, ephemeral) as repo_dir:
                    if repo_dir is None:
                        for unit, _branch, _tag, _commit in pending:
                            reporter.finish(unit, "locked", detail="another sourcerer run holds this repo's cache lock")
                        return
                    # Reorder pending refs newest-first by creation date so more-recent refs
                    # are indexed first. creatordate is available now that the clone exists;
                    # it was not available during Phase 1 (ls-remote returns no timestamps).
                    # Refs absent from the map (unusual) sink to the end with key -1.
                    dates = ref_dates(repo_dir)
                    pending.sort(key=lambda p: dates.get((p[0].kind, p[0].ref or ""), -1), reverse=True)
                    # Reorder the reporter's unit list to match, so the live
                    # display and actual processing order stay consistent.
                    reporter.reorder_group(org, repo, dates)
                    cfg = cfg_by_repo.get((org, repo))
                    now = datetime.datetime.now(datetime.timezone.utc)

                    # 2c. Prune-aware pre-filter. Among the pending refs, drop those `since`
                    # excludes and those `retain` would immediately delete, so we never
                    # pay to index a ref that prune would remove. This runs the SAME planner as
                    # `sourcerer prune`, over the candidate refs, so "indexed" and "survives
                    # prune" can't diverge. count/version/prerelease are cohort-relative, so the
                    # whole group's refs are scored together (e.g. v8.14.0-.2 are dropped once
                    # v8.14.3 is in the candidate set via patch:0).
                    prospective: list[tuple[Unit, str | None, str | None, str | None, Marker]] = []
                    for unit, branch, tag, commit in pending:
                        ref_type = "branch" if branch else "tag" if tag else "commit"
                        info = _rev_info(repo_dir, unit.ref)
                        sha, cd = info if info else ("", None)
                        # For a commit selector, unit.ref is still the (possibly short) SHA
                        # prefix from the config; once resolved, key the marker on the full SHA
                        # so it lines up with what write_ref_marker stores and prune matches
                        # against. Fall back to the prefix if resolution failed (checkout will
                        # then fail too, reported as a per-unit error).
                        marker_ref = sha if (ref_type == "commit" and sha) else unit.ref
                        floor = _effective_since_floor(cfg, repo_dir, ref_type, unit.ref, now)
                        if floor is not None and cd is not None and cd < floor:
                            reporter.finish(unit, "skipped", detail=f"before since floor {floor.date()}")
                            continue
                        prospective.append((unit, branch, tag, commit, Marker(
                            id=f"{ref_type}:{marker_ref}", ref=marker_ref, ref_type=ref_type,
                            commit=sha, commit_date=cd, indexed_at=now,
                        )))

                    doomed: set[str] = set()
                    if cfg is not None and prospective:
                        decisions = plan_repo([m for *_, m in prospective], cfg, now)
                        doomed = {d.marker.id for d in decisions if d.action == "delete"}

                    for unit, branch, tag, commit, marker in prospective:
                        if _aborted.is_set():
                            break
                        if marker.id in doomed:
                            reporter.finish(unit, "skipped", detail="pruned by retain")
                            continue
                        try:
                            index_ref_in_dir(
                                es, org, repo, repo_dir, branch, tag, commit,
                                force, reporter, unit,
                            )
                        except KeyboardInterrupt:
                            break  # aborted mid-ref -- stop this group, leave the rest unmarked
                        except (FileNotFoundError, subprocess.CalledProcessError, ValueError, *ES_ERRORS) as e:
                            with failures_lock:
                                failures += 1
                            reporter.finish(unit, "error", detail=str(e))
            except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as e:
                # Clone failed: fail every still-pending ref of this repo, continue others.
                for unit, _branch, _tag, _commit in pending:
                    if unit.status is None:
                        with failures_lock:
                            failures += 1
                        reporter.finish(unit, "error", detail=str(e))

        with bulk_indexing_settings(es), ThreadPoolExecutor(
            max_workers=max(1, _tuning().index_repo_concurrency)
        ) as pool:
            # Drain the iterator so any unexpected error surfaces; expected per-ref/clone
            # errors are handled inside process_group and counted in `failures`.
            list(pool.map(process_group, groups.items()))

    # Prune only after ALL indexing is complete, so a ref newly indexed this run is present in
    # the refs index before it's scored for retention (e.g. it can be the cohort-newest that
    # supersedes an older sibling). Skipped on abort: the plan is incomplete, so its retention
    # cohorts would be too.
    if prune and not _aborted.is_set():
        prune_cmd.run(config_path, url, api_key, username, password, quiet=quiet)

    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)
