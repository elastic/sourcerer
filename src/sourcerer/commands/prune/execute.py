# sourcerer/commands/prune/execute.py
# Every code path that actually deletes something -- retention prune and the orphan sweep --
# lives here. commands/index/ only ever reads from Elasticsearch (it writes content during
# indexing, but never deletes), so every deleting code path stays in one place.

# Standard packages
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk

# App packages
from ...indices import REFS_INDEX, files_index, lines_index
from ...planner import OrphanPlan, content_delete_set, index_in_scope, parse_index_name, plan_orphans
from ...progress import PruneReporter
from ...queries import (
    empty_content_indices, enumerate_ref_repo_identities,
    enumerate_snapshot_ref_commit_tuples, fetch_complete_commits_for_repo,
    fetch_stale_markers, gather_content_and_incremental_by_index, gather_intended_locations,
    list_sourcerer_indices,
)


def delete_commit_content(
    es: Elasticsearch, host: str, org: str, repo: str, sha: str, index_names=None,
) -> list[str]:
    """Fire an async delete-by-query for all content docs (lines + files) belonging to one
    commit SHA in a repo. No-op if the index doesn't exist yet. Called both from
    execute_deletions (marker-driven path) and from run_ref's no-marker content-only fallback.

    `index_names` targets a specific set of physical index names (the source's index.level/suffix
    location, reconstructed from the pruned markers' recorded routing). When None, falls back to the
    historical repo-level files/lines names -- correct for legacy content and the default level.

    Returns the ES task id ("<node>:<task>") of each delete_by_query submitted (skipping any
    index that doesn't exist), so callers can report and/or poll them: `wait_for_completion=False`
    means the count returned by execute_deletions/execute_orphan_deletions is a submission target,
    not yet a fact -- see `wait_for_deletions`."""
    if index_names is None:
        index_names = (lines_index(host, org, repo), files_index(host, org, repo))
    task_ids: list[str] = []
    for idx in index_names:
        try:
            resp = es.delete_by_query(
                index=idx,
                query={"bool": {"filter": [
                    {"term": {"git.host": host}},
                    {"term": {"git.org": org}},
                    {"term": {"git.repo": repo}},
                    {"term": {"git.commit": sha}},
                ]}},
                conflicts="proceed",
                refresh=False,
                scroll_size=5000,
                wait_for_completion=False,
            )
            task = resp.get("task")
            if task:
                task_ids.append(task)
        except NotFoundError:
            pass
    return task_ids


