# sourcerer/commands/index/markers.py
# Refs-index idempotency: content-addressing a ref's indexed state, the guards that decide
# whether a ref needs (re)indexing, and writing the completion marker. Reads use the sourcerer
# aliases; writes use sourcerer-v2-refs and the physical per-repo content indices;
# broader read-only queries across the whole cluster live in sourcerer/queries.py.

# Standard packages
import datetime

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError

# App packages
from ...indices import FILES_ALIAS, REFS_ALIAS, REFS_INDEX
from ...utils import make_doc_id
from .git import resolve_remote


def build_ref_id(host: str, org: str, repo: str, ref_type: str, ref: str, commit_sha: str) -> str:
    """Content address of one indexed ref state.

    host is folded in so the same org/repo on two different hosting providers never collide.
    (ref_type, ref) identifies the ref -- a branch and a same-named tag ("release",
    "stable") are distinct, and multiple refs can resolve to one commit, so keying on commit
    alone would collapse them and clobber one on the next run. Folding commit in makes a
    moving branch append a new marker per commit (the append-only history that count/age
    pruning needs), while an immutable tag re-hashes to the same id and stays idempotent."""
    return make_doc_id(host, org, repo, ref_type, ref, commit_sha)


def markers_status_by_id(es: Elasticsearch, ref_ids: list[str]) -> dict[str, dict]:
    """Fetch the status, commit, and indexing_started_at for a batch of ref marker ids.

    Returns a mapping of {ref_id: {"status": ..., "commit": ..., "indexing_started_at": ...}}
    for every id that exists in the refs alias. Missing ids (never indexed) are absent from the
    result. Returns {} when the refs index does not exist yet.

    This is the batched equivalent of the per-ref `should_index` ids-lookup: instead of N
    serial searches (one per ref), callers compute all ref_ids up front and fetch them in one
    shot before the per-ref loop.
    """
    if not ref_ids:
        return {}
    try:
        resp = es.search(
            index=REFS_ALIAS,
            size=len(ref_ids),
            query={"ids": {"values": ref_ids}},
            source_includes=["status", "git.commit", "indexing_started_at"],
        )
    except NotFoundError:
        return {}
    out: dict[str, dict] = {}
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        out[hit["_id"]] = {
            "status": src.get("status"),
            "commit": src.get("git", {}).get("commit"),
            "indexing_started_at": src.get("indexing_started_at"),
        }
    return out


def _parse_marker_started(value: object) -> datetime.datetime | None:
    """Parse an `indexing_started_at` field value to a tz-aware UTC datetime, or None.

    Handles:
    - None / missing field -> None
    - Malformed / non-ISO strings -> None (safe: treated as unknown/stuck)
    - Naive ISO strings -> coerced to UTC (legacy / hand-written markers)
    - Tz-aware ISO strings -> returned as-is
    """
    if value is None:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def commits_with_content(
    es: Elasticsearch, host: str, org: str, repo: str, commit_shas: set[str],
) -> set[str]:
    """Return the subset of `commit_shas` that have at least one content doc in the files alias.

    This is the batched equivalent of `content_present`: instead of one `es.count` per
    (complete-marker) commit, a single terms aggregation resolves all of them at once.

    Used to detect the GC'd-out-from-under case: a ref whose marker is `status:complete` but
    whose content was deleted needs re-indexing even though its marker exists.

    Returns an empty set when `commit_shas` is empty or the files index does not exist yet.
    """
    if not commit_shas:
        return set()
    try:
        resp = es.search(
            index=FILES_ALIAS,
            size=0,
            query={
                "bool": {
                    "filter": [
                        {"term": {"git.host": host}},
                        {"term": {"git.org": org}},
                        {"term": {"git.repo": repo}},
                        {"terms": {"git.commit": sorted(commit_shas)}},
                    ]
                }
            },
            aggs={"present": {"terms": {"field": "git.commit", "size": len(commit_shas)}}},
        )
    except NotFoundError:
        return set()
    return {b["key"] for b in resp["aggregations"]["present"]["buckets"]}


