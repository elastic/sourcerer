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
                # Legacy markers omit these -> repo-level default (where their content lives).
                index_level=src.get("index_level") or "repo",
                index_suffix=(src.get("index_suffix") or None),
            ))
    except NotFoundError:
        return []
    return out


def fetch_stale_markers(es: Elasticsearch) -> list[dict]:
    """Return all refs docs with status="stale" across all repos. Each element is the raw
    Elasticsearch hit dict (keys: _id, _source). Called by the prune command to reclaim snapshot
    content that was switched to incremental mode (the flip-status switchover writes "stale"
    markers before publishing the incremental join doc as "complete", so these never re-appear in
    any content-tool query, which all gate on status=="complete")."""
    try:
        hits = []
        for hit in scan(es, index=REFS_ALIAS,
                        query={"query": {"term": {"status": "stale"}}},
                        preserve_order=False):
            hits.append(hit)
        return hits
    except NotFoundError:
        return []


def fetch_complete_commits_for_repo(es: Elasticsearch, host: str, org: str, repo: str) -> set[str]:
    """Return the set of git.commit values referenced by any complete (non-stale) marker in this
    repo. Used by the stale-marker reclamation step to apply the commit-safety guard: a commit
    still held by a surviving complete marker must not have its content deleted."""
    try:
        resp = es.search(
            index=REFS_ALIAS, size=0,
            query={"bool": {"filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"term": {"status": "complete"}},
                {"exists": {"field": "git.commit"}},
            ]}},
            aggs={"commits": {"terms": {"field": "git.commit", "size": 10000}}},
        )
        return {b["key"] for b in resp["aggregations"]["commits"]["buckets"]}
    except NotFoundError:
        return set()


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


def empty_content_indices(es: Elasticsearch, index_names: list[str]) -> list[str]:
    """The subset of `index_names` that are sourcerer content indices with ZERO docs.

    Feeds the empty-index sweep: an index drained to nothing (every commit pruned, or an
    index.suffix change that moved all content to a sibling index whose git identity still has
    markers -- so orphan_indices' identity test doesn't flag it) is safe to DELETE outright,
    since there is nothing to lose. Guarded by parse_index_name so only real sourcerer
    files/lines indices are ever considered -- an unrelated empty index in the cluster (or the
    refs index) is never touched. es.count over refs-settled state is reliable here because prune
    reads after its own async deletes are a separate concern from the write path.

    Returns the names in the given order; an index that vanished between listing and counting
    (NotFound) is simply skipped."""
    out: list[str] = []
    for name in index_names:
        if parse_index_name(name) is None:
            continue
        try:
            if int(es.count(index=name)["count"]) == 0:
                out.append(name)
        except NotFoundError:
            continue
    return out


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


def gather_content_by_index(
    es: Elasticsearch, index_names: list[str],
) -> dict[str, set[tuple[str, str, str, str]]]:
    """Per physical index, the distinct (host, org, repo, commit) tuples with content docs in it.

    Feeds Class-D stale-location detection (planner.orphan_stale_content): to decide a doc is
    stale we must know WHICH physical index holds it, so this enumerates each backing index by name
    rather than through the union alias. Empty/missing indices contribute nothing."""
    out: dict[str, set[tuple[str, str, str, str]]] = {}
    for name in index_names:
        tuples = enumerate_content_commits(es, name)
        if tuples:
            out[name] = tuples
    return out


def gather_intended_index_by_commit(
    es: Elasticsearch,
) -> dict[tuple[str, str, str, str], set[str]]:
    """For every (host, org, repo, commit) with a ref marker, the set of physical content index
    names its markers intend -- reconstructed from each marker's index_level/index_suffix via
    files_index/lines_index (both prefixes, since a commit's content spans a files and a lines
    index). Multiple markers for one commit (e.g. two refs) union their intended locations, which
    is exactly right: content at any of them is legitimate, content anywhere else is stale.

    Reconstruction (not stored names) keeps this correct across an index-prefix version bump and
    matches wherever indexing actually wrote. Returns {} if the refs index doesn't exist."""
    out: dict[tuple[str, str, str, str], set[str]] = {}
    body = {"query": {"match_all": {}}}
    src_fields = ["git.host", "git.org", "git.repo", "git.commit", "index_level", "index_suffix"]
    try:
        for hit in scan(es, index=REFS_ALIAS, query=body, _source=src_fields, preserve_order=False):
            src = hit["_source"]
            g = src.get("git", {})
            host, org, repo, commit = g.get("host"), g.get("org"), g.get("repo"), g.get("commit")
            if not (host and org and repo and commit):
                continue
            level = src.get("index_level") or "repo"
            suffix = src.get("index_suffix") or None
            key = (host, org, repo, commit)
            intended = out.setdefault(key, set())
            intended.add(files_index(host, org, repo, commit, level, suffix))
            intended.add(lines_index(host, org, repo, commit, level, suffix))
    except NotFoundError:
        return {}
    return out


