# sourcerer/queries.py
# Read-only Elasticsearch queries over the sourcerer indices, shared by `index` (idempotency
# guards, dry-run preview) and `prune` (orphan sweep, retention). The detection/planning logic
# itself is pure and lives in planner.py; these are the thin, mockable wrappers that gather
# its inputs from a real cluster. Nothing here ever writes -- deletion lives in
# commands/prune/execute.py, and indexing writes live in commands/index/documents.py.

# Standard packages
import datetime
import os
from concurrent.futures import ThreadPoolExecutor

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError
from elasticsearch.helpers import scan

# App packages
from .indices import FILES_ALIAS, FILES_INDEX_PREFIX, LINES_ALIAS, LINES_INDEX_PREFIX, REFS_ALIAS, files_index, lines_index
from .planner import Marker, parse_index_name

_COMPOSITE_PAGE_SIZE = 1000


def _scan_concurrency() -> int:
    """Concurrent per-index composite-aggregation requests during orphan-sweep planning
    (gather_content_and_incremental_by_index). Env-overridable like commands/index/runtime's
    _tuning() knobs, kept as a standalone function since queries.py has no dependency on the
    index command's runtime module. The ES client is thread-safe, and every per-index request
    is independent, so this only bounds how many are in flight at once."""
    return int(os.environ.get("SOURCERER_PRUNE_SCAN_CONCURRENCY", "8"))


def _scope_filters(host: str | None, org: str | None, repo: str | None) -> list[dict]:
    """Term filters for an optional (host, org, repo) scope, used by a scoped prune
    (--host/--org/--repo) to narrow a refs-index query. A field left as None contributes no
    filter, so passing all-None reproduces the previous, unscoped query exactly."""
    filters = []
    if host is not None:
        filters.append({"term": {"git.host": host}})
    if org is not None:
        filters.append({"term": {"git.org": org}})
    if repo is not None:
        filters.append({"term": {"git.repo": repo}})
    return filters


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
    refs index) is never touched.

    Doc counts for every candidate are fetched in ONE es.indices.stats call rather than one
    es.count per index -- the N->1 batching this function exists to do. Falls back to the
    original per-index es.count loop if the batched call 404s (a race where an index vanished
    between `index_names` being listed and this call -- e.g. a concurrent prune run's own
    orphan sweep -- since indices.stats, unlike count/search, has no per-request
    ignore_unavailable to skip just the missing name)."""
    candidates = [name for name in index_names if parse_index_name(name) is not None]
    if not candidates:
        return []
    candidate_set = set(candidates)
    # Use wildcard patterns instead of a comma-joined list of all candidates to avoid
    # exceeding Elasticsearch's 4096-byte HTTP line limit when there are many indices.
    wildcard = f"{FILES_INDEX_PREFIX}*,{LINES_INDEX_PREFIX}*"
    try:
        stats = es.indices.stats(index=wildcard, metric="docs")
    except NotFoundError:
        return _empty_content_indices_fallback(es, candidates)
    indices_stats = {k: v for k, v in (stats.get("indices") or {}).items() if k in candidate_set}
    out: list[str] = []
    for name in candidates:
        entry = indices_stats.get(name)
        if entry is None:
            continue
        count = entry.get("total", {}).get("docs", {}).get("count", 0)
        if int(count) == 0:
            out.append(name)
    return out


def _empty_content_indices_fallback(es: Elasticsearch, candidates: list[str]) -> list[str]:
    """Per-index es.count fallback for empty_content_indices, used only when the batched
    es.indices.stats call 404s. Reproduces the pre-batching behaviour exactly: an index that
    vanished between listing and counting is simply skipped."""
    out: list[str] = []
    for name in candidates:
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


def enumerate_snapshot_ref_commit_tuples(
    es: Elasticsearch, host: str | None = None, org: str | None = None, repo: str | None = None,
) -> set[tuple[str, str, str, str]]:
    """Snapshot-only (mode != "delta") (git.host, git.org, git.repo, git.commit) tuples from the
    refs alias, also excluding stale markers (status == "stale").

    This is the correct commit set for the orphan-sweep Class-B/C comparison. Delta join docs
    carry git.commit = live HEAD but their content is ref-addressed (no git.commit on content docs),
    so including them in the commit comparison would mark every delta HEAD as an orphan marker and
    delete the join doc. Using must_not mode=delta (exclude-delta) rather than must mode=snapshot
    (require-snapshot) preserves legacy markers that predate the mode field and would otherwise be
    dropped, causing their content to become false Class-B orphans. Stale markers are owned by
    execute_stale_marker_deletions and must not also be processed by the orphan sweep.

    `host`/`org`/`repo` narrow a scoped prune (--host/--org/--repo) to that identity; left at the
    default None, every repo is scanned exactly as before.

    Returns an empty set if the refs index doesn't exist yet."""
    bool_query: dict = {"must_not": [{"term": {"mode": "delta"}}, {"term": {"status": "stale"}}]}
    scope = _scope_filters(host, org, repo)
    if scope:
        bool_query["filter"] = scope
    return _composite_host_org_repo_commit_tuples(es, REFS_ALIAS, query={"bool": bool_query})


