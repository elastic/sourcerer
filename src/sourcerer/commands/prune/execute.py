# sourcerer/commands/prune/execute.py
# Every code path that actually deletes something -- retention prune and the orphan sweep --
# lives here. commands/index/ only ever reads from Elasticsearch (it writes content during
# indexing, but never deletes), so every deleting code path stays in one place.

# Standard packages
from collections import defaultdict

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk

# App packages
from ...indices import REFS_INDEX, files_index, lines_index
from ...planner import OrphanPlan, content_delete_set, plan_orphans
from ...queries import (
    empty_content_indices, enumerate_ref_tuples, fetch_complete_commits_for_repo,
    fetch_stale_markers, gather_content_by_index, gather_content_commit_tuples,
    gather_incremental_content_by_index, gather_intended_incremental_index_by_ref,
    gather_intended_index_by_commit, list_sourcerer_indices,
)


def delete_commit_content(
    es: Elasticsearch, host: str, org: str, repo: str, sha: str, index_names=None,
) -> None:
    """Fire an async delete-by-query for all content docs (lines + files) belonging to one
    commit SHA in a repo. No-op if the index doesn't exist yet. Called both from
    execute_deletions (marker-driven path) and from run_ref's no-marker content-only fallback.

    `index_names` targets a specific set of physical index names (the source's index.level/suffix
    location, reconstructed from the pruned markers' recorded routing). When None, falls back to the
    historical repo-level files/lines names -- correct for legacy content and the default level."""
    if index_names is None:
        index_names = (lines_index(host, org, repo), files_index(host, org, repo))
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
) -> tuple[int, int]:
    """Apply a retention prune plan. Deletes the marker docs, then fires async delete-by-query
    for each pruned commit's content at the physical index the pruned markers recorded (index.
    level/suffix routing), reconstructed via files_index/lines_index. Content is dropped ONLY for
    commits that no surviving marker references (the commit-safety guard, in
    content_delete_set). Returns (markers_deleted, commits_content_dropped)."""
    deletes = [d.marker for d in decisions if d.action == "delete"]
    if not deletes:
        return (0, 0)
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
    for index_name in sorted({n for names in commit_level_indices.values() for n in names}):
        delete_index(es, index_name)
    for sha in dbq_commits:
        delete_commit_content(es, host, org, repo, sha, index_names=locations_by_commit.get(sha))
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


