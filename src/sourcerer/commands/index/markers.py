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
from ...indices import FILES_ALIAS, LINES_ALIAS, REFS_ALIAS, REFS_INDEX, files_index, lines_index
from ...utils import build_ref_key, make_doc_id
from .git import resolve_remote


def marker_routing(marker: dict) -> tuple[str, str | None]:
    """The (index_level, index_suffix) a ref marker recorded, defaulting to the historical
    repo-level routing ("repo"/None) for legacy markers that predate the index.* feature.

    `marker` is a ref doc `_source` (or the subset returned by markers_status_by_id). Centralizing
    the legacy fallback here keeps every reader (skip, migration, prune) agreeing on where a
    pre-feature marker's content lives -- the repo-level default, which is exactly correct."""
    level = marker.get("index_level") or "repo"
    suffix = marker.get("index_suffix")  # None or "" both mean "no suffix"
    return level, (suffix or None)


def build_ref_id(host: str, org: str, repo: str, ref_type: str, ref: str, commit_sha: str) -> str:
    """Content address of one indexed ref state.

    host is folded in so the same org/repo on two different hosting providers never collide.
    (ref_type, ref) identifies the ref -- a branch and a same-named tag ("release",
    "stable") are distinct, and multiple refs can resolve to one commit, so keying on commit
    alone would collapse them and clobber one on the next run. Folding commit in makes a
    moving branch append a new marker per commit (the append-only history that count/age
    pruning needs), while an immutable tag re-hashes to the same id and stays idempotent."""
    return make_doc_id(host, org, repo, ref_type, ref, commit_sha)