def enumerate_ref_repo_identities(
    es: Elasticsearch, host: str | None = None, org: str | None = None, repo: str | None = None,
) -> set[tuple[str, str, str]]:
    """Every distinct (git.host, git.org, git.repo) with any ref doc -- snapshot OR delta --
    via a paginated composite aggregation over git.host/git.org/git.repo only (no commit source).

    Feeds the Class-A orphan_indices identity protection so that a delta-only repo (one with a
    delta join doc but no snapshot markers) still contributes (host, org, repo) identity and its
    content index is not falsely flagged orphan:index and deleted. The commit source is intentionally
    absent: a delta first-index join doc has git.commit=None (invisible to a git.commit terms agg)
    but still has host/org/repo and must contribute identity. No mode filter is applied so legacy,
    snapshot, and delta docs all contribute equally.

    `host`/`org`/`repo` narrow a scoped prune (--host/--org/--repo) to that identity; left at the
    default None, every repo is scanned exactly as before (and no `query` clause is added at all,
    matching the historical unfiltered request shape).

    Returns an empty set if the refs index doesn't exist yet."""
    out: set[tuple[str, str, str]] = set()
    scope = _scope_filters(host, org, repo)
    query = {"bool": {"filter": scope}} if scope else None
    after: dict | None = None
    while True:
        composite: dict = {
            "size": _COMPOSITE_PAGE_SIZE,
            "sources": [
                {"host": {"terms": {"field": "git.host"}}},
                {"org": {"terms": {"field": "git.org"}}},
                {"repo": {"terms": {"field": "git.repo"}}},
            ],
        }
        if after is not None:
            composite["after"] = after
        search_kwargs: dict = {"index": REFS_ALIAS, "size": 0, "aggs": {"ids": {"composite": composite}}}
        if query is not None:
            search_kwargs["query"] = query
        try:
            resp = es.search(**search_kwargs)
        except NotFoundError:
            return out
        agg = resp["aggregations"]["ids"]
        buckets = agg["buckets"]
        if not buckets:
            return out
        for b in buckets:
            out.add((b["key"]["host"], b["key"]["org"], b["key"]["repo"]))
        after = agg.get("after_key")
        if after is None:
            return out


def enumerate_content_commits(es: Elasticsearch, index: str) -> set[tuple[str, str, str, str]]:
    """Every distinct (git.host, git.org, git.repo, git.commit) tuple with at least one doc in
    `index` (a single files or lines physical index). Returns an empty set if `index` doesn't
    exist."""
    return _composite_host_org_repo_commit_tuples(es, index)


def _composite_host_org_repo_commit_tuples(
    es: Elasticsearch, index: str, query: dict | None = None,
) -> set[tuple[str, str, str, str]]:
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
        search_kwargs: dict = {"index": index, "size": 0, "aggs": {"tuples": {"composite": composite}}}
        if query is not None:
            search_kwargs["query"] = query
        try:
            resp = es.search(**search_kwargs)
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