def _needs_index(
    ref_id: str,
    remote_sha: str,
    status_map: dict[str, dict],
    content_commits: set[str],
    indexing_cutoff: datetime.datetime | None = None,
) -> bool:
    """Pure (no ES) per-ref skip decision -- the decision port of `should_index`.

    Returns True if this ref needs (re)indexing.

    - Missing from status_map: no marker -> must index.
    - status == 'indexing' AND indexing_started_at >= indexing_cutoff: another run is actively
      indexing this ref -> skip (return False). If indexing_cutoff is None the active-run check
      is bypassed (back-compat default).
    - status != 'complete' (stuck/abandoned marker, or no cutoff given): must index.
    - commit in content_commits: complete marker AND content still present -> skip.
    - complete marker but content absent (GC'd): must index.

    `status_map` is the result of `markers_status_by_id` and `content_commits` is the result of
    `commits_with_content` over the set of complete-marker commits. Both are computed once per
    repo group before the per-ref loop.
    """
    marker = status_map.get(ref_id)
    if marker is None:
        return True
    status = marker.get("status")
    if status != "complete":
        if (
            status == "indexing"
            and indexing_cutoff is not None
        ):
            started = _parse_marker_started(marker.get("indexing_started_at"))
            if started is not None and started >= indexing_cutoff:
                return False  # another run is actively indexing this ref
        return True
    commit = marker.get("commit") or remote_sha
    return commit not in content_commits


def count_commit_docs(es: Elasticsearch, index: str, host: str, org: str, repo: str, commit_sha: str) -> int:
    """Count docs in `index` for one commit. Content is keyed by (host, org, repo, commit, path),
    so the commit alone identifies a snapshot regardless of which ref reached it.

    Returns 0 when the index does not yet exist (indices are created on demand by the first
    write, so a brand-new repo has no index until its first successful ingest)."""
    query = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"term": {"git.commit": commit_sha}},
            ]
        }
    }
    try:
        return int(es.count(index=index, query=query)["count"])
    except NotFoundError:
        return 0


def content_present(es: Elasticsearch, host: str, org: str, repo: str, commit_sha: str) -> bool:
    """True if ANY content doc exists for this commit. A cheap presence probe -- NOT proof of a
    complete snapshot, since an interrupted run (Ctrl-C) leaves a partial set of docs behind with
    no marker (see commit_fully_indexed). Used only to detect content GC'd out from under a
    surviving complete marker."""
    return count_commit_docs(es, FILES_ALIAS, host, org, repo, commit_sha) > 0


def commit_fully_indexed(es: Elasticsearch, host: str, org: str, repo: str, commit_sha: str) -> bool:
    """True if a `status: complete` ref marker references this commit -- i.e. some ref finished
    indexing this exact snapshot end to end.

    The completeness signal is the marker, not the mere presence of content docs. write_ref_marker
    is written only after index_repo returns (line-by-line ingest complete), so a commit whose
    content came from an aborted run -- partial docs, no marker -- is NOT fully indexed here and
    gets re-indexed (safe: doc ids are idempotent, so re-ingest just fills the gaps and overwrites
    in place). This is the guard that stops a Ctrl-C'd ref from being wrongly recorded as
    "already indexed" on the next run."""
    query = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"term": {"git.commit": commit_sha}},
                {"term": {"status": "complete"}},
            ]
        }
    }
    try:
        return int(es.count(index=REFS_ALIAS, query=query)["count"]) > 0
    except NotFoundError:
        return False