def gather_intended_incremental_index_by_ref(
    es: Elasticsearch,
) -> dict[tuple[str, str, str, str], set[str]]:
    """For every (host, org, repo, ref) with an incremental join doc, the set of physical content
    index names that join doc intends -- reconstructed from its index_level/index_suffix via
    files_index/lines_index with commit=None (incremental content is ref-addressed, not
    commit-addressed).

    Feeds the incremental stale-location sweep (Class D-I): content for a branch sitting in an
    index NOT in this set is stale from a crashed migration and should be reclaimed.  Filters to
    mode=="incremental" docs only, so snapshot markers (which always have git.commit) are
    not double-counted.  Returns {} if the refs index doesn't exist."""
    out: dict[tuple[str, str, str, str], set[str]] = {}
    body = {"query": {"term": {"mode": "incremental"}}}
    src_fields = ["git.host", "git.org", "git.repo", "git.ref", "index_level", "index_suffix"]
    try:
        for hit in scan(es, index=REFS_ALIAS, query=body, _source=src_fields, preserve_order=False):
            src = hit["_source"]
            g = src.get("git", {})
            host, org, repo, ref = g.get("host"), g.get("org"), g.get("repo"), g.get("ref")
            if not (host and org and repo and ref):
                continue
            level = src.get("index_level") or "repo"
            suffix = src.get("index_suffix") or None
            key = (host, org, repo, ref)
            intended = out.setdefault(key, set())
            intended.add(files_index(host, org, repo, None, level, suffix))
            intended.add(lines_index(host, org, repo, None, level, suffix))
    except NotFoundError:
        return {}
    return out


def gather_incremental_content_by_index(
    es: Elasticsearch, index_names: list[str],
) -> dict[str, set[tuple[str, str, str, str]]]:
    """Per physical index, the distinct (host, org, repo, ref) tuples with incremental content
    docs in it (docs that have git.ref and a null/absent git.commit).

    Feeds the incremental stale-location sweep (Class D-I) in planner.orphan_stale_incremental_content:
    to decide a doc is stale we must know WHICH physical index holds it AND which ref it belongs to,
    so this enumerates each backing index by name via a composite aggregation over git.ref.
    Empty/missing indices contribute nothing."""
    out: dict[str, set[tuple[str, str, str, str]]] = {}
    for name in index_names:
        tuples = _composite_incremental_ref_tuples(es, name)
        if tuples:
            out[name] = tuples
    return out


def _composite_incremental_ref_tuples(
    es: Elasticsearch, index: str,
) -> set[tuple[str, str, str, str]]:
    """Distinct (host, org, repo, ref) tuples from incremental content docs (git.ref present,
    git.commit absent) in `index`. Returns empty set if the index doesn't exist."""
    out: set[tuple[str, str, str, str]] = set()
    after: dict | None = None
    while True:
        composite: dict = {
            "size": _COMPOSITE_PAGE_SIZE,
            "sources": [
                {"host": {"terms": {"field": "git.host"}}},
                {"org": {"terms": {"field": "git.org"}}},
                {"repo": {"terms": {"field": "git.repo"}}},
                {"ref": {"terms": {"field": "git.ref"}}},
            ],
        }
        if after is not None:
            composite["after"] = after
        # Filter to docs that have git.ref but no git.commit (incremental content).
        query = {"bool": {"filter": [{"exists": {"field": "git.ref"}}],
                          "must_not": [{"exists": {"field": "git.commit"}}]}}
        try:
            resp = es.search(index=index, size=0, query=query,
                             aggs={"tuples": {"composite": composite}})
        except NotFoundError:
            return out
        agg = resp["aggregations"]["tuples"]
        buckets = agg["buckets"]
        if not buckets:
            return out
        for b in buckets:
            out.add((b["key"]["host"], b["key"]["org"], b["key"]["repo"], b["key"]["ref"]))
        after = agg.get("after_key")
        if after is None:
            return out


_FULL_SHA_LEN = 40
_MIN_PREFIX_LEN = 7


def content_indices_for_commit(
    es: Elasticsearch, host: str, org: str, repo: str, sha: str,
) -> list[str]:
    """The physical content index names (files and/or lines) that actually hold docs for one
    commit, discovered via the read aliases. Used by the single-commit content-only prune path,
    which has no marker to reconstruct routing from -- so it discovers the real location instead of
    assuming the repo-level name (which would miss a commit-level or suffixed index). Returns [] if
    nothing matches or the aliases don't exist."""
    names: set[str] = set()
    query = {"bool": {"filter": [
        {"term": {"git.host": host}},
        {"term": {"git.org": org}},
        {"term": {"git.repo": repo}},
        {"term": {"git.commit": sha}},
    ]}}
    for alias in (FILES_ALIAS, LINES_ALIAS):
        try:
            resp = es.search(index=alias, size=0, query=query,
                             aggs={"idx": {"terms": {"field": "_index", "size": 1000}}})
        except NotFoundError:
            continue
        for b in resp["aggregations"]["idx"]["buckets"]:
            names.add(b["key"])
    return sorted(names)


