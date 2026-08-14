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
    empty_content_indices, enumerate_ref_tuples, gather_content_by_index,
    gather_content_commit_tuples, gather_intended_index_by_commit, list_sourcerer_indices,
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
    # Class E: content indices already drained to zero docs (a fully-pruned repo, or a suffix
    # a->b migration that emptied ~repo^a while its identity still has markers at ~repo^b).
    empty = empty_content_indices(es, index_names)
    return plan_orphans(index_names, ref_tuples, content_tuples,
                        content_by_index_commit=content_by_index,
                        intended_index_by_commit=intended_by_commit,
                        empty_index_names=empty)


def execute_orphan_deletions(es: Elasticsearch, plan: OrphanPlan) -> tuple[int, int, int, int, int]:
    """Apply an OrphanPlan in ascending-cost order: whole-index DELETEs first (Class A orphaned
    indices and Class E empty indices -- both near-instant), then the per-repo content
    delete_by_query (Class B -- expensive, one call per content index per repo), then the
    per-index stale-location delete_by_query (Class D -- the index.level/suffix migration backstop,
    one call per index holding stale docs), then a single delete_by_query against sourcerer-v2-refs
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