def recorded_routing(
    es: Elasticsearch, host: str, org: str, repo: str, ref_type: str, ref: str, commit_sha: str,
) -> tuple[str, str | None] | None:
    """The (index_level, index_suffix) recorded by the complete marker for this exact ref-at-commit,
    or None if there is no such marker. Used by the migration path to reconstruct where the ref's
    content CURRENTLY lives (the OLD index) so it can be cleaned up after re-ingest at the NEW
    index. A legacy marker with no routing fields resolves to the repo-level default via
    marker_routing -- exactly where its pre-feature content sits."""
    ref_id = build_ref_id(host, org, repo, ref_type, ref, commit_sha)
    try:
        resp = es.search(
            index=REFS_ALIAS, size=1, query={"ids": {"values": [ref_id]}},
            source_includes=["status", "index_level", "index_suffix"],
        )
    except NotFoundError:
        return None
    hits = resp["hits"]["hits"]
    if not hits or hits[0]["_source"].get("status") != "complete":
        return None
    return marker_routing(hits[0]["_source"])


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
            source_includes=[
                "status", "git.commit", "indexing_started_at", "index_level", "index_suffix",
            ],
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
            # Recorded index.* routing (legacy fallback handled by marker_routing).
            "index_level": src.get("index_level"),
            "index_suffix": src.get("index_suffix"),
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
    expected_routing: tuple[str, str | None] | None = None,
) -> bool:
    """Pure (no ES) per-ref skip decision -- the decision port of `should_index`.

    Returns True if this ref needs (re)indexing.

    - Missing from status_map: no marker -> must index.
    - status == 'indexing' AND indexing_started_at >= indexing_cutoff: another run is actively
      indexing this ref -> skip (return False). If indexing_cutoff is None the active-run check
      is bypassed (back-compat default).
    - status != 'complete' (stuck/abandoned marker, or no cutoff given): must index.
    - complete marker whose recorded index.* routing != expected_routing: the source now routes to
      a different physical index -> must (re)index at the new location, i.e. migrate. Checked
      before content presence, because the alias-wide content_commits probe is location-blind and
      would otherwise wrongly report the OLD-location content as "present" and skip the migration.
    - commit in content_commits: complete marker AND content still present -> skip.
    - complete marker but content absent (GC'd): must index.

    `status_map` is the result of `markers_status_by_id` and `content_commits` is the result of
    `commits_with_content` over the set of complete-marker commits. Both are computed once per
    repo group before the per-ref loop. `expected_routing` is the (index_level, index_suffix) the
    current source config routes this ref to; None disables the routing-mismatch check.
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
    if expected_routing is not None and marker_routing(marker) != expected_routing:
        return True  # routing changed -> migrate to the new physical index
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


def content_present(
    es: Elasticsearch, host: str, org: str, repo: str, commit_sha: str, at_index: str | None = None,
) -> bool:
    """True if ANY content doc exists for this commit. A cheap presence probe -- NOT proof of a
    complete snapshot, since an interrupted run (Ctrl-C) leaves a partial set of docs behind with
    no marker (see commit_fully_indexed). Used only to detect content GC'd out from under a
    surviving complete marker.

    `at_index` scopes the probe to one physical files index (the source's expected target) instead
    of the whole alias. This makes presence *location-aware*: content sitting only in an OLD index
    after an index.level/suffix change reads as absent at the new target, so the caller re-indexes
    (migrates) rather than wrongly skipping and leaving alias duplicates. A not-yet-created target
    index yields 0 (NotFound), i.e. absent -> index."""
    return count_commit_docs(es, at_index or FILES_ALIAS, host, org, repo, commit_sha) > 0


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
    expected_routing: tuple[str, str | None] | None = None,
) -> bool:
    """
    True if this exact (ref_type, ref, commit) needs (re)indexing. The id now encodes the
    commit, so a moved branch simply misses (NotFound -> index the new commit, old marker
    retained). A present+complete marker is guaranteed to be this commit; the only remaining
    reason to re-index is content GC'd out from under a surviving marker.

    With `retry_window` set, an `indexing` marker whose `indexing_started_at` is within the
    window is treated as an active concurrent run and returns False (skip). Without it (default),
    any non-complete marker triggers re-indexing (previous behavior).

    `expected_routing` is the (index_level, index_suffix) the current source config routes this
    ref to. When given, this is the authoritative, location-aware post-clone guard: a complete
    marker whose recorded routing differs means the source now targets a different physical index,
    so we must (re)index there (migrate); and the content-presence check is scoped to that target
    index rather than the whole alias, so an old-location copy doesn't mask the need to migrate.
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
    at_index = None
    if expected_routing is not None:
        if marker_routing(marker) != expected_routing:
            return True  # routing changed -> migrate to the new physical index
        level, suffix = expected_routing
        at_index = files_index(host, org, repo, commit_sha, level, suffix)
    return not content_present(es, host, org, repo, commit_sha, at_index=at_index)


def write_indexing_marker(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref_type: str,
    ref: str,
    commit_sha: str,
    commit_date_iso: str | None,
    index_level: str = "repo",
    index_suffix: str | None = None,
) -> None:
    """Write a status:'indexing' marker for a ref that is about to be ingested.

    Uses the same build_ref_id-keyed doc as write_ref_marker, so the terminal write_ref_marker
    call (status:'complete') will overwrite this marker in place once ingest completes.

    This marker allows the schedule gate to detect that another run is currently indexing this
    source's scope (host/org/repo/ref_type) and skip it, preventing redundant parallel work.
    If the run dies before calling write_ref_marker, the indexing marker stays behind; the gate
    retries the source after the retry window elapses (default 1h, configurable via
    --retry-window).

    `index_level`/`index_suffix` record this source's index.* routing so prune/migration can
    reconstruct where the content physically lives (see write_ref_marker).
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
        "index_level": index_level,
        "index_suffix": index_suffix,
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
    index_level: str = "repo",
    index_suffix: str | None = None,
) -> None:
    # (ref, ref_type) replaces the old git.branch/git.tag fields: those were write-only and
    # fully reconstructable as `git.ref filtered by git.ref_type`. git.tag was an array that
    # per the id scheme never held more than one element, so a single git.ref keyword is more
    # honest. git.ref is intentionally un-normalized -- git ref names are case-sensitive.
    #
    # index_level/index_suffix record this source's index.* routing (semantic, not the resolved
    # index name). The physical files/lines index is reconstructed on demand from git.host/org/
    # repo/commit + these two fields via indices.files_index/lines_index, so a v2->v3 prefix bump
    # stays correct and prune/migration can find (and clean up) exactly where content lives.
    # Legacy markers written before this feature omit both; readers fall back to the "repo"/None
    # defaults, which reconstruct to the historical repo-level name where that content actually is.
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
        "index_level": index_level,
        "index_suffix": index_suffix,
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
    expected_routing: tuple[str, str | None] | None = None,
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
            # A routing change (index.level/suffix) must not be pre-clone skipped: the pinned
            # commit needs re-indexing at the new location. commit_prefix_indexed found a complete
            # marker for this commit; only skip if its recorded routing still matches.
            if expected_routing is not None and (
                recorded_routing(es, host, org, repo, "commit", full_sha, full_sha) != expected_routing
            ):
                return False, None, None
            return True, full_sha, full_sha
        return False, None, None
    remote_sha, resolved_default_branch = resolve_remote(clone_url, branch, tag)
    if not remote_sha:
        return False, None, None
    ref_type = "tag" if tag else "branch"  # branch, or the resolved remote HEAD (a branch)
    ref_for_id = branch or tag or resolved_default_branch
    if ref_for_id and not should_index(
        es, host, org, repo, ref_type, ref_for_id, remote_sha,
        retry_window=retry_window, expected_routing=expected_routing,
    ):
        return True, ref_for_id, remote_sha
    return False, ref_for_id, remote_sha


# --- refs join docs (git.ref_key), keyed by `_id = ref_key` -------------------------------
# One document per `ref_key` (INV-004): snapshot content's join doc lives at `_id = <commit>`;
# an incremental branch's single join doc lives at `_id = {host}~{org}~{repo}~{ref}` and its
# `git.commit` is the branch's live HEAD, advanced only by a two-phase indexing -> ready
# publication (INV-006). These are a DISTINCT id space from `build_ref_id`'s hashed, append-only
# ref-name markers above (untouched -- they still drive `since`/retention history); a join doc's
# `_id` is a plain, unhashed `ref_key` string, which a `build_ref_id` hash can never collide with.

ERROR_MAX_LEN = 2000  # bound stored failure text so a giant git/ES error can't bloat the doc


def write_snapshot_join_doc(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref_type: str,
    ref: str,
    commit_sha: str,
    commit_date_iso: str | None,
    refresh: bool = False,
) -> None:
    """Write (or idempotently re-write) the snapshot refs join doc: `_id = commit_sha`,
    `git.ref_key = commit_sha`, `git.commit = commit_sha` (the commit is its own citable
    identity). `ref`/`ref_type` record the ref that produced this write (informational only --
    multiple refs resolving to the same commit all write the same doc, so re-writes are a
    no-op change)."""
    doc = {
        "git": {
            "ref_key": commit_sha,
            "host": host,
            "org": org,
            "repo": repo,
            "ref": ref,
            "ref_type": ref_type,
            "commit": commit_sha,
            "commit_date": commit_date_iso,
        },
        "update_mode": "snapshot",
        "status": "complete",
    }
    es.index(index=REFS_INDEX, id=commit_sha, document=doc, refresh=refresh)


def read_incremental_ref(es: Elasticsearch, host: str, org: str, repo: str, ref: str) -> dict | None:
    """The branch's incremental join doc `_source`, or None if never indexed. A real-time GET
    (by `_id = ref_key`), so it reflects the last write even without a refresh."""
    try:
        return es.get(index=REFS_INDEX, id=build_ref_key(host, org, repo, ref))["_source"]
    except NotFoundError:
        return None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_incremental_join_doc(
    host: str,
    org: str,
    repo: str,
    ref: str,
    *,
    status: str,
    commit: str | None,
    target_commit: str | None = None,
    commit_date_iso: str | None = None,
    files_count: int = 0,
    lines_count: int = 0,
    indexed_at: str | None = None,
    indexing_started_at: str | None = None,
    failed_at: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "git": {
            "ref_key": build_ref_key(host, org, repo, ref),
            "host": host,
            "org": org,
            "repo": repo,
            "ref": ref,
            "ref_type": "branch",
            "commit": commit,
            "target_commit": target_commit,
            "commit_date": commit_date_iso,
        },
        "update_mode": "incremental",
        "status": status,
        "files_count": files_count,
        "lines_count": lines_count,
        "indexed_at": indexed_at,
        "indexing_started_at": indexing_started_at,
        "failed_at": failed_at,
        "error": error[:ERROR_MAX_LEN] if error else None,
    }


def write_incremental_indexing(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref: str,
    completed_commit: str | None,
    target_commit: str,
    prior: dict | None = None,
    refresh: bool = False,
) -> None:
    """Publish `status: indexing`: the completed pointer (`git.commit`) stays at the LAST
    completed SHA (or None on a first index) while `git.target_commit` advertises the candidate
    SHA the run is advancing to. A failed run never overwrites `git.commit` with `target_commit`
    (INV-006) -- only `write_incremental_ready` does that, after delete+index+refresh succeed."""
    prior = prior or {}
    pg = prior.get("git", {})
    doc = _build_incremental_join_doc(
        host, org, repo, ref,
        status="indexing",
        commit=completed_commit,
        target_commit=target_commit,
        commit_date_iso=pg.get("commit_date"),
        files_count=prior.get("files_count", 0),
        lines_count=prior.get("lines_count", 0),
        indexed_at=prior.get("indexed_at"),
        indexing_started_at=_now_iso(),
        failed_at=prior.get("failed_at"),
        error=prior.get("error"),
    )
    es.index(index=REFS_INDEX, id=build_ref_key(host, org, repo, ref), document=doc, refresh=refresh)


def write_incremental_ready(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref: str,
    commit: str,
    commit_date_iso: str | None,
    files_count: int,
    lines_count: int,
    refresh: bool = True,
) -> None:
    """Publish `status: ready` at the NEW completed commit, clearing `target_commit` and any
    prior failure fields. This is the pointer-advancing publication boundary (INV-006): callers
    must delete+index+refresh the content indices FIRST, then call this."""
    doc = _build_incremental_join_doc(
        host, org, repo, ref,
        status="ready",
        commit=commit,
        target_commit=None,
        commit_date_iso=commit_date_iso,
        files_count=files_count,
        lines_count=lines_count,
        indexed_at=_now_iso(),
        indexing_started_at=None,
        failed_at=None,
        error=None,
    )
    es.index(index=REFS_INDEX, id=build_ref_key(host, org, repo, ref), document=doc, refresh=refresh)


def write_incremental_failed(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref: str,
    completed_commit: str | None,
    target_commit: str | None,
    error: str,
    prior: dict | None = None,
    refresh: bool = False,
) -> None:
    """Record a failed update WITHOUT advancing the completed pointer: status stays `indexing`,
    `git.commit` remains the last completed SHA, and a bounded `error`/`failed_at` are stored for
    diagnosis (INV-006). The next run retries old -> current and clears these on success."""
    prior = prior or {}
    pg = prior.get("git", {})
    doc = _build_incremental_join_doc(
        host, org, repo, ref,
        status="indexing",
        commit=completed_commit,
        target_commit=target_commit,
        commit_date_iso=pg.get("commit_date"),
        files_count=prior.get("files_count", 0),
        lines_count=prior.get("lines_count", 0),
        indexed_at=prior.get("indexed_at"),
        indexing_started_at=prior.get("indexing_started_at") or _now_iso(),
        failed_at=_now_iso(),
        error=error,
    )
    es.index(index=REFS_INDEX, id=build_ref_key(host, org, repo, ref), document=doc, refresh=refresh)


def _delete_by_query_sync(es: Elasticsearch, index: str, query: dict, refresh: bool) -> None:
    """Synchronous delete-by-query used by the incremental path. Unlike the async prune
    deletion, this waits for completion so a subsequent re-index can't race a still-running
    delete, and uses `conflicts="proceed"` so a concurrent version bump doesn't abort the batch.
    A missing index (first index, before any content exists) is ignored."""
    try:
        es.delete_by_query(
            index=index,
            query=query,
            wait_for_completion=True,
            conflicts="proceed",
            refresh=refresh,
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except NotFoundError:
        pass


def delete_incremental_paths(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref: str,
    paths,
    index_level: str = "repo",
    index_suffix: str | None = None,
    refresh: bool = False,
) -> None:
    """Synchronously delete the file and line docs for `paths` on this exact branch. Scoped by
    the exact `git.ref_key` (a single keyword term, so one branch's docs can never bleed into
    another's -- INV-008) plus a `file.path` terms filter, never a wildcard. A no-op for an
    empty path set."""
    paths = list(paths)
    if not paths:
        return
    ref_key = build_ref_key(host, org, repo, ref)
    query = {
        "bool": {
            "filter": [
                {"term": {"git.ref_key": ref_key}},
                {"terms": {"file.path": paths}},
            ]
        }
    }
    for index in (
        files_index(host, org, repo, None, index_level, index_suffix),
        lines_index(host, org, repo, None, index_level, index_suffix),
    ):
        _delete_by_query_sync(es, index, query, refresh)


def delete_incremental_branch(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref: str,
    index_level: str = "repo",
    index_suffix: str | None = None,
    refresh: bool = False,
) -> None:
    """Delete EVERY incremental content doc for this branch (full namespace), scoped by the
    exact `git.ref_key` (INV-008). Used for the initial index and the missing-diff-base rebuild
    (INV-007)."""
    ref_key = build_ref_key(host, org, repo, ref)
    query = {"bool": {"filter": [{"term": {"git.ref_key": ref_key}}]}}
    for index in (
        files_index(host, org, repo, None, index_level, index_suffix),
        lines_index(host, org, repo, None, index_level, index_suffix),
    ):
        _delete_by_query_sync(es, index, query, refresh)


def count_incremental_branch_docs(
    es: Elasticsearch, host: str, org: str, repo: str, ref: str,
    index_level: str = "repo", index_suffix: str | None = None,
) -> tuple[int, int]:
    """Authoritative (files, lines) totals for a branch's current incremental view, counted by
    exact `git.ref_key`. Call AFTER refreshing the content indices so counts reflect the just-
    applied deletes and indexes -- these become the ready marker's files_count/lines_count.
    Returns 0 for an index that does not exist yet."""
    ref_key = build_ref_key(host, org, repo, ref)
    query = {"bool": {"filter": [{"term": {"git.ref_key": ref_key}}]}}

    def _count(index: str) -> int:
        try:
            return int(es.count(index=index, query=query)["count"])
        except NotFoundError:
            return 0

    return (
        _count(files_index(host, org, repo, None, index_level, index_suffix)),
        _count(lines_index(host, org, repo, None, index_level, index_suffix)),
    )


def refresh_incremental_content(
    es: Elasticsearch, host: str, org: str, repo: str,
    index_level: str = "repo", index_suffix: str | None = None,
) -> None:
    """Make the branch's just-written incremental content visible before the ready pointer is
    published (INV-006: content refresh precedes the final refs write). Best-effort over
    missing indices."""
    es.indices.refresh(
        index=[
            files_index(host, org, repo, None, index_level, index_suffix),
            lines_index(host, org, repo, None, index_level, index_suffix),
        ],
        ignore_unavailable=True,
        allow_no_indices=True,
    )


# --- one-time upgrade backfill (default-on; --no-backfill opts out) -----------------------
# Stamps `git.ref_key`/`update_mode` onto pre-existing snapshot content that predates this
# feature, migrates the refs index mapping, and creates the missing `_id = commit` join docs
# (INV-009/INV-010). Safe to run on every `index` invocation: both the content update and the
# join-doc creation are no-ops the second time.

def backfill_snapshot_ref_keys(es: Elasticsearch, host: str, org: str, repo: str) -> int:
    """Idempotent `_update_by_query` stamping `git.ref_key = git.commit` + `update_mode:
    "snapshot"` onto this repo's content docs that lack `git.ref_key` (pre-upgrade data).
    Returns the total number of docs updated across the files and lines aliases; 0 on a repeat
    run (INV-009) since the `must_not: exists` filter then matches nothing."""
    query = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
            ],
            "must_not": [{"exists": {"field": "git.ref_key"}}],
        }
    }
    script = {
        "source": (
            "ctx._source.git.ref_key = ctx._source.git.commit; "
            "ctx._source.update_mode = 'snapshot';"
        ),
        "lang": "painless",
    }
    total = 0
    for index in (FILES_ALIAS, LINES_ALIAS):
        try:
            resp = es.update_by_query(
                index=index, query=query, script=script,
                wait_for_completion=True, conflicts="proceed", refresh=True,
                ignore_unavailable=True, allow_no_indices=True,
            )
            total += int(resp.get("updated", 0))
        except NotFoundError:
            pass
    return total


def distinct_commits_for_repo(es: Elasticsearch, host: str, org: str, repo: str) -> set[str]:
    """Every distinct `git.commit` present in this repo's content, via a terms aggregation.
    Used by the backfill to find every already-indexed snapshot commit that needs a refs join
    doc (INV-010). Returns an empty set when the files index doesn't exist yet."""
    query = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
            ]
        }
    }
    try:
        resp = es.search(
            index=FILES_ALIAS, size=0, query=query,
            aggs={"commits": {"terms": {"field": "git.commit", "size": 10000}}},
        )
    except NotFoundError:
        return set()
    return {b["key"] for b in resp["aggregations"]["commits"]["buckets"]}