def delete_commit_from_indices(
    es: Elasticsearch, host: str, org: str, repo: str, sha: str, index_names,
) -> None:
    """Delete one commit's content docs from a specific set of physical index names.

    Used by the index command's index.level/suffix MIGRATION (write-new -> flip-marker ->
    delete-old): after a source's content has been re-ingested at its new index and the marker
    flipped to point there, the stale copy at the OLD index name(s) is dropped. Ordering (delete
    only after the marker flip) guarantees a crash never removes live data; any old copy left by a
    crash mid-migration is reclaimed later by the prune stale-location sweep. No-op for an index
    that doesn't exist (e.g. already drained). Kept here so every delete path lives in execute.py."""
    for idx in index_names:
        try:
            es.delete_by_query(
                index=idx,
                query={"bool": {"filter": [
                    {"term": {"git.host": host}},
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


def execute_deletions(
    es: Elasticsearch, host: str, org: str, repo: str, decisions,
) -> tuple[int, int, list[str]]:
    """Apply a retention prune plan. Deletes the marker docs, then fires async delete-by-query
    for each pruned commit's content at the physical index the pruned markers recorded (index.
    level/suffix routing), reconstructed via files_index/lines_index. Content is dropped ONLY for
    commits that no surviving marker references (the commit-safety guard, in
    content_delete_set). Returns (markers_deleted, commits_content_dropped, submitted_task_ids)
    -- the marker deletes are synchronous (a real bulk DELETE) but the content
    delete_by_query calls are async submissions; see delete_commit_content."""
    deletes = [d.marker for d in decisions if d.action == "delete"]
    if not deletes:
        return (0, 0, [])
    drop_commits = content_delete_set(decisions)

    # Reconstruct where each dropped commit's content lives from the pruned markers' routing. A
    # commit may be referenced by several deleted markers; union their intended locations so every
    # physical copy is targeted (per-source routing means two sources of a repo can differ). A
    # legacy marker (level "repo"/None) reconstructs to the historical repo-level name.
    locations_by_commit: dict[str, set[str]] = defaultdict(set)
    for m in deletes:
        if m.commit not in drop_commits:
            continue
        level, suffix = getattr(m, "index_level", "repo"), getattr(m, "index_suffix", None)
        locations_by_commit[m.commit].add(files_index(host, org, repo, m.commit, level, suffix))
        locations_by_commit[m.commit].add(lines_index(host, org, repo, m.commit, level, suffix))

    # Partition drop_commits by whether every deleted marker for the commit used level="commit".
    # A commit-level index is guaranteed to hold exactly that one commit's content, so a whole-
    # index DELETE is safe and near-instant. Any commit that has even one non-commit-level marker
    # falls back to delete_by_query (it shares its index with other commits).
    commit_level_indices: dict[str, set[str]] = {}   # sha -> physical index names (files + lines)
    dbq_commits: set[str] = set()
    for sha in drop_commits:
        markers_for_sha = [m for m in deletes if m.commit == sha]
        level_values = {getattr(m, "index_level", "repo") for m in markers_for_sha}
        if level_values == {"commit"}:
            # All deleted markers agree: this commit lives in its own index/indices.
            commit_level_indices[sha] = locations_by_commit.get(sha, set())
        else:
            dbq_commits.add(sha)

    bulk(
        es,
        ({"_op_type": "delete", "_index": REFS_INDEX, "_id": m.id} for m in deletes),
        raise_on_error=False,
        refresh=False,
    )

    # Whole-index DELETEs first (near-instant); then async delete_by_query for shared indices.
    task_ids: list[str] = []
    for index_name in sorted({n for names in commit_level_indices.values() for n in names}):
        delete_index(es, index_name)
    for sha in dbq_commits:
        task_ids.extend(delete_commit_content(es, host, org, repo, sha, index_names=locations_by_commit.get(sha)))
    return (len(deletes), len(drop_commits), task_ids)


def delete_index(es: Elasticsearch, name: str) -> bool:
    """DELETE one physical index by its exact (non-wildcard) name -- near-instant, and safe on
    clusters that reject wildcard index deletes. Returns True if deleted, False if it was
    already gone (e.g. a race with another prune/index run)."""
    try:
        es.indices.delete(index=name)
        return True
    except NotFoundError:
        return False


def execute_stale_marker_deletions(es: Elasticsearch) -> tuple[int, int, list[str]]:
    """Reclaim snapshot content that was superseded by a mode switch to incremental indexing.

    The flip-status switchover in `index_incremental_branch_in_dir` marks old snapshot ref-name
    markers as status="stale" BEFORE publishing the incremental join doc as "complete", so the
    two-complete-docs fan-out window never opens. Stale markers carry git.commit (so their content
    can be reclaimed) but are invisible to all content tools (which gate on status=="complete").

    This function:
      1. Enumerates all status="stale" snapshot markers cluster-wide.
      2. Groups them by (host, org, repo) so the commit-safety guard can be applied per-repo.
      3. For each stale marker, drops its snapshot content ONLY if the commit is not referenced
         by any surviving complete marker in the same repo (the same commit-safety guard used by
         execute_deletions via content_delete_set). A commit shared with another snapshot ref is
         left in place.
      4. Deletes the stale marker doc from sourcerer-v3-refs.

    Returns (stale_markers_deleted, stale_commits_content_dropped, submitted_task_ids) --
    the marker deletes are synchronous but the content delete_by_query calls (via
    delete_commit_content) are async submissions.

    Crash-safety: if a crash occurs between step 3 and step 4, the stale marker doc remains and
    this function is idempotent -- it will reattempt the same cleanup on the next prune run.
    Unreachable content left by a crashed step 3 is also reclaimed by the orphan sweep."""
    stale_hits = fetch_stale_markers(es)
    if not stale_hits:
        return (0, 0, [])

    # Group stale markers by repo for the per-repo commit-safety check.
    by_repo: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for hit in stale_hits:
        g = hit["_source"].get("git", {})
        host = g.get("host", "")
        org = g.get("org", "")
        repo = g.get("repo", "")
        if host and org and repo:
            by_repo[(host, org, repo)].append(hit)

    markers_deleted = 0
    commits_dropped = 0
    task_ids: list[str] = []
    for (host, org, repo), hits in by_repo.items():
        # Fetch commits currently protected by any complete marker in this repo.
        protected_commits = fetch_complete_commits_for_repo(es, host, org, repo)
        for hit in hits:
            commit = hit["_source"].get("git", {}).get("commit")
            if commit and commit not in protected_commits:
                # Safe to delete this commit's content.
                task_ids.extend(delete_commit_content(es, host, org, repo, commit))
                commits_dropped += 1
            # Delete the stale marker doc regardless (the content is either dropped or still
            # protected by another marker, so the stale marker itself is never useful again).
            try:
                es.delete(index=REFS_INDEX, id=hit["_id"])
            except NotFoundError:
                pass  # already gone -- race with another prune run
            markers_deleted += 1

    return (markers_deleted, commits_dropped, task_ids)


def plan_orphans_now(
    es: Elasticsearch,
    reporter: PruneReporter | None = None,
    host: str | None = None,
    org: str | None = None,
    repo: str | None = None,
) -> OrphanPlan:
    """Take one read-only snapshot of the cluster (index names, ref tuples, content tuples) via
    the read helpers in sourcerer/queries.py, and compute the full orphan plan from it via
    planner.plan_orphans. Single-pass by construction: every input is gathered before any
    orphan is computed, so the plan reflects one consistent view rather than re-reading state
    between classes.

    `reporter` (default a no-op PruneReporter) is given a named phase for each gather step, so a
    slow prune reports progress instead of going silent for minutes; see progress.py.

    `host`/`org`/`repo` (default None = unscoped, the historical full-cluster sweep) narrow the
    swept index names and refs queries to one host/org/repo, per index_in_scope's rule that a
    scoped run must skip any physical index coarser than the given scope (a host-only or host~org
    index whose orphan status can't be judged correctly on partial identity data)."""
    reporter = reporter or PruneReporter()
    scoped = host is not None or org is not None or repo is not None

    with reporter.phase("listing indices") as p:
        index_names = list_sourcerer_indices(es)
        if scoped:
            index_names = [
                name for name in index_names
                if (parsed := parse_index_name(name)) is not None and index_in_scope(parsed, host, org, repo)
            ]
        p.set_detail(f"{len(index_names):,} indices" + (" (scoped)" if scoped else ""))

    # The remaining gathers are mutually independent reads -- the refs-index scans (snapshot
    # commit tuples, broad identity, intended locations) don't depend on index_names, and the
    # per-index content/empty-index scans don't depend on the refs index -- so they run
    # concurrently instead of sequentially. Each is still a single pass internally (Phase 2's
    # query-count cuts), so this parallelization shrinks wall-clock without re-reading any
    # state between classes; `plan_orphans` below only runs once every result is in hand.
    with ThreadPoolExecutor(max_workers=3) as pool:
        refs_future = pool.submit(_gather_refs, es, reporter, host, org, repo)
        content_future = pool.submit(_gather_content, es, index_names, reporter)
        empty_future = pool.submit(_gather_empty, es, index_names, reporter)
        ref_tuples, ref_identities, intended_by_commit, intended_incremental_by_ref = refs_future.result()
        content_by_index, incremental_content_by_index = content_future.result()
        empty = empty_future.result()

    # Phase 2, item 1: derive the union-of-content-indices commit set in Python instead of a
    # separate pair of full-corpus paginated aggregations (the old gather_content_commit_tuples) --
    # it is exactly the union of gather_content_and_incremental_by_index's per-index values, since
    # index_names is the set of indices behind the files/lines aliases by construction.
    content_tuples = set().union(*content_by_index.values()) if content_by_index else set()

    return plan_orphans(index_names, ref_tuples, content_tuples,
                        content_by_index_commit=content_by_index,
                        intended_index_by_commit=intended_by_commit,
                        empty_index_names=empty,
                        incremental_content_by_index=incremental_content_by_index,
                        intended_incremental_index_by_ref=intended_incremental_by_ref,
                        ref_identity_tuples=ref_identities)


def _gather_refs(es: Elasticsearch, reporter: PruneReporter, host, org, repo):
    """Refs-index reads for plan_orphans_now's "refs scan" phase: the snapshot-only commit
    tuples (Class B/C), the broad snapshot+delta identity set (Class A), and both
    intended-location maps (Class D/D-I) -- all read via the refs alias/index, so they run as one
    named phase even though they're several distinct queries under the hood.

    Snapshot-only (mode != "delta", status != "stale") commit tuples feed the Class-B/C
    commit-level comparison. Delta join docs carry git.commit = live HEAD but their content is
    ref-addressed (no git.commit on content docs), so including them would mark every delta HEAD
    as a Class-C orphan marker and delete the join doc -- causing a full re-index on every run.
    Stale markers are owned by execute_stale_marker_deletions and excluded here to avoid
    double-handling.

    The broad (host, org, repo) identity set -- snapshot AND delta -- feeds Class-A
    orphan_indices. A delta-only repo (delta branch, no snapshot tags) has no entries in
    ref_tuples, so without a separate identity set its content index would be falsely flagged
    orphan:index and deleted."""
    with reporter.phase("refs scan") as p:
        ref_tuples = enumerate_snapshot_ref_commit_tuples(es, host, org, repo)
        ref_identities = enumerate_ref_repo_identities(es, host, org, repo)
        intended_by_commit, intended_incremental_by_ref = gather_intended_locations(es, host, org, repo)
        p.set_detail(f"{len(ref_identities):,} repo(s)")
    return ref_tuples, ref_identities, intended_by_commit, intended_incremental_by_ref


def _gather_content(es: Elasticsearch, index_names: list[str], reporter: PruneReporter):
    """Per-index content gather for plan_orphans_now's "content by index" phase -- the single
    most expensive part of planning (one composite aggregation per content index), so it reports
    determinate progress (N of len(index_names)) as each index's aggregation completes."""
    with reporter.phase("content by index", total=len(index_names)) as p:
        content_by_index, incremental_by_index = gather_content_and_incremental_by_index(
            es, index_names, progress_cb=p.advance,
        )
    return content_by_index, incremental_by_index


def _gather_empty(es: Elasticsearch, index_names: list[str], reporter: PruneReporter):
    """Empty-index check for plan_orphans_now's "empty-index check" phase (Class E)."""
    with reporter.phase("empty-index check") as p:
        empty = empty_content_indices(es, index_names)
        p.set_detail(f"{len(empty):,} empty")
    return empty


def execute_orphan_deletions(es: Elasticsearch, plan: OrphanPlan) -> tuple[int, int, int, int, int, list[str]]:
    """Apply an OrphanPlan in ascending-cost order: whole-index DELETEs first (Class A orphaned
    indices and Class E empty indices -- both near-instant), then the per-repo content
    delete_by_query (Class B -- expensive, one call per content index per repo), then the
    per-index stale-location delete_by_query (Class D -- the index.level/suffix migration backstop,
    one call per index holding stale docs), then a single delete_by_query against sourcerer-v3-refs
    covering every orphaned marker tuple across every repo (Class C -- refs is tiny, so one
    combined query costs one merge cycle instead of one per repo). Repo keys are (host, org, repo).
    Returns (indices_deleted, content_commits_dropped, marker_commits_dropped,
    stale_commits_dropped, empty_indices_deleted, submitted_task_ids) -- the whole-index DELETEs
    (Class A/E) and the marker delete_by_query (Class C, against the tiny refs index) are
    synchronous in practice, but every content delete_by_query (Class B/D/D-I) is an async
    submission; `submitted_task_ids` collects all of them (Class C's included, since it too is
    `wait_for_completion=False`) so a caller can report/poll them via `wait_for_deletions`."""
    indices_deleted = 0
    for name in plan.orphan_index_names:
        if delete_index(es, name):
            indices_deleted += 1

    # Class E: empty content indices (disjoint from Class A by construction in plan_orphans).
    empty_deleted = 0
    for name in plan.empty_index_names:
        if delete_index(es, name):
            empty_deleted += 1

    task_ids: list[str] = []

    commits_dropped = 0
    for (host, org, repo), commits in plan.orphan_content.items():
        commits_dropped += len(commits)
        for idx in (lines_index(host, org, repo), files_index(host, org, repo)):
            try:
                resp = es.delete_by_query(
                    index=idx,
                    query={"bool": {"filter": [
                        {"term": {"git.host": host}},
                        {"term": {"git.org": org}},
                        {"term": {"git.repo": repo}},
                        {"terms": {"git.commit": sorted(commits)}},
                    ]}},
                    conflicts="proceed",
                    refresh=False,
                    scroll_size=5000,
                    wait_for_completion=False,
                )
                if resp.get("task"):
                    task_ids.append(resp["task"])
            except NotFoundError:
                pass

    # Class D: stale-location content. Each index holds docs whose commit's markers point
    # elsewhere (a migration whose old-copy delete never completed). Delete those exact commits
    # from that exact index by name -- discover-by-enumeration means the name is real, so no
    # discover-before-delete reconstruction is needed here.
    stale_dropped = 0
    for index_name, commits in plan.orphan_stale.items():
        stale_dropped += len(commits)
        try:
            resp = es.delete_by_query(
                index=index_name,
                query={"bool": {"filter": [
                    {"terms": {"git.commit": sorted(commits)}},
                ]}},
                conflicts="proceed",
                refresh=False,
                scroll_size=5000,
                wait_for_completion=False,
            )
            if resp.get("task"):
                task_ids.append(resp["task"])
        except NotFoundError:
            pass

    # Class D-I: stale-location incremental content (ref-addressed, no git.commit). Mirrors Class D
    # but keyed on (host, org, repo, ref_type, ref_pattern) tuples -- the commit-keyed filter above
    # cannot match incremental docs whose git.commit is absent.
    for index_name, ref_tuples in plan.orphan_stale_incremental.items():
        stale_dropped += len(ref_tuples)
        for (host, org, repo, ref_type, ref_pattern) in ref_tuples:
            try:
                resp = es.delete_by_query(
                    index=index_name,
                    query={"bool": {"filter": [
                        {"term": {"git.host": host}},
                        {"term": {"git.org": org}},
                        {"term": {"git.repo": repo}},
                        {"term": {"git.ref_type": ref_type}},
                        {"term": {"git.ref_pattern": ref_pattern}},
                    ]}},
                    conflicts="proceed",
                    refresh=False,
                    scroll_size=5000,
                    wait_for_completion=False,
                )
                if resp.get("task"):
                    task_ids.append(resp["task"])
            except NotFoundError:
                pass

    markers_dropped = sum(len(commits) for commits in plan.orphan_marker_commits.values())
    if plan.orphan_marker_commits:
        should = [
            {"bool": {"filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"terms": {"git.commit": sorted(commits)}},
            ]}}
            for (host, org, repo), commits in plan.orphan_marker_commits.items()
        ]
        try:
            resp = es.delete_by_query(
                index=REFS_INDEX,
                query={"bool": {"should": should, "minimum_should_match": 1}},
                conflicts="proceed",
                refresh=False,
                scroll_size=5000,
                wait_for_completion=False,
            )
            if resp.get("task"):
                task_ids.append(resp["task"])
        except NotFoundError:
            pass

    return indices_deleted, commits_dropped, markers_dropped, stale_dropped, empty_deleted, task_ids


def wait_for_deletions(
    es: Elasticsearch, task_ids: list[str], reporter: PruneReporter | None = None, poll_interval: float = 1.0,
) -> dict[str, dict]:
    """Block until every async delete_by_query task in `task_ids` reports `completed: True` via
    `es.tasks.get`, turning the fire-and-forget submission from execute_deletions/
    execute_orphan_deletions/execute_stale_marker_deletions into a genuinely accurate "done"
    signal (backs `prune --wait`). Polls all still-pending tasks once per `poll_interval` seconds
    rather than waiting on them one at a time, since they run concurrently on the cluster.

    Returns {task_id: status} where `status` is the task's ES "status" sub-object (fields
    include `total`, `deleted`, `version_conflicts`, `failures`) -- the actual outcome, as
    opposed to the target counts execute_*_deletions returned at submission time.

    A task that has already expired from the tasks index by the time this polls it (fast
    completion + GC beat the first poll) is treated as done with an empty status, since ES no
    longer has anything to report for it."""
    reporter = reporter or PruneReporter()
    if not task_ids:
        return {}
    results: dict[str, dict] = {}
    with reporter.phase("waiting for deletions", total=len(task_ids)) as p:
        pending = set(task_ids)
        while pending:
            for task_id in list(pending):
                try:
                    resp = es.tasks.get(task_id=task_id)
                except NotFoundError:
                    results[task_id] = {}
                    pending.discard(task_id)
                    p.advance()
                    continue
                if resp.get("completed"):
                    results[task_id] = resp.get("task", {}).get("status", {})
                    pending.discard(task_id)
                    p.advance()
            if pending:
                time.sleep(poll_interval)
    return results