def fully_indexed_counts(
    es: Elasticsearch, host: str, org: str, repo: str, commit_sha: str,
) -> tuple[int, int] | None:
    """The (files_count, lines_count) recorded by a `status: complete` ref marker for this
    commit, or None if no such marker exists. The count-returning sibling of commit_fully_indexed.

    Content is keyed by commit (host, org, repo, commit, path), so every complete marker for this
    exact commit describes the same snapshot and carries identical counts -- any one is
    authoritative, so we take the first hit. Crucially the counts come from the marker (written by
    write_ref_marker, tallied from bulk results at ingest), NOT from an es.count over the content
    indices: during a bulk run refresh is disabled on the content indices
    (runtime.bulk_indexing_settings), so a sibling that ingested this commit earlier in the same
    run isn't search-visible yet and an es.count would spuriously return 0 -- but its marker, on
    the refresh-enabled refs index, holds the real counts. Returns None when no complete marker
    exists (including when the refs index does not exist yet)."""
    query = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"term": {"git.commit": commit_sha}},
                {"term": {"status": "complete"}},
            ]
        }
    }
    try:
        resp = es.search(
            index=REFS_ALIAS, size=1, query=query,
            source_includes=["files_count", "lines_count"],
        )
    except NotFoundError:
        return None
    hits = resp["hits"]["hits"]
    if not hits:
        return None
    src = hits[0]["_source"]
    return int(src.get("files_count", 0)), int(src.get("lines_count", 0))


def commit_prefix_indexed(es: Elasticsearch, host: str, org: str, repo: str, sha_prefix: str) -> str | None:
    """The full commit SHA of a `status: complete` marker in this repo whose commit starts with
    `sha_prefix`, or None if no marker matches. There is no remote way to resolve a SHA/prefix
    (unlike ls-remote for branch/tag), so a pinned commit's cheap pre-clone skip instead checks
    this index directly: if some earlier run already fully indexed a commit with this prefix,
    a `type: commit` (or `-c`) re-run can skip the clone entirely, the same way branch/tag do.
    `git.commit` is a lowercase-normalized keyword, so `prefix` matches case-insensitively as
    long as `sha_prefix` is already lowercase (config parsing normalizes it; callers of the
    `-c` CLI flag should lowercase it too)."""
    query = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"prefix": {"git.commit": sha_prefix}},
                {"term": {"status": "complete"}},
            ]
        }
    }
    try:
        resp = es.search(index=REFS_ALIAS, size=1, query=query)
    except NotFoundError:
        return None
    hits = resp["hits"]["hits"]
    return hits[0]["_source"]["git"]["commit"] if hits else None


def should_index(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref_type: str,
    ref: str,
    commit_sha: str,
    retry_window: datetime.timedelta | None = None,
) -> bool:
    """
    True if this exact (ref_type, ref, commit) needs (re)indexing. The id now encodes the
    commit, so a moved branch simply misses (NotFound -> index the new commit, old marker
    retained). A present+complete marker is guaranteed to be this commit; the only remaining
    reason to re-index is content GC'd out from under a surviving marker.

    With `retry_window` set, an `indexing` marker whose `indexing_started_at` is within the
    window is treated as an active concurrent run and returns False (skip). Without it (default),
    any non-complete marker triggers re-indexing (previous behavior).
    """
    ref_id = build_ref_id(host, org, repo, ref_type, ref, commit_sha)
    try:
        resp = es.search(index=REFS_ALIAS, size=1, query={"ids": {"values": [ref_id]}})
    except NotFoundError:
        return True
    hits = resp["hits"]["hits"]
    if not hits:
        return True
    marker = hits[0]["_source"]
    status = marker.get("status")
    if status != "complete":
        if status == "indexing" and retry_window is not None:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - retry_window
            started = _parse_marker_started(marker.get("indexing_started_at"))
            if started is not None and started >= cutoff:
                return False  # another run is actively indexing this ref
        return True
    return not content_present(es, host, org, repo, commit_sha)


def write_indexing_marker(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref_type: str,
    ref: str,
    commit_sha: str,
    commit_date_iso: str | None,
) -> None:
    """Write a status:'indexing' marker for a ref that is about to be ingested.

    Uses the same build_ref_id-keyed doc as write_ref_marker, so the terminal write_ref_marker
    call (status:'complete') will overwrite this marker in place once ingest completes.

    This marker allows the schedule gate to detect that another run is currently indexing this
    source's scope (host/org/repo/ref_type) and skip it, preventing redundant parallel work.
    If the run dies before calling write_ref_marker, the indexing marker stays behind; the gate
    retries the source after the retry window elapses (default 1h, configurable via
    --retry-window).
    """
    ref_id = build_ref_id(host, org, repo, ref_type, ref, commit_sha)
    doc = {
        "git": {
            "host": host,
            "org": org,
            "repo": repo,
            "ref": ref,
            "ref_type": ref_type,
            "commit": commit_sha,
            "commit_date": commit_date_iso,
        },
        "status": "indexing",
        "indexing_started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "files_count": 0,
        "lines_count": 0,
    }
    es.index(index=REFS_INDEX, id=ref_id, document=doc)


