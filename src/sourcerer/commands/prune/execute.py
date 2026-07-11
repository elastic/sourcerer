# sourcerer/commands/prune/execute.py
# Every code path that actually deletes something -- retention prune and the orphan sweep --
# lives here. commands/index/ only ever reads from Elasticsearch (it writes content during
# indexing, but never deletes), so every deleting code path stays in one place.

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import bulk

# App packages
from ...indices import REFS_INDEX, files_index, lines_index
from ...planner import OrphanPlan, content_delete_set, plan_orphans
from ...queries import enumerate_ref_tuples, gather_content_commit_tuples, list_sourcerer_indices


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
    the read helpers in sourcerer/queries.py, and compute the full orphan plan from it via
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