def commits_with_join_doc(es: Elasticsearch, commits: set[str]) -> set[str]:
    """The subset of `commits` that already have a `_id = commit` refs join doc. A batched
    ids lookup, the join-doc analogue of `commits_with_content`."""
    if not commits:
        return set()
    try:
        resp = es.search(
            index=REFS_ALIAS, size=len(commits), query={"ids": {"values": sorted(commits)}},
            source_includes=[],
        )
    except NotFoundError:
        return set()
    return {hit["_id"] for hit in resp["hits"]["hits"]}


def backfill_refs_join_docs(es: Elasticsearch, host: str, org: str, repo: str) -> int:
    """Ensure a snapshot `_id = commit` join doc exists for every distinct content commit in
    this repo (INV-010). For each missing commit, an existing `build_ref_id` marker (if any)
    supplies the ref/ref_type/commit_date it was originally indexed under; falls back to
    generic values if none is found (the content is authoritative either way -- the join doc's
    ref/ref_type are informational). Returns the number of join docs created; 0 on a repeat run
    (INV-009)."""
    commits = distinct_commits_for_repo(es, host, org, repo)
    if not commits:
        return 0
    missing = commits - commits_with_join_doc(es, commits)
    created = 0
    for commit_sha in missing:
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
            resp = es.search(index=REFS_ALIAS, size=1, query=query)
        except NotFoundError:
            resp = {"hits": {"hits": []}}
        hits = resp["hits"]["hits"]
        if hits:
            src_git = hits[0]["_source"].get("git", {})
            ref = src_git.get("ref") or commit_sha
            ref_type = src_git.get("ref_type") or "commit"
            commit_date_iso = src_git.get("commit_date")
        else:
            ref, ref_type, commit_date_iso = commit_sha, "commit", None
        write_snapshot_join_doc(es, host, org, repo, ref_type, ref, commit_sha, commit_date_iso)
        created += 1
    if created:
        # Refresh so a uniqueness gate run immediately afterward (see command._run_uniqueness_gate)
        # sees every join doc just created rather than racing the refs index's refresh interval.
        try:
            es.indices.refresh(index=REFS_INDEX)
        except NotFoundError:
            pass
    return created