def write_ref_marker(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref_type: str,
    ref: str,
    commit_sha: str,
    commit_date_iso: str | None,
    files_count: int,
    lines_count: int,
) -> None:
    # (ref, ref_type) replaces the old git.branch/git.tag fields: those were write-only and
    # fully reconstructable as `git.ref filtered by git.ref_type`. git.tag was an array that
    # per the id scheme never held more than one element, so a single git.ref keyword is more
    # honest. git.ref is intentionally un-normalized -- git ref names are case-sensitive.
    ref_id = build_ref_id(host, org, repo, ref_type, ref, commit_sha)
    doc = {
        "git": {
            "host": host,
            "org": org,
            "repo": repo,
            "ref": ref,
            "ref_type": ref_type,
            "commit": commit_sha,
            "commit_date": commit_date_iso,
        },
        "status": "complete",
        "files_count": files_count,
        "lines_count": lines_count,
        "indexed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    es.index(index=REFS_INDEX, id=ref_id, document=doc)


def pre_clone_skip(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    branch: str | None,
    tag: str | None,
    commit: str | None,
    clone_url: str,
    force: bool,
    retry_window: datetime.timedelta | None = None,
) -> tuple[bool, str | None, str | None]:
    """
    Cheap pre-clone decision -- no clone needed either way. Returns
    (skip, ref_for_id, remote_sha):
      - skip=True  -> the ref is already fully indexed (or actively being indexed by another
        run within the retry window); the caller should finish "skipped" without cloning.
      - skip=False -> the caller must clone/checkout and run the post-clone path.

    branch/tag resolve via `git ls-remote` against `clone_url`. A pinned commit (-c, or a
    `type: commit` config selector) can't be resolved remotely that way, so it instead checks for
    an already-indexed marker whose commit starts with the given SHA/prefix (commit_prefix_indexed)
    -- a hit skips the clone just like a branch/tag whose tip is already indexed. --force bypasses
    both checks and falls through to cloning; so does an ls-remote failure for branch/tag.
    """
    if force:
        return False, None, None
    if commit:
        full_sha = commit_prefix_indexed(es, host, org, repo, commit.lower())
        if full_sha:
            return True, full_sha, full_sha
        return False, None, None
    remote_sha, resolved_default_branch = resolve_remote(clone_url, branch, tag)
    if not remote_sha:
        return False, None, None
    ref_type = "tag" if tag else "branch"  # branch, or the resolved remote HEAD (a branch)
    ref_for_id = branch or tag or resolved_default_branch
    if ref_for_id and not should_index(
        es, host, org, repo, ref_type, ref_for_id, remote_sha, retry_window=retry_window
    ):
        return True, ref_for_id, remote_sha
    return False, ref_for_id, remote_sha


def resolve_head(es: Elasticsearch, host: str, org: str, repo: str, ref_type: str, ref: str) -> dict | None:
    """The current marker for a ref: the newest commit_date among its (possibly many)
    markers. For a branch with retained history this is its live tip; for a tag it's the
    sole marker. An agent searching `main` resolves branch -> this commit -> that commit's
    content indices, so retained older snapshots never leak into search results. Returns the
    marker _source, or None if the ref has no complete marker."""
    resp = es.search(
        index=REFS_ALIAS,
        size=1,
        query={"bool": {"filter": [
            {"term": {"git.host": host}},
            {"term": {"git.org": org}},
            {"term": {"git.repo": repo}},
            {"term": {"git.ref_type": ref_type}},
            {"term": {"git.ref": ref}},
            {"term": {"status": "complete"}},
        ]}},
        sort=[{"git.commit_date": {"order": "desc"}}],
    )
    hits = resp["hits"]["hits"]
    return hits[0]["_source"] if hits else None