def _enumerate_content_field(
    es: Elasticsearch, host: str, org: str, repo: str, field: str,
) -> set[str]:
    """Every distinct value of `field` (git.commit or git.ref) in this repo's content,
    restricted to docs where that field IS NOT NULL, via paginated composite aggregation."""
    filters = [
        {"term": {"git.host": host}},
        {"term": {"git.org": org}},
        {"term": {"git.repo": repo}},
        {"exists": {"field": field}},
    ]
    out: set[str] = set()
    for index in (FILES_ALIAS, LINES_ALIAS):
        after: dict | None = None
        while True:
            composite: dict = {
                "size": _COMPOSITE_PAGE_SIZE,
                "sources": [{"val": {"terms": {"field": field}}}],
            }
            if after is not None:
                composite["after"] = after
            try:
                resp = es.search(
                    index=index, size=0,
                    query={"bool": {"filter": filters}},
                    aggs={"keys": {"composite": composite}},
                )
            except NotFoundError:
                break
            agg = resp["aggregations"]["keys"]
            buckets = agg["buckets"]
            if not buckets:
                break
            for b in buckets:
                out.add(b["key"]["val"])
            after = agg.get("after_key")
            if after is None:
                break
    return out


def check_join_uniqueness(es: Elasticsearch, host: str, org: str, repo: str) -> list[str]:
    """Join-uniqueness gate (INV-011 backstop): verifies every content key maps to a correct
    refs join doc. Split by content shape (no `mode` on content docs):

    - Snapshot (git.commit IS NOT NULL): each commit must resolve to ≥1 complete refs doc
      (presence check -- multi-ref-per-commit is legal; the snapshot FORK arm no longer joins
      so the uniqueness requirement there is already removed).
    - Incremental (git.ref IS NOT NULL): each ref must resolve to EXACTLY ONE refs doc with
      `mode == "incremental"` -- the anti-fan-out invariant for the surviving join.

    Returns the sorted list of offending keys (commits/refs that fail their respective check);
    an empty list means the invariant holds."""
    offending: list[str] = []

    # --- snapshot: each commit must have ≥1 complete refs doc ---
    commits = _enumerate_content_field(es, host, org, repo, "git.commit")
    if commits:
        try:
            resp = es.search(
                index=REFS_ALIAS, size=0,
                query={"bool": {"filter": [
                    {"term": {"git.host": host}},
                    {"term": {"git.org": org}},
                    {"term": {"git.repo": repo}},
                    {"terms": {"git.commit": sorted(commits)}},
                    {"term": {"status": "complete"}},
                ]}},
                aggs={"commits": {"terms": {"field": "git.commit", "size": len(commits)}}},
            )
            found_commits = {b["key"] for b in resp["aggregations"]["commits"]["buckets"]}
        except NotFoundError:
            found_commits = set()
        offending.extend(sorted(commits - found_commits))

    # --- incremental: each ref must have EXACTLY ONE incremental join doc ---
    refs = _enumerate_content_field(es, host, org, repo, "git.ref")
    if refs:
        try:
            resp = es.search(
                index=REFS_ALIAS, size=0,
                query={"bool": {"filter": [
                    {"term": {"git.host": host}},
                    {"term": {"git.org": org}},
                    {"term": {"git.repo": repo}},
                    {"terms": {"git.ref": sorted(refs)}},
                    {"term": {"mode": "incremental"}},
                ]}},
                aggs={"refs": {"terms": {"field": "git.ref", "size": len(refs)}}},
            )
            ref_counts = {b["key"]: b["doc_count"] for b in resp["aggregations"]["refs"]["buckets"]}
        except NotFoundError:
            ref_counts = {}
        offending.extend(sorted(ref for ref in refs if ref_counts.get(ref, 0) != 1))

    return sorted(offending)


# Legacy alias: used by tests and any external callers referencing the old name.
# Prefer check_join_uniqueness for new code.
check_ref_key_uniqueness = check_join_uniqueness


def resolve_content_commit(
    es: Elasticsearch, host: str, org: str, repo: str, prefix: str,
) -> set[str]:
    """Resolve a commit hash (full 40-char SHA or ≥7-char prefix) to the set of distinct
    ``git.commit`` values that actually have content docs in the repo's files or lines index.

    Returns an empty set if nothing matches or the index does not exist. The caller is
    responsible for rejecting an ambiguous result (>1 element) and the no-content case (0
    elements).

    Uses a composite aggregation over the read aliases (host/org/repo-scoped, so the fan-out only
    touches this repo's backing indices) rather than a single per-repo physical name. This is what
    makes it index.level/suffix-aware: a commit's content may live in a commit-level or suffixed
    index whose exact name isn't derivable here, but it is always under the alias. For a full
    40-char SHA a ``term`` filter is used instead of ``prefix``; otherwise a ``prefix`` query is
    added alongside the host/org/repo filter."""
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
    for idx in (LINES_ALIAS, FILES_ALIAS):
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