def apply_refs_index_mapping(es: Elasticsearch, mapping: dict) -> None:
    """Apply an updated mapping to the physical REFS_INDEX. A `put_index_template` change alone
    (see `setup`) only affects indices created AFTER the change -- an existing repo's refs index
    predates the `git.ref_key` field and needs its mapping updated explicitly so the field is
    typed as intended rather than dynamically guessed on first write. A no-op if the index
    doesn't exist yet."""
    try:
        es.indices.put_mapping(index=REFS_INDEX, properties=mapping.get("properties", {}))
    except NotFoundError:
        pass


def apply_content_index_mapping(es: Elasticsearch, files_mapping: dict, lines_mapping: dict) -> None:
    """Apply the updated files/lines template mappings to every EXISTING physical content index
    behind the read aliases. This must run BEFORE `backfill_snapshot_ref_keys` writes
    `git.ref_key`/`update_mode` onto pre-existing content: an index created before this feature
    has no explicit mapping for those fields, so the first `_update_by_query` write would
    otherwise fall back to ES's dynamic string mapping (`text`, no fielddata) instead of the
    `keyword` type the template defines -- silently breaking every later `git.ref_key`
    aggregation/sort/exact-match query. `put_mapping` against an alias updates every backing
    index it resolves to. A no-op if neither alias has any backing index yet."""
    for alias, mapping in ((FILES_ALIAS, files_mapping), (LINES_ALIAS, lines_mapping)):
        try:
            es.indices.put_mapping(index=alias, properties=mapping.get("properties", {}))
        except NotFoundError:
            pass


def backfill_repo(
    es: Elasticsearch, host: str, org: str, repo: str, refs_mapping: dict | None = None,
    files_mapping: dict | None = None, lines_mapping: dict | None = None,
) -> dict:
    """Run the full one-time upgrade for one repo: apply the updated content/refs index
    mappings (once, if given -- must happen BEFORE the content update so the new fields land
    typed correctly rather than dynamically guessed), stamp `ref_key`/`update_mode` onto
    pre-existing snapshot content (idempotent), and create a join doc for every already-indexed
    commit that lacks one. Returns a small summary dict for reporting; every field is 0 on a
    repeat run (INV-009)."""
    if files_mapping is not None and lines_mapping is not None:
        apply_content_index_mapping(es, files_mapping, lines_mapping)
    if refs_mapping is not None:
        apply_refs_index_mapping(es, refs_mapping)
    updated = backfill_snapshot_ref_keys(es, host, org, repo)
    created = backfill_refs_join_docs(es, host, org, repo)
    return {"content_updated": updated, "join_docs_created": created}


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