def execute_stale_marker_deletions(es: Elasticsearch) -> tuple[int, int]:
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

    Returns (stale_markers_deleted, stale_commits_content_dropped).

    Crash-safety: if a crash occurs between step 3 and step 4, the stale marker doc remains and
    this function is idempotent -- it will reattempt the same cleanup on the next prune run.
    Unreachable content left by a crashed step 3 is also reclaimed by the orphan sweep."""
    stale_hits = fetch_stale_markers(es)
    if not stale_hits:
        return (0, 0)

    # Group stale markers by repo for the per-repo commit-safety check.
    from collections import defaultdict
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
    for (host, org, repo), hits in by_repo.items():
        # Fetch commits currently protected by any complete marker in this repo.
        protected_commits = fetch_complete_commits_for_repo(es, host, org, repo)
        for hit in hits:
            commit = hit["_source"].get("git", {}).get("commit")
            if commit and commit not in protected_commits:
                # Safe to delete this commit's content.
                delete_commit_content(es, host, org, repo, commit)
                commits_dropped += 1
            # Delete the stale marker doc regardless (the content is either dropped or still
            # protected by another marker, so the stale marker itself is never useful again).
            try:
                es.delete(index=REFS_INDEX, id=hit["_id"])
            except NotFoundError:
                pass  # already gone -- race with another prune run
            markers_deleted += 1

    return (markers_deleted, commits_dropped)


def plan_orphans_now(es: Elasticsearch) -> OrphanPlan:
    """Take one read-only snapshot of the cluster (index names, ref tuples, content tuples) via
    the read helpers in sourcerer/queries.py, and compute the full orphan plan from it via
    planner.plan_orphans. Single-pass by construction: every input is gathered before any
    orphan is computed, so the plan reflects one consistent view rather than re-reading state
    between classes."""
    index_names = list_sourcerer_indices(es)
    ref_tuples = enumerate_ref_tuples(es)
    content_tuples = gather_content_commit_tuples(es)
    # Per-index content + each commit's marker-intended locations feed Class-D stale-location
    # detection (the index.level/suffix migration backstop).
    content_by_index = gather_content_by_index(es, index_names)
    intended_by_commit = gather_intended_index_by_commit(es)
    # Class D-I: incremental (ref-addressed, commit-less) stale-location detection -- the
    # incremental mirror of Class D, since the commit-keyed sweep can't see incremental docs.
    incremental_content_by_index = gather_incremental_content_by_index(es, index_names)
    intended_incremental_by_ref = gather_intended_incremental_index_by_ref(es)
    # Class E: content indices already drained to zero docs (a fully-pruned repo, or a suffix
    # a->b migration that emptied ~repo^a while its identity still has markers at ~repo^b).
    empty = empty_content_indices(es, index_names)
    return plan_orphans(index_names, ref_tuples, content_tuples,
                        content_by_index_commit=content_by_index,
                        intended_index_by_commit=intended_by_commit,
                        empty_index_names=empty,
                        incremental_content_by_index=incremental_content_by_index,
                        intended_incremental_index_by_ref=intended_incremental_by_ref)


def execute_orphan_deletions(es: Elasticsearch, plan: OrphanPlan) -> tuple[int, int, int, int, int]:
    """Apply an OrphanPlan in ascending-cost order: whole-index DELETEs first (Class A orphaned
    indices and Class E empty indices -- both near-instant), then the per-repo content
    delete_by_query (Class B -- expensive, one call per content index per repo), then the
    per-index stale-location delete_by_query (Class D -- the index.level/suffix migration backstop,
    one call per index holding stale docs), then a single delete_by_query against sourcerer-v3-refs
    covering every orphaned marker tuple across every repo (Class C -- refs is tiny, so one
    combined query costs one merge cycle instead of one per repo). Repo keys are (host, org, repo).
    Returns (indices_deleted, content_commits_dropped, marker_commits_dropped,
    stale_commits_dropped, empty_indices_deleted)."""
    indices_deleted = 0
    for name in plan.orphan_index_names:
        if delete_index(es, name):
            indices_deleted += 1

    # Class E: empty content indices (disjoint from Class A by construction in plan_orphans).
    empty_deleted = 0
    for name in plan.empty_index_names:
        if delete_index(es, name):
            empty_deleted += 1

    commits_dropped = 0
    for (host, org, repo), commits in plan.orphan_content.items():
        commits_dropped += len(commits)
        for idx in (lines_index(host, org, repo), files_index(host, org, repo)):
            try:
                es.delete_by_query(
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
            es.delete_by_query(
                index=index_name,
                query={"bool": {"filter": [
                    {"terms": {"git.commit": sorted(commits)}},
                ]}},
                conflicts="proceed",
                refresh=False,
                scroll_size=5000,
                wait_for_completion=False,
            )
        except NotFoundError:
            pass

    # Class D-I: stale-location incremental content (ref-addressed, no git.commit). Mirrors Class D
    # but keyed on (host, org, repo, ref_type, ref) tuples -- the commit-keyed filter above cannot
    # match incremental docs whose git.commit is absent.
    for index_name, ref_tuples in plan.orphan_stale_incremental.items():
        stale_dropped += len(ref_tuples)
        for (host, org, repo, ref_type, ref) in ref_tuples:
            try:
                es.delete_by_query(
                    index=index_name,
                    query={"bool": {"filter": [
                        {"term": {"git.host": host}},
                        {"term": {"git.org": org}},
                        {"term": {"git.repo": repo}},
                        {"term": {"git.ref_type": ref_type}},
                        {"term": {"git.ref": ref}},
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
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"terms": {"git.commit": sorted(commits)}},
            ]}}
            for (host, org, repo), commits in plan.orphan_marker_commits.items()
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

    return indices_deleted, commits_dropped, markers_dropped, stale_dropped, empty_deleted
