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
import json
import pathlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Third-party packages
import click
import yaml
from elasticsearch import Elasticsearch

# App packages
from ...hosts import resolve_hosts
from ...planner import Marker, plan_repo
from ...progress import ProgressReporter, Unit, make_reporter
from ...indices import files_index, lines_index
from ...queries import check_ref_key_uniqueness
from ...utils import ES_ERRORS, make_client
from ..prune import command as prune_cmd
from ..prune.execute import delete_commit_from_indices
from .documents import index_incremental_paths, index_repo
from .git import (
    checkout_branch,
    checkout_ref,
    commit_date,
    count_tracked_files,
    default_branch,
    list_branch_commits,
    plan_changes,
    prepared_repo,
    ref_dates,
    resolve_cache_root,
    resolve_commit,
    _rev_info,
)
from .markers import (
    backfill_repo, build_ref_id, commits_with_content, content_present,
    count_incremental_branch_docs, delete_incremental_branch, delete_incremental_paths,
    fully_indexed_counts, markers_status_by_id, _needs_index, pre_clone_skip,
    read_incremental_ref, recorded_routing, refresh_incremental_content, should_index,
    write_incremental_failed, write_incremental_indexing, write_incremental_ready,
    write_indexing_marker, write_ref_marker, write_snapshot_join_doc,
)
from .report import dry_run_config
from .schedule import filter_config_by_schedule
from .runtime import _aborted, _tuning, bulk_indexing_settings, handle_interrupts
from .selection import _effective_since_floor, _load_config, _resolve_entry


# The index template files, reused by the upgrade backfill to migrate the mapping of EXISTING
# physical indices (a put_index_template change alone only affects indices created afterward).
_INDEX_TEMPLATES_DIR = pathlib.Path(__file__).resolve().parents[2] / "elastic" / "index_templates"


def _load_template_mapping(name: str) -> dict | None:
    try:
        body = json.loads((_INDEX_TEMPLATES_DIR / name).read_text())
    except OSError:
        return None
    return body.get("template", {}).get("mappings")


def _load_refs_mapping() -> dict | None:
    return _load_template_mapping("sourcerer-v3-refs.json")


def _load_files_mapping() -> dict | None:
    return _load_template_mapping("sourcerer-v3-files.json")


def _load_lines_mapping() -> dict | None:
    return _load_template_mapping("sourcerer-v3-lines.json")


def _run_uniqueness_gate(es: Elasticsearch, host: str, org: str, repo: str) -> bool:
    """Post-index uniqueness gate (INV-011): every distinct `git.ref_key` in this repo's
    content must resolve to exactly one `sourcerer-v3-refs` join doc. Prints the offending
    ref_key(s) to stderr and returns False on any violation; True (silent) when the invariant
    holds."""
    offending = check_ref_key_uniqueness(es, host, org, repo)
    if offending:
        click.echo(
            f"Error: {host}/{org}/{repo}: {len(offending)} git.ref_key value(s) missing or "
            f"duplicated in sourcerer-v3-refs: {', '.join(offending)}",
            err=True,
        )
        return False
    return True


def _branch_has_since(branch_name: str, cfg) -> bool:
    """True if any selector for this branch has a date/age/commit/ref `since` that would
    trigger a history walk. Version-based `since` (a tag name used as a floor for tag
    selectors) does not apply to branches and is excluded. Used to bypass the pre-clone
    tip-only skip so the post-clone walk can find unindexed historical commits."""
    if cfg is None:
        return False
    for sel in cfg.selectors:
        if sel.ref_type != "branch":
            continue
        if sel.matches("branch", branch_name) is None:
            continue
        if sel.since is not None and sel.since_version_floor() is None:
            return True
    return False