def gather_content_and_incremental_by_index(
    es: Elasticsearch, index_names: list[str], progress_cb=None,
) -> tuple[dict[str, set[tuple[str, str, str, str]]], dict[str, set[tuple[str, str, str, str, str]]]]:
    """Per physical index, both the snapshot (host, org, repo, commit) tuples AND the incremental
    (host, org, repo, ref_type, ref_pattern) tuples with content docs in it -- gathered with ONE
    composite aggregation per index (via `_composite_content_and_incremental_tuples`) instead of
    two, using `missing_bucket: true` on the fields only one of the two disjoint content-doc
    shapes carries (a snapshot doc has git.commit and no git.ref_pattern/ref_type; an incremental
    doc has the reverse -- see commands/index/documents.py's build_file_doc vs.
    build_incremental_file_doc).

    Feeds Class-D and Class-D-I stale-location detection (planner.orphan_stale_content /
    orphan_stale_incremental_content). The per-index requests run concurrently -- the ES client is
    thread-safe and each index's aggregation is independent of every other's -- bounded by
    `_scan_concurrency()` (env `SOURCERER_PRUNE_SCAN_CONCURRENCY`, default 8), mirroring
    commands/index/runtime._tuning()'s env-overridable worker counts.

    `progress_cb`, if given, is called with no arguments once per index as its result arrives
    (regardless of completion order), so a caller can drive a determinate progress display
    without waiting for the whole gather to finish.

    Returns (content_by_index, incremental_content_by_index); an index contributing only one
    content-doc shape appears in only the corresponding dict. Empty/missing indices contribute
    nothing to either."""
    content_by_index: dict[str, set[tuple[str, str, str, str]]] = {}
    incremental_by_index: dict[str, set[tuple[str, str, str, str, str]]] = {}
    if not index_names:
        return content_by_index, incremental_by_index

    def _gather_one(name: str):
        return name, _composite_content_and_incremental_tuples(es, name)

    workers = max(1, min(_scan_concurrency(), len(index_names)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, (snapshot_tuples, incremental_tuples) in pool.map(_gather_one, index_names):
            if snapshot_tuples:
                content_by_index[name] = snapshot_tuples
            if incremental_tuples:
                incremental_by_index[name] = incremental_tuples
            if progress_cb is not None:
                progress_cb()
    return content_by_index, incremental_by_index


def _composite_content_and_incremental_tuples(
    es: Elasticsearch, index: str,
) -> tuple[set[tuple[str, str, str, str]], set[tuple[str, str, str, str, str]]]:
    """One composite aggregation over `index` covering both content-doc shapes at once, via
    `missing_bucket: true` on commit/ref_type/ref_pattern: a snapshot doc (commit set, ref_pattern
    absent) and an incremental doc (ref_pattern/ref_type set, commit absent) each land in their
    own bucket without a query filter to pick one shape or the other -- ES composite buckets on
    the *combination* of source values including the missing ones. Returns (snapshot tuples,
    incremental tuples); both empty if `index` doesn't exist."""
    snapshot: set[tuple[str, str, str, str]] = set()
    incremental: set[tuple[str, str, str, str, str]] = set()
    after: dict | None = None
    while True:
        composite: dict = {
            "size": _COMPOSITE_PAGE_SIZE,
            "sources": [
                {"host": {"terms": {"field": "git.host"}}},
                {"org": {"terms": {"field": "git.org"}}},
                {"repo": {"terms": {"field": "git.repo"}}},
                {"commit": {"terms": {"field": "git.commit", "missing_bucket": True}}},
                {"ref_type": {"terms": {"field": "git.ref_type", "missing_bucket": True}}},
                {"ref_pattern": {"terms": {"field": "git.ref_pattern", "missing_bucket": True}}},
            ],
        }
        if after is not None:
            composite["after"] = after
        try:
            resp = es.search(index=index, size=0, aggs={"tuples": {"composite": composite}})
        except NotFoundError:
            return snapshot, incremental
        agg = resp["aggregations"]["tuples"]
        buckets = agg["buckets"]
        if not buckets:
            return snapshot, incremental
        for b in buckets:
            key = b["key"]
            host, org, repo = key["host"], key["org"], key["repo"]
            commit, ref_type, ref_pattern = key.get("commit"), key.get("ref_type"), key.get("ref_pattern")
            if commit is not None:
                snapshot.add((host, org, repo, commit))
            elif ref_type is not None and ref_pattern is not None:
                incremental.add((host, org, repo, ref_type, ref_pattern))
        after = agg.get("after_key")
        if after is None:
            return snapshot, incremental


def gather_intended_locations(
    es: Elasticsearch, host: str | None = None, org: str | None = None, repo: str | None = None,
) -> tuple[dict[tuple[str, str, str, str], set[str]], dict[tuple[str, str, str, str, str], set[str]]]:
    """One scan of the refs alias that gathers BOTH intended-location maps the stale-location
    sweep needs, replacing two full scans (gather_intended_index_by_commit and
    gather_intended_incremental_index_by_ref) with one:

    - For every (host, org, repo, commit) with a ref marker (snapshot OR delta -- a delta join
      doc carries git.commit = live HEAD too), the set of physical content index names its
      markers intend -- reconstructed via files_index/lines_index from index_level/index_suffix.
    - For every (host, org, repo, ref_type, ref_pattern) with a `mode == "delta"` join doc, the
      set of physical content index names it intends with commit=None (incremental content is
      ref-addressed). The mode=="delta" check matters because snapshot markers ALSO carry
      git.ref_pattern (defaulted to their ref) -- without it every snapshot marker would spuriously
      contribute an incremental entry.

    Multiple markers for one key union their intended locations: content at any of them is
    legitimate, content anywhere else is stale.

    `host`/`org`/`repo` narrow a scoped prune (--host/--org/--repo) to that identity.

    Returns (intended_index_by_commit, intended_incremental_index_by_ref); both empty if the refs
    index doesn't exist."""
    intended_by_commit: dict[tuple[str, str, str, str], set[str]] = {}
    intended_incremental_by_ref: dict[tuple[str, str, str, str, str], set[str]] = {}
    scope = _scope_filters(host, org, repo)
    query = {"query": {"bool": {"filter": scope}}} if scope else {"query": {"match_all": {}}}
    src_fields = ["git.host", "git.org", "git.repo", "git.commit", "git.ref_type", "git.ref_pattern",
                  "mode", "index_level", "index_suffix"]
    try:
        for hit in scan(es, index=REFS_ALIAS, query=query, _source=src_fields, preserve_order=False):
            src = hit["_source"]
            g = src.get("git", {})
            h, o, r = g.get("host"), g.get("org"), g.get("repo")
            if not (h and o and r):
                continue
            level = src.get("index_level") or "repo"
            suffix = src.get("index_suffix") or None
            commit = g.get("commit")
            if commit:
                key = (h, o, r, commit)
                intended = intended_by_commit.setdefault(key, set())
                intended.add(files_index(h, o, r, commit, level, suffix))
                intended.add(lines_index(h, o, r, commit, level, suffix))
            if src.get("mode") == "delta":
                ref_type = g.get("ref_type")
                ref_pattern = g.get("ref_pattern")
                if ref_type and ref_pattern:
                    key2 = (h, o, r, ref_type, ref_pattern)
                    intended2 = intended_incremental_by_ref.setdefault(key2, set())
                    intended2.add(files_index(h, o, r, None, level, suffix))
                    intended2.add(lines_index(h, o, r, None, level, suffix))
    except NotFoundError:
        return intended_by_commit, intended_incremental_by_ref
    return intended_by_commit, intended_incremental_by_ref


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


def _enumerate_incremental_content_ref_pairs(
    es: Elasticsearch, host: str, org: str, repo: str,
) -> set[tuple[str, str]]:
    """Every distinct (git.ref_type, git.ref_pattern) pair in this repo's incremental content docs
    (docs that have git.ref_pattern and no git.commit), via paginated composite aggregation."""
    out: set[tuple[str, str]] = set()
    for index in (FILES_ALIAS, LINES_ALIAS):
        after: dict | None = None
        while True:
            composite: dict = {
                "size": _COMPOSITE_PAGE_SIZE,
                "sources": [
                    {"ref_type": {"terms": {"field": "git.ref_type"}}},
                    {"ref_pattern": {"terms": {"field": "git.ref_pattern"}}},
                ],
            }
            if after is not None:
                composite["after"] = after
            query = {"bool": {
                "filter": [
                    {"term": {"git.host": host}},
                    {"term": {"git.org": org}},
                    {"term": {"git.repo": repo}},
                    {"exists": {"field": "git.ref_pattern"}},
                ],
                "must_not": [{"exists": {"field": "git.commit"}}],
            }}
            try:
                resp = es.search(
                    index=index, size=0,
                    query=query,
                    aggs={"pairs": {"composite": composite}},
                )
            except NotFoundError:
                break
            agg = resp["aggregations"]["pairs"]
            buckets = agg["buckets"]
            if not buckets:
                break
            for b in buckets:
                out.add((b["key"]["ref_type"], b["key"]["ref_pattern"]))
            after = agg.get("after_key")
            if after is None:
                break
    return out


def check_join_uniqueness(es: Elasticsearch, host: str, org: str, repo: str) -> list[str]:
    """Join-uniqueness gate: verifies every content key maps to a correct
    refs join doc. Split by content shape (no `mode` on content docs):

    - Snapshot (git.commit IS NOT NULL): each commit must resolve to ≥1 complete refs doc
      (presence check -- multi-ref-per-commit is legal; the snapshot FORK arm no longer joins
      so the uniqueness requirement there is already removed).
    - Incremental (git.ref IS NOT NULL): each ref must resolve to EXACTLY ONE refs doc with
      `mode == "delta"` -- the anti-fan-out invariant for the surviving join.

    Returns the sorted list of offending keys (commits/refs that fail their respective check);
    an empty list means the invariant holds."""
    offending: list[str] = []

    # --- snapshot: each commit must have ≥1 complete (or in-progress) refs doc ---
    # Accept "indexing" status as non-offending: an active run holding this commit is not stale.
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
                    {"terms": {"status": ["complete", "indexing"]}},
                ]}},
                aggs={"commits": {"terms": {"field": "git.commit", "size": len(commits)}}},
            )
            found_commits = {b["key"] for b in resp["aggregations"]["commits"]["buckets"]}
        except NotFoundError:
            found_commits = set()
        offending.extend(sorted(commits - found_commits))

    # --- incremental: each (ref_type, ref_pattern) pair must have EXACTLY ONE incremental join doc ---
    # Content docs carry git.ref_pattern (= the stream identity) and git.ref_type; a same-named
    # branch and tag are distinct (ref_type, ref_pattern) pairs, each allowed exactly one join doc.
    # On the refs side the join key is git.ref_pattern (= the stream identity), NOT git.ref
    # (which holds the concrete resolved ref for delta-tag streams). Filter and aggregate by
    # (git.ref_type, git.ref_pattern) so the pair-counts align with the content-side key.
    ref_pairs = _enumerate_incremental_content_ref_pairs(es, host, org, repo)
    if ref_pairs:
        try:
            resp = es.search(
                index=REFS_ALIAS, size=0,
                query={"bool": {"filter": [
                    {"term": {"git.host": host}},
                    {"term": {"git.org": org}},
                    {"term": {"git.repo": repo}},
                    {"terms": {"git.ref_pattern": sorted({r for _, r in ref_pairs})}},
                    {"term": {"mode": "delta"}},
                ]}},
                aggs={"ref_pairs": {"composite": {"size": 1000, "sources": [
                    {"ref_type": {"terms": {"field": "git.ref_type"}}},
                    {"ref_pattern": {"terms": {"field": "git.ref_pattern"}}},
                ]}}},
            )
            pair_counts = {
                (b["key"]["ref_type"], b["key"]["ref_pattern"]): b["doc_count"]
                for b in resp["aggregations"]["ref_pairs"]["buckets"]
            }
        except NotFoundError:
            pair_counts = {}
        offending.extend(
            sorted(f"{rt}/{ref_pattern}" for rt, ref_pattern in ref_pairs if pair_counts.get((rt, ref_pattern), 0) != 1)
        )

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
