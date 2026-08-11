# sourcerer/queries.py
# Read-only Elasticsearch queries over the sourcerer indices, shared by `index` (idempotency
# guards, dry-run preview) and `prune` (orphan sweep, retention). The detection/planning logic
# itself is pure and lives in planner.py; these are the thin, mockable wrappers that gather
# its inputs from a real cluster. Nothing here ever writes -- deletion lives in
# commands/prune/execute.py, and indexing writes live in commands/index/documents.py.

# Standard packages
import datetime

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import scan

# App packages
from .indices import FILES_ALIAS, LINES_ALIAS, REFS_ALIAS, files_index, lines_index
from .planner import Marker

_COMPOSITE_PAGE_SIZE = 1000


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_markers(
    es: Elasticsearch, host: str, org: str, repo: str,
    ref_type: str | None = None, ref: str | None = None,
) -> list[Marker]:
    """Every ref marker for a repo (optionally narrowed to one ref), read via scan. Returns
    [] if the refs index does not exist yet. Shared by `prune` and the inline head-only GC."""
    filters = [
        {"term": {"git.host": host}},
        {"term": {"git.org": org}},
        {"term": {"git.repo": repo}},
    ]
    if ref_type is not None:
        filters.append({"term": {"git.ref_type": ref_type}})
    if ref is not None:
        filters.append({"term": {"git.ref": ref}})
    body = {"query": {"bool": {"filter": filters}}}
    out: list[Marker] = []
    try:
        for hit in scan(es, index=REFS_ALIAS, query=body, preserve_order=False):
            src = hit["_source"]
            g = src.get("git", {})
            out.append(Marker(
                id=hit["_id"],
                ref=g.get("ref"),
                ref_type=g.get("ref_type"),
                commit=g.get("commit"),
                commit_date=_parse_dt(g.get("commit_date")),
                indexed_at=_parse_dt(src.get("indexed_at")),
            ))
    except NotFoundError:
        return []
    return out


# --- Orphan sweep: ES-facing read helpers --------------------------------------------------
# The detection logic itself (orphan_indices/orphan_content_commits/orphan_markers/
# plan_orphans) is pure and lives in planner.py; these are the thin, mockable, READ-ONLY
# wrappers that gather its inputs from the cluster. The functions that apply a plan (actually
# delete anything) live in commands/prune/execute.py, so every deleting code path is in one
# place.


def list_sourcerer_indices(es: Elasticsearch) -> list[str]:
    """Every physical content index behind the read aliases.

    The orphan sweep needs concrete backing names to delete a single index, but discovers them
    through the aliases so it never reads a versioned index pattern directly.
    """
    names: set[str] = set()
    for alias in (FILES_ALIAS, LINES_ALIAS):
        try:
            names.update(es.indices.get_alias(name=alias))
        except NotFoundError:
            pass
    return sorted(names)


def enumerate_ref_tuples(es: Elasticsearch) -> set[tuple[str, str, str, str]]:
    """Every distinct (git.host, git.org, git.repo, git.commit) tuple recorded in the refs alias,
    via a paginated composite aggregation (safe over an unbounded number of distinct tuples).
    Returns an empty set if the refs index doesn't exist yet."""
    return _composite_host_org_repo_commit_tuples(es, REFS_ALIAS)


def enumerate_content_commits(es: Elasticsearch, index: str) -> set[tuple[str, str, str, str]]:
    """Every distinct (git.host, git.org, git.repo, git.commit) tuple with at least one doc in
    `index` (a single files or lines physical index). Returns an empty set if `index` doesn't
    exist."""
    return _composite_host_org_repo_commit_tuples(es, index)


def _composite_host_org_repo_commit_tuples(es: Elasticsearch, index: str) -> set[tuple[str, str, str, str]]:
    out: set[tuple[str, str, str, str]] = set()
    after: dict | None = None
    while True:
        composite: dict = {
            "size": _COMPOSITE_PAGE_SIZE,
            "sources": [
                {"host": {"terms": {"field": "git.host"}}},
                {"org": {"terms": {"field": "git.org"}}},
                {"repo": {"terms": {"field": "git.repo"}}},
                {"commit": {"terms": {"field": "git.commit"}}},
            ],
        }
        if after is not None:
            composite["after"] = after
        try:
            resp = es.search(index=index, size=0, aggs={"tuples": {"composite": composite}})
        except NotFoundError:
            return out
        agg = resp["aggregations"]["tuples"]
        buckets = agg["buckets"]
        if not buckets:
            return out
        for b in buckets:
            out.add((b["key"]["host"], b["key"]["org"], b["key"]["repo"], b["key"]["commit"]))
        after = agg.get("after_key")
        if after is None:
            return out


def gather_content_commit_tuples(es: Elasticsearch) -> set[tuple[str, str, str, str]]:
    """Union of (host, org, repo, commit) tuples with content docs across the content read
    aliases."""
    return (
        enumerate_content_commits(es, FILES_ALIAS)
        | enumerate_content_commits(es, LINES_ALIAS)
    )


_FULL_SHA_LEN = 40
_MIN_PREFIX_LEN = 7


def resolve_content_commit(
    es: Elasticsearch, host: str, org: str, repo: str, prefix: str,
) -> set[str]:
    """Resolve a commit hash (full 40-char SHA or ≥7-char prefix) to the set of distinct
    ``git.commit`` values that actually have content docs in the repo's files or lines index.

    Returns an empty set if nothing matches or the index does not exist. The caller is
    responsible for rejecting an ambiguous result (>1 element) and the no-content case (0
    elements).

    Uses a composite aggregation on the per-repo physical indices rather than the read aliases
    so that a prefix query can be issued without scanning the entire cluster-wide alias.  For a
    full 40-char SHA a ``term`` filter is used instead of ``prefix``; otherwise a ``prefix``
    query is added alongside the host/org/repo filter."""
    # Build query: host/org/repo scoped, plus a prefix or term filter on git.commit.
    if len(prefix) == _FULL_SHA_LEN:
        commit_clause: dict = {"term": {"git.commit": prefix}}
    else:
        commit_clause = {"prefix": {"git.commit": prefix}}

    query = {"bool": {"filter": [
        {"term": {"git.host": host}},
        {"term": {"git.org": org}},
        {"term": {"git.repo": repo}},
        commit_clause,
    ]}}

    found: set[str] = set()
    for idx in (lines_index(host, org, repo), files_index(host, org, repo)):
        if found:
            # Already resolved from the first index; no need to scan the second.
            break
        after: dict | None = None
        while True:
            composite: dict = {
                "size": _COMPOSITE_PAGE_SIZE,
                "sources": [{"commit": {"terms": {"field": "git.commit"}}}],
            }
            if after is not None:
                composite["after"] = after
            try:
                resp = es.search(
                    index=idx, size=0,
                    query=query,
                    aggs={"commits": {"composite": composite}},
                )
            except NotFoundError:
                break
            agg = resp["aggregations"]["commits"]
            buckets = agg["buckets"]
            if not buckets:
                break
            for b in buckets:
                found.add(b["key"]["commit"])
            after = agg.get("after_key")
            if after is None:
                break
    return found