def index_ref_in_dir(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    repo_dir,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
    retry_window: datetime.timedelta | None = None,
    at_commit: str | None = None,
    index_level: str | None = None,
    index_suffix: str | None = None,
) -> None:
    """
    Index a single ref of one repo into an already-cloned `repo_dir`. Checks out the ref
    (the clone holds every branch/tag, so one clone serves any number of refs), then
    runs the authoritative SHA guard and indexes, tags, or records the ref. At most one of
    branch/tag/commit should be set; none means the remote's default branch.

    `at_commit`: when set on a branch, check out this specific commit SHA instead of the
    branch tip, but record the ref marker as `ref_type=branch, ref=<branch>`. Used by the
    branch history walk (`since` on a branch) to index each historical commit while keeping
    it associated with the branch so `resolve_head`/`retain.count`-per-branch work correctly.

    `index_level`/`index_suffix`: this source's index.* routing (see specs/sourcerer-yml.md),
    determining the physical files/lines index the content is written to. When None, they fall
    back to the unit's routing (or the repo-level default). If a prior complete marker for this
    exact ref recorded a DIFFERENT routing, this is a migration: content is re-ingested at the new
    index, the marker is flipped to point there, and only then is the old copy deleted.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
        unit = Unit(host=host, org=org, repo=repo, ref=branch or tag or commit, kind=kind)
    # Effective routing: explicit args win, else the unit carries it from its selector.
    level = index_level if index_level is not None else unit.index_level
    suffix = index_suffix if index_suffix is not None else unit.index_suffix

    # The repo is already cloned/fetched (callers clone once, then reuse this dir for every ref);
    # this only checks out the ref, so the stage is "checkout", not "cloning". Branches (and the
    # default branch) are checked out at their fetched remote tip so a reused clone can't land on
    # a stale local branch; tags/commits are immutable and checked out directly.
    reporter.set_stage(unit, "checkout")
    if at_commit:
        # Branch history walk: check out a specific historical commit, not the branch tip.
        checkout_ref(repo_dir, at_commit)
        commit_sha = at_commit
    elif branch:
        checkout_branch(repo_dir, branch)
        commit_sha = resolve_commit(repo_dir)
    elif tag or commit:
        checkout_ref(repo_dir, tag or commit)
        commit_sha = resolve_commit(repo_dir)
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
    # For historical branch commits (at_commit set), keep the display ref that was set by the
    # caller (e.g. "main@abc12345") so progress output shows which commit is being indexed.
    # For all other refs, update unit.ref to the resolved ref name (resolves default branch).
    if not at_commit:
        unit.ref = ref_for_id

    # The physical files/lines index this ref's content should live in, given the source's routing.
    expected_routing = (level, suffix)
    new_f = files_index(host, org, repo, commit_sha, level, suffix)
    new_l = lines_index(host, org, repo, commit_sha, level, suffix)

    # Where this ref's content CURRENTLY lives, per its prior complete marker (None if never
    # indexed / no complete marker). A recorded routing that differs from `expected_routing` means
    # the source's index.level/suffix changed since the last run -> migrate to the new index and
    # clean up the old copy afterwards.
    old_routing = None if force else recorded_routing(es, host, org, repo, ref_type, ref_for_id, commit_sha)
    migrating = old_routing is not None and old_routing != expected_routing

    # Post-clone guard: authoritative, location-aware SHA check (covers -c, any ls-remote
    # peeling mismatch, AND a routing change -- see should_index's expected_routing).
    if not force and not should_index(
        es, host, org, repo, ref_type, ref_for_id, commit_sha,
        retry_window=retry_window, expected_routing=expected_routing,
    ):
        reporter.finish(unit, "skipped")
        return

    # Content is keyed by commit, so another ref may already have fully indexed this exact
    # snapshot. If a complete sibling marker exists AND its content is still present AT THE TARGET
    # index, reuse that marker's counts and record this ref without rewriting the (large) content
    # docs. The location guard matters once routing is per-source: a sibling's content sitting at a
    # DIFFERENT index must not let us record a marker pointing at `new_f` while no docs exist there.
    #
    # Counts come from the sibling marker (fully_indexed_counts), NOT an es.count over the content
    # indices: refresh is disabled during the bulk phase (runtime.bulk_indexing_settings), so a
    # sibling that ingested this commit earlier in the same run isn't search-visible and an
    # es.count would spuriously return 0 -- the refs index is refresh-enabled, so its marker is the
    # honest source. content_present guards the GC'd case: a surviving complete marker whose
    # content was deleted must be re-ingested, not recorded again with stale counts. If content is
    # gone (or a same-run sibling's docs aren't refresh-visible yet) we fall through and re-index
    # -- safe, since doc ids are idempotent. "Fully" is load-bearing: an aborted run leaves partial
    # content but no complete marker, so fully_indexed_counts returns None and we fall through.
    reuse_counts = None
    if not force:
        marker_counts = fully_indexed_counts(es, host, org, repo, commit_sha)
        if marker_counts is not None and content_present(es, host, org, repo, commit_sha, at_index=new_f):
            reuse_counts = marker_counts

    if reuse_counts is not None:
        status = "tagged" if tag else "recorded"
        files_count, lines_count = reuse_counts
    else:
        reporter.set_total_files(unit, count_tracked_files(repo_dir))
        reporter.set_stage(unit, "indexing")
        # Mark the ref as in-progress before ingest so the schedule gate can detect
        # that this scope is currently being indexed by another run and skip it.
        # The terminal write_ref_marker (status:'complete') overwrites this doc in place.
        write_indexing_marker(es, host, org, repo, ref_type, ref_for_id, commit_sha,
                              commit_date_iso, index_level=level, index_suffix=suffix)
        files_count, lines_count = index_repo(
            es, host, org, repo, repo_dir, commit_sha,
            on_progress=lambda f, l: reporter.update_counts(unit, f, l),
            index_level=level, index_suffix=suffix,
        )
        status = "indexed"
    # write-new -> FLIP MARKER -> delete-old: the marker now points at the new location before any
    # old copy is deleted, so a crash between here and the delete below leaves stale (not missing)
    # data that the prune stale-location sweep reclaims.
    write_ref_marker(es, host, org, repo, ref_type, ref_for_id, commit_sha, commit_date_iso,
                     files_count, lines_count, index_level=level, index_suffix=suffix)
    # Every snapshot unit -- whether freshly indexed or reusing a sibling's already-indexed
    # content -- must have its `_id = commit` refs join doc so the universal join query resolves
    # a commit for this content regardless of which ref reached it (INV-004).
    # refresh=True so the post-index uniqueness gate (_run_uniqueness_gate, INV-011) sees this join
    # doc immediately instead of racing the refs index's default (~1s) refresh interval: the bulk
    # context manager refreshes the CONTENT indices on exit but not refs, so an unrefreshed write
    # here would make the gate read this ref_key's content but miss its join doc and false-fail
    # "missing". Mirrors write_incremental_ready (refresh=True) and backfill_refs_join_docs.
    write_snapshot_join_doc(es, host, org, repo, ref_type, ref_for_id, commit_sha, commit_date_iso,
                            refresh=True)
    if migrating:
        # Reconstruct the OLD index name from the prior marker's routing and drop this commit's
        # stale copy there. Commit-safety (another surviving ref sharing the commit) is respected
        # because this is a commit-scoped delete-by-query, not a whole-index DELETE; an emptied
        # old index is later reclaimed by the prune orphan sweep.
        old_level, old_suffix = old_routing  # type: ignore[misc]
        old_f = files_index(host, org, repo, commit_sha, old_level, old_suffix)
        old_l = lines_index(host, org, repo, commit_sha, old_level, old_suffix)
        if {old_f, old_l} != {new_f, new_l}:
            delete_commit_from_indices(es, host, org, repo, commit_sha, (old_l, old_f))
    reporter.finish(unit, status, files_count, lines_count)


def index_incremental_branch_in_dir(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    repo_dir,
    branch: str,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
) -> None:
    """Advance one incremental (ref-addressed) branch source in an already-cloned `repo_dir`.

    Reads the branch's prior completed commit (its refs join doc, `_id = ref_key`), checks out
    the fetched branch tip, and either:
      - does nothing (already at the completed commit and not `--force`),
      - does a full rebuild (first index, `--force`, or a missing diff base -- INV-007): delete
        the whole branch namespace, then index every currently-tracked path, or
      - does a delta update: `git diff --name-status` (via `plan_changes`) between the prior and
        new commit, deleting only the paths git reports removed/changed and (re)indexing only the
        paths git reports added/changed (INV-008 -- scoped by the exact `ref_key`, never a whole
        namespace sweep).
    The refs join doc is published `indexing` before any mutation and `complete` only after the
    content deletes/indexes and a refresh all succeed (INV-006); a raised exception instead
    records `write_incremental_failed` and leaves the completed pointer untouched, then
    re-raises so the caller's per-unit error handling reports it.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        unit = Unit(host=host, org=org, repo=repo, ref=branch, kind="branch", update="incremental")

    reporter.set_stage(unit, "checkout")
    checkout_branch(repo_dir, branch)
    new_sha = resolve_commit(repo_dir)
    commit_date_iso = commit_date(repo_dir)

    prior = read_incremental_ref(es, host, org, repo, branch)
    old_sha = None if force else (prior.get("git", {}).get("commit") if prior else None)

    if old_sha == new_sha and not force:
        reporter.finish(unit, "no-changes")
        return

    level = unit.index_level
    suffix = unit.index_suffix

    reporter.set_stage(unit, "indexing")
    write_incremental_indexing(es, host, org, repo, branch, completed_commit=old_sha,
                               target_commit=new_sha, prior=prior)
    try:
        full_rebuild = old_sha is None or force
        if not full_rebuild:
            plan = plan_changes(repo_dir, old_sha, new_sha)
            full_rebuild = plan.base_missing

        if full_rebuild:
            delete_incremental_branch(es, host, org, repo, branch, index_level=level, index_suffix=suffix)
            reporter.set_total_files(unit, count_tracked_files(repo_dir))
            indexed_files, indexed_lines = index_incremental_paths(
                es, host, org, repo, repo_dir, branch, None,
                on_progress=lambda f, l: reporter.update_counts(unit, f, l),
                index_level=level, index_suffix=suffix,
            )
        else:
            delete_incremental_paths(es, host, org, repo, branch, plan.delete_paths,
                                     index_level=level, index_suffix=suffix)
            reporter.set_total_files(unit, len(plan.index_paths))
            indexed_files, indexed_lines = index_incremental_paths(
                es, host, org, repo, repo_dir, branch, plan.index_paths,
                on_progress=lambda f, l: reporter.update_counts(unit, f, l),
                index_level=level, index_suffix=suffix,
            )

        refresh_incremental_content(es, host, org, repo, index_level=level, index_suffix=suffix)
        files_count, lines_count = count_incremental_branch_docs(
            es, host, org, repo, branch, index_level=level, index_suffix=suffix,
        )
        write_incremental_ready(es, host, org, repo, branch, new_sha, commit_date_iso,
                                files_count, lines_count)
    except KeyboardInterrupt:
        write_incremental_failed(es, host, org, repo, branch, completed_commit=old_sha,
                                 target_commit=new_sha, error="interrupted", prior=prior)
        raise
    except Exception as e:
        write_incremental_failed(es, host, org, repo, branch, completed_commit=old_sha,
                                 target_commit=new_sha, error=str(e), prior=prior)
        raise
    reporter.finish(unit, "indexed", indexed_files, indexed_lines)


