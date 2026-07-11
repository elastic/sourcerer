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
from .indices import FILES_INDEX_PREFIX, LINES_INDEX_PREFIX, REFS_INDEX
from .planner import Marker, parse_index_name

_COMPOSITE_PAGE_SIZE = 1000


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_markers(
    es: Elasticsearch, org: str, repo: str,
    ref_type: str | None = None, ref: str | None = None,
) -> list[Marker]:
    """Every ref marker for a repo (optionally narrowed to one ref), read via scan. Returns
    [] if the refs index does not exist yet. Shared by `prune` and the inline head-only GC."""
    filters = [{"term": {"git.org": org}}, {"term": {"git.repo": repo}}]
    if ref_type is not None:
        filters.append({"term": {"git.ref_type": ref_type}})
    if ref is not None:
        filters.append({"term": {"git.ref": ref}})
    body = {"query": {"bool": {"filter": filters}}}
    out: list[Marker] = []
    try:
        for hit in scan(es, index=REFS_INDEX, query=body, preserve_order=False):
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
    """Every physical sourcerer-v1-{files,lines} index name, via one `_cat/indices` read.
    Read-side wildcards are safe on every cluster (including Serverless) -- only wildcard
    *deletes* are the compatibility problem this sweep is designed to avoid, so this is the
    one place a wildcard is used. sourcerer-v1-refs is a single global index (never
    per-org/repo), so it's intentionally excluded from this pattern."""
    pattern = f"{FILES_INDEX_PREFIX}*,{LINES_INDEX_PREFIX}*"
    rows = es.cat.indices(index=pattern, h="index", format="json")
    return [row["index"] for row in rows]


def enumerate_ref_tuples(es: Elasticsearch) -> set[tuple[str, str, str]]:
    """Every distinct (git.org, git.repo, git.commit) tuple recorded in sourcerer-v1-refs, via
    a paginated composite aggregation (safe over an unbounded number of distinct tuples).
    Returns an empty set if the refs index doesn't exist yet."""
    return _composite_org_repo_commit_tuples(es, REFS_INDEX)


def enumerate_content_commits(es: Elasticsearch, index: str) -> set[tuple[str, str, str]]:
    """Every distinct (git.org, git.repo, git.commit) tuple with at least one doc in `index` (a
    single files or lines physical index). Returns an empty set if `index` doesn't exist."""
    return _composite_org_repo_commit_tuples(es, index)


def _composite_org_repo_commit_tuples(es: Elasticsearch, index: str) -> set[tuple[str, str, str]]:
    out: set[tuple[str, str, str]] = set()
    after: dict | None = None
    while True:
        composite: dict = {
            "size": _COMPOSITE_PAGE_SIZE,
            "sources": [
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
            out.add((b["key"]["org"], b["key"]["repo"], b["key"]["commit"]))
        after = agg.get("after_key")
        if after is None:
            return out


def gather_content_commit_tuples(es: Elasticsearch, index_names: list[str]) -> set[tuple[str, str, str]]:
    """Union of (org, repo, commit) tuples with content docs, across every org~repo-granularity
    files/lines index in `index_names`. Org-only and org~repo~commit indices are skipped here --
    Class-A (orphan_indices) already judges those by name alone, with no per-doc commit variety
    to aggregate over that isn't already implied by the name itself."""
    out: set[tuple[str, str, str]] = set()
    for name in index_names:
        parsed = parse_index_name(name, FILES_INDEX_PREFIX, LINES_INDEX_PREFIX)
        if parsed is None or parsed.repo is None or parsed.commit is not None:
            continue
        out |= enumerate_content_commits(es, name)
    return out