def index_one(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    clone_url: str,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
    cache_root=None,
    ephemeral: bool = False,
    retry_window: datetime.timedelta | None = None,
) -> None:
    """
    Index a single ref (branch, tag, or commit) of one repo into an existing ES client.
    At most one of branch/tag/commit should be set; none means the remote's default branch.
    Used by the single-repo CLI path (`run`); the config-driven batch path (`run_config`)
    clones once per repo and calls `index_ref_in_dir` directly for each ref. `clone_url` is the
    resolved clone URL from the host registry.

    With `cache_root` set and `ephemeral` false, the clone is kept under the cache dir and
    fetched on later runs; otherwise it is a throwaway temp clone.

    Progress is reported through `reporter`/`unit`; when omitted, a quiet no-op reporter
    and a fresh unit are used so the function is still callable standalone.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
        unit = Unit(host=host, org=org, repo=repo, ref=branch or tag or commit, kind=kind)

    reporter.start(unit)

    # Pre-clone skip: if the ref is already fully indexed (or another run is actively
    # indexing it within the retry window), skip before paying the clone cost.
    skip, ref_for_id, _ = pre_clone_skip(
        es, host, org, repo, branch, tag, commit, clone_url, force, retry_window=retry_window,
        expected_routing=(unit.index_level, unit.index_suffix),
    )
    if skip:
        unit.ref = ref_for_id
        reporter.finish(unit, "skipped")
        return

    reporter.set_stage(unit, "cloning")
    with prepared_repo(host, org, repo, clone_url, cache_root, ephemeral) as repo_dir:
        if repo_dir is None:
            reporter.finish(unit, "locked", detail="another sourcerer run holds this repo's cache lock")
            return
        index_ref_in_dir(
            es, host, org, repo, repo_dir, branch, tag, commit, force, reporter, unit,
            retry_window=retry_window,
        )


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
    retry_window: datetime.timedelta | None = None,
    insecure: bool = False,
    no_backfill: bool = False,
) -> None:
    parts = repo_spec.split("/", 2)
    if len(parts) != 3 or not all(parts):
        click.echo(f"Error: repo_spec must be '<host>/<org>/<repo>', got: {repo_spec!r}", err=True)
        sys.exit(1)
    host, org, repo = parts

    refs = {k: v for k, v in [("branch", branch), ("tag", tag), ("commit", commit)] if v}
    if len(refs) > 1:
        click.echo("Error: specify at most one of -b/--branch, -t/--tag, -c/--commit", err=True)
        sys.exit(1)

    # The single-repo CLI path has no config, so it uses the built-in host registry. host
    # defaults to "github"; a caller can pass another built-in id.
    hosts = resolve_hosts(None)
    if host not in hosts:
        click.echo(f"Error: unknown git host {host!r}", err=True)
        sys.exit(1)
    clone_url = hosts[host].clone_url(org, repo)

    es = make_client(url, api_key, username, password, insecure=insecure)
    cache_root = None if ephemeral else resolve_cache_root(cache_dir)

    if not no_backfill:
        backfill_repo(
            es, host, org, repo, refs_mapping=_load_refs_mapping(),
            files_mapping=_load_files_mapping(), lines_mapping=_load_lines_mapping(),
        )

    kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
    unit = Unit(host=host, org=org, repo=repo, ref=branch or tag or commit, kind=kind)
    reporter = make_reporter(quiet)
    with handle_interrupts(), reporter, bulk_indexing_settings(es):
        reporter.set_plan([unit])
        try:
            index_one(
                es, host, org, repo, clone_url, branch, tag, commit, force,
                reporter=reporter, unit=unit, cache_root=cache_root, ephemeral=ephemeral,
                retry_window=retry_window,
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

    if not _run_uniqueness_gate(es, host, org, repo):
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
    retry_window: datetime.timedelta | None = None,
    insecure: bool = False,
    no_backfill: bool = False,
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
        config = _load_config(config_path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        click.echo(f"Error: invalid config: {e}", err=True)
        sys.exit(1)

    hosts = config.hosts
    es = make_client(url, api_key, username, password, insecure=insecure)
    cache_root = None if ephemeral else resolve_cache_root(cache_dir)

    # One-time upgrade backfill (default-on; --no-backfill opts out; skipped on --dry-run,
    # which promises no ES writes). Runs once per distinct (host, org, repo) in the config,
    # before the schedule gate, so it applies regardless of which sources are due this tick.
    if not no_backfill and not dry_run:
        refs_mapping = _load_refs_mapping()
        files_mapping = _load_files_mapping()
        lines_mapping = _load_lines_mapping()
        for repo_cfg in config.repos:
            backfill_repo(
                es, repo_cfg.host, repo_cfg.org, repo_cfg.repo, refs_mapping=refs_mapping,
                files_mapping=files_mapping, lines_mapping=lines_mapping,
            )

    # Schedule gate: determine which sources are due for indexing based on their configured
    # schedule and the refs index's record of when they were last indexed. Sources with no
    # schedule (or schedule "* * * * *") are always due; others are skipped until their next
    # scheduled tick fires. A source actively being indexed by another run is also skipped
    # (until RETRY_INTERVAL elapses, at which point it is assumed stuck and retried).
    #
    # Any source that has no schedule fields at all passes through unchanged (all due = True),
    # so the gate is transparent for configs that don't use scheduling.
    has_schedules = bool(config.schedules) or any(
        sel.schedule is not None
        for repo_cfg in config.repos
        for sel in repo_cfg.selectors
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    if has_schedules:
        filtered_config, schedule_decisions = filter_config_by_schedule(
            es, config, now, retry_window=retry_window
        )
    else:
        filtered_config = config
        schedule_decisions = []

    entries = filtered_config.repos

    if dry_run:
        # Look up a repo's selectors when resolving each ref's `since` floor below.
        cfg_by_repo = {(c.host, c.org, c.repo): c for c in entries}
        dry_run_config(
            es, entries, cfg_by_repo, hosts, cache_root, ephemeral, force, prune,
            schedule_decisions=schedule_decisions,
            retry_window=retry_window,
        )
        return

    # Look up a repo's selectors when resolving each ref's `since` floor below.
    cfg_by_repo = {(c.host, c.org, c.repo): c for c in entries}

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
            futures = [pool.submit(_resolve_entry, entry, hosts[entry.host]) for entry in entries]
            done = 0
            for _ in as_completed(futures):
                done += 1
                reporter.planning(f"resolving refs - {done}/{len(entries)} repos")
            for fut in futures:
                units.extend(fut.result())
        # Order the plan lexicographically by (org, repo, ref) regardless of config-file order,
        # so e.g. every elastic/elasticsearch ref precedes elastic/kibana, and a repo's refs go
        # by name. This drives Phase 2's grouping (dict insertion order) and the concurrent
        # dispatch order below -- repos are still indexed up to INDEX_REPO_CONCURRENCY at a time,
        # but they are started in lexicographical order. kind breaks ties between a same-named
        # branch and tag.
        units.sort(key=lambda u: (u.host, u.org, u.repo, u.ref or "", u.kind))
        reporter.set_plan(units)

        # Phase 2: index each selected ref, cloning each repo at most once. Group units by
        # (org, repo) -- preserving plan order, and collapsing a repo that appears across
        # multiple config entries. Each group is independent (its own clone + checkouts), so up
        # to INDEX_REPO_CONCURRENCY groups run concurrently, overlapping one repo's clone
        # (network/disk, GIL-releasing) with another's indexing. refresh is disabled across the
        # whole indexing phase (best-effort) for index-side bulk throughput.
        groups: dict[tuple[str, str, str], list[Unit]] = {}
        for unit in units:
            groups.setdefault((unit.host, unit.org, unit.repo), []).append(unit)

        failures_lock = threading.Lock()

        # Precompute once: the cutoff datetime before which an "indexing" marker is considered
        # stuck/abandoned and eligible for retry (used by the batch skip check below).
        indexing_cutoff = (now - retry_window) if retry_window is not None else None

        def process_group(item: tuple[tuple[str, str, str], list[Unit]]) -> None:
            nonlocal failures
            # These run in pool worker threads, which never receive Ctrl-C themselves -- so they
            # bail by polling the abort flag. A group not yet started just returns; one in flight
            # stops at the next ref boundary (and index_repo's own poll stops a ref mid-stream).
            if _aborted.is_set():
                return
            (host, org, repo), group = item
            clone_url = hosts[host].clone_url(org, repo)

            # Incremental branch units are split from the snapshot pre-clone/skip/retention flow
            # entirely: no cohort retention, no `since` history walk, no commit-addressed content
            # reuse -- each is a standalone two-phase delta update against its own prior state
            # (see index_incremental_branch_in_dir). Processed here, before the snapshot-only
            # `group` continues below with incremental units filtered out.
            incremental_units = [u for u in group if u.update == "incremental"]
            group = [u for u in group if u.update != "incremental"]
            for unit in incremental_units:
                reporter.start(unit)
            if incremental_units:
                try:
                    with prepared_repo(host, org, repo, clone_url, cache_root, ephemeral) as repo_dir:
                        if repo_dir is None:
                            for unit in incremental_units:
                                reporter.finish(
                                    unit, "locked",
                                    detail="another sourcerer run holds this repo's cache lock",
                                )
                        else:
                            for unit in incremental_units:
                                if _aborted.is_set():
                                    break
                                try:
                                    index_incremental_branch_in_dir(
                                        es, host, org, repo, repo_dir, unit.ref, force, reporter, unit,
                                    )
                                except KeyboardInterrupt:
                                    break
                                except (FileNotFoundError, subprocess.CalledProcessError,
                                        ValueError, *ES_ERRORS) as e:
                                    with failures_lock:
                                        failures += 1
                                    reporter.finish(unit, "error", detail=str(e))
                except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as e:
                    for unit in incremental_units:
                        if unit.status is None:
                            with failures_lock:
                                failures += 1
                            reporter.finish(unit, "error", detail=str(e))
            if not group:
                return

            # 2a. Cheap pre-clone skip for the whole group (no clone yet).
            #
            # Batched approach: for branch/tag units that carry a remote_sha from Phase 1's
            # ls-remote, compute all their ref_ids up front and fetch marker status in one ES
            # query per repo (instead of one per ref). Pinned-commit units have no remote_sha
            # and fall back to the per-ref pre_clone_skip path unchanged.
            #
            # On an ES error for the batched lookup, fall back to treating the whole group as
            # pending (clone + authoritative post-clone guard) rather than counting each ref as
            # a failure -- the post-clone should_index remains the correctness backstop.
            for unit in group:
                reporter.start(unit)

            if force:
                # --force bypasses all pre-clone checks; everything is pending.
                pending: list[tuple[Unit, str | None, str | None, str | None]] = []
                for unit in group:
                    if _aborted.is_set():
                        return
                    pending.append((
                        unit,
                        unit.ref if unit.kind == "branch" else None,
                        unit.ref if unit.kind == "tag" else None,
                        unit.ref if unit.kind == "commit" else None,
                    ))
            else:
                # Split into units we can check cheaply via the batched path (branch/tag with a
                # Phase-1 remote_sha) and units that need per-ref logic (commit selectors or
                # anything without a pre-resolved SHA).
                batchable: list[tuple[Unit, str]] = []   # (unit, remote_sha)
                fallback: list[Unit] = []
                for unit in group:
                    if unit.remote_sha is not None and unit.kind in ("branch", "tag"):
                        batchable.append((unit, unit.remote_sha))
                    else:
                        fallback.append(unit)

                # Batch ES lookup for batchable units.
                status_map: dict[str, dict] = {}
                content_commits: set[str] = set()
                batch_error: Exception | None = None
                if batchable:
                    ref_ids = [
                        build_ref_id(host, org, repo, unit.kind, unit.ref or "", sha)
                        for unit, sha in batchable
                    ]
                    try:
                        status_map = markers_status_by_id(es, ref_ids)
                        # For the complete markers, check whether their content is still present.
                        complete_commits = {
                            m["commit"] for m in status_map.values()
                            if m.get("status") == "complete" and m.get("commit")
                        }
                        content_commits = commits_with_content(es, host, org, repo, complete_commits)
                    except ES_ERRORS as e:
                        batch_error = e

                pending = []
                for unit, sha in batchable:
                    if _aborted.is_set():
                        return
                    branch = unit.ref if unit.kind == "branch" else None
                    tag = unit.ref if unit.kind == "tag" else None
                    if batch_error is not None:
                        # ES error -- treat as pending; post-clone guard is authoritative.
                        pending.append((unit, branch, tag, None))
                        continue
                    # A branch unit with a date/age/commit/ref `since` must not be pre-clone
                    # skipped even if its tip is already indexed: historical commits back to the
                    # floor may still need indexing (e.g. when `since` is first added to an
                    # existing source). The post-clone walk enumerates only the unindexed commits.
                    if branch and _branch_has_since(unit.ref, cfg_by_repo.get((host, org, repo))):
                        pending.append((unit, branch, tag, None))
                        continue
                    ref_id = build_ref_id(host, org, repo, unit.kind, unit.ref or "", sha)
                    # Pass the unit's routing so a source whose index.level/suffix changed since the
                    # last run is treated as needing (re)index (migration) instead of being
                    # pre-clone skipped on the strength of its now-stale-location content.
                    if _needs_index(ref_id, sha, status_map, content_commits, indexing_cutoff,
                                    expected_routing=(unit.index_level, unit.index_suffix)):
                        pending.append((unit, branch, tag, None))
                    else:
                        reporter.finish(unit, "skipped")

                # Per-ref fallback for commit selectors and any unit without a remote_sha.
                for unit in fallback:
                    if _aborted.is_set():
                        return
                    branch = unit.ref if unit.kind == "branch" else None
                    tag = unit.ref if unit.kind == "tag" else None
                    commit = unit.ref if unit.kind == "commit" else None
                    # Same bypass as the batchable path: a branch with a `since` history walk
                    # must not be pre-clone skipped even if its tip is already indexed.
                    if branch and _branch_has_since(unit.ref, cfg_by_repo.get((host, org, repo))):
                        pending.append((unit, branch, tag, commit))
                        continue
                    try:
                        skip, ref_for_id, _ = pre_clone_skip(
                            es, host, org, repo, branch, tag, commit, clone_url, force,
                            retry_window=retry_window,
                            expected_routing=(unit.index_level, unit.index_suffix),
                        )
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
                with prepared_repo(host, org, repo, clone_url, cache_root, ephemeral) as repo_dir:
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
                    reporter.reorder_group(host, org, repo, dates)
                    cfg = cfg_by_repo.get((host, org, repo))
                    now = datetime.datetime.now(datetime.timezone.utc)

                    # 2c. Prune-aware pre-filter. Among the pending refs, drop those `since`
                    # excludes and those `retain` would immediately delete, so we never
                    # pay to index a ref that prune would remove. This runs the SAME planner as
                    # `sourcerer prune`, over the candidate refs, so "indexed" and "survives
                    # prune" can't diverge. count/version/prerelease are cohort-relative, so the
                    # whole group's refs are scored together (e.g. v8.14.0-.2 are dropped once
                    # v8.14.3 is in the candidate set via patch:0).
                    #
                    # Branch history walk: a branch unit with a `since` floor is expanded into
                    # one entry per historical commit instead of using the single tip-date gate.
                    # `list_branch_commits` walks the first-parent mainline back to the floor.
                    # The original unit (which represents the branch tip in Phase 1's ls-remote
                    # skip pass) is replaced by per-commit units here; the retention pre-filter
                    # then trims the full cohort so only commits that would survive `retain` are
                    # actually indexed (no index-then-prune waste). A branch without `since` and
                    # non-branch refs are unchanged (single entry, tip-date gate as before).
                    prospective: list[tuple[Unit, str | None, str | None, str | None, str | None, Marker]] = []
                    for unit, branch, tag, commit in pending:
                        ref_type = "branch" if branch else "tag" if tag else "commit"
                        floor = _effective_since_floor(cfg, repo_dir, ref_type, unit.ref, now)

                        if branch and floor is not None:
                            # Expand the branch into its historical commits back to `floor`.
                            branch_commits = list_branch_commits(repo_dir, branch, floor)
                            if not branch_commits:
                                # Walk returned nothing (e.g. subprocess error or branch too
                                # new) -- fall through to the regular tip-only path below.
                                pass
                            else:
                                # The original tip unit (from Phase 1) becomes the first entry
                                # (newest commit); remaining historical commits each get a new
                                # Unit. All share kind="branch" so retain.count-per-branch works.
                                tip_sha, tip_cd = branch_commits[0]
                                unit.ref = branch   # ensure ref is the branch name, not a SHA
                                prospective.append((unit, branch, None, None, tip_sha, Marker(
                                    id=f"branch:{branch}:{tip_sha}", ref=branch, ref_type="branch",
                                    commit=tip_sha, commit_date=tip_cd, indexed_at=now,
                                )))
                                extra_units: list[Unit] = []
                                for sha, cd in branch_commits[1:]:
                                    hist_unit = Unit(
                                        host=host, org=org, repo=repo,
                                        ref=f"{branch}@{sha[:8]}", kind="branch",
                                        index_level=unit.index_level, index_suffix=unit.index_suffix,
                                    )
                                    extra_units.append(hist_unit)
                                    prospective.append((hist_unit, branch, None, None, sha, Marker(
                                        id=f"branch:{branch}:{sha}", ref=branch, ref_type="branch",
                                        commit=sha, commit_date=cd, indexed_at=now,
                                    )))
                                if extra_units:
                                    reporter.add_units(extra_units)
                                continue  # skip the non-branch / no-since path below

                        # Non-branch refs, branch without `since`, or branch whose walk returned
                        # nothing: single entry with the tip-date gate.
                        info = _rev_info(repo_dir, unit.ref)
                        sha, cd = info if info else ("", None)
                        # For a commit selector, unit.ref is still the (possibly short) SHA
                        # prefix from the config; once resolved, key the marker on the full SHA
                        # so it lines up with what write_ref_marker stores and prune matches
                        # against. Fall back to the prefix if resolution failed (checkout will
                        # then fail too, reported as a per-unit error).
                        marker_ref = sha if (ref_type == "commit" and sha) else unit.ref
                        if floor is not None and cd is not None and cd < floor:
                            reporter.finish(unit, "skipped", detail=f"before since floor {floor.date()}")
                            continue
                        prospective.append((unit, branch, tag, commit, None, Marker(
                            id=f"{ref_type}:{marker_ref}", ref=marker_ref, ref_type=ref_type,
                            commit=sha, commit_date=cd, indexed_at=now,
                        )))

                    doomed: set[str] = set()
                    if cfg is not None and prospective:
                        decisions = plan_repo([m for *_, m in prospective], cfg, now)
                        doomed = {d.marker.id for d in decisions if d.action == "delete"}

                    for unit, branch, tag, commit, at_commit, marker in prospective:
                        if _aborted.is_set():
                            break
                        if marker.id in doomed:
                            reporter.finish(unit, "skipped", detail="pruned by retain")
                            continue
                        try:
                            index_ref_in_dir(
                                es, host, org, repo, repo_dir, branch, tag, commit,
                                force, reporter, unit, retry_window=retry_window,
                                at_commit=at_commit,
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

    # Post-index uniqueness gate (INV-011), one distinct repo at a time, skipped on abort (the
    # plan is incomplete). Every offending repo's ref_key(s) are reported before exiting.
    if not _aborted.is_set():
        distinct_repos = {(c.host, c.org, c.repo) for c in entries}
        for host, org, repo in sorted(distinct_repos):
            if not _run_uniqueness_gate(es, host, org, repo):
                failures += 1

    # Prune only after ALL indexing is complete, so a ref newly indexed this run is present in
    # the refs index before it's scored for retention (e.g. it can be the cohort-newest that
    # supersedes an older sibling). Skipped on abort: the plan is incomplete, so its retention
    # cohorts would be too.
    if prune and not _aborted.is_set():
        prune_cmd.run(config_path, url, api_key, username, password, quiet=quiet, insecure=insecure)

    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)
