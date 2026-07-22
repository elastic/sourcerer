# sourcerer/commands/index/markers.py
# Refs-index idempotency: content-addressing a ref's indexed state, the guards that decide
# whether a ref needs (re)indexing, and writing the completion marker. Everything here reads or
# writes sourcerer-v1-refs / the per-repo content indices for exactly one commit at a time;
# broader read-only queries across the whole cluster live in sourcerer/queries.py.

# Standard packages
import datetime

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError

# App packages
from ...indices import (
    REFS_INDEX,
    REFS_INDEX_V2,
    files_index,
    files_index_v2,
    lines_index_v2,
)
from ...utils import build_ref_key, make_doc_id
from .git import resolve_remote


def build_ref_id(org: str, repo: str, ref_type: str, ref: str, commit_sha: str) -> str:
    """Content address of one indexed ref state.

    (ref_type, ref) identifies the ref -- a branch and a same-named tag ("release",
    "stable") are distinct, and multiple refs can resolve to one commit, so keying on commit
    alone would collapse them and clobber one on the next run. Folding commit in makes a
    moving branch append a new marker per commit (the append-only history that count/age
    pruning needs), while an immutable tag re-hashes to the same id and stays idempotent."""
    return make_doc_id(org, repo, ref_type, ref, commit_sha)


def count_commit_docs(es: Elasticsearch, index: str, org: str, repo: str, commit_sha: str) -> int:
    """Count docs in `index` for one commit. Content is keyed by (org, repo, commit, path),
    so the commit alone identifies a snapshot regardless of which ref reached it.

    Returns 0 when the index does not yet exist (indices are created on demand by the first
    write, so a brand-new repo has no index until its first successful ingest)."""
    query = {
        "bool": {
            "filter": [
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


def content_present(es: Elasticsearch, org: str, repo: str, commit_sha: str) -> bool:
    """True if ANY content doc exists for this commit. A cheap presence probe -- NOT proof of a
    complete snapshot, since an interrupted run (Ctrl-C) leaves a partial set of docs behind with
    no marker (see commit_fully_indexed). Used only to detect content GC'd out from under a
    surviving complete marker."""
    return count_commit_docs(es, files_index(org, repo), org, repo, commit_sha) > 0


def commit_fully_indexed(es: Elasticsearch, org: str, repo: str, commit_sha: str) -> bool:
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
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"term": {"git.commit": commit_sha}},
                {"term": {"status": "complete"}},
            ]
        }
    }
    try:
        return int(es.count(index=REFS_INDEX, query=query)["count"]) > 0
    except NotFoundError:
        return False


def commit_prefix_indexed(es: Elasticsearch, org: str, repo: str, sha_prefix: str) -> str | None:
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
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"prefix": {"git.commit": sha_prefix}},
                {"term": {"status": "complete"}},
            ]
        }
    }
    try:
        resp = es.search(index=REFS_INDEX, size=1, query=query)
    except NotFoundError:
        return None
    hits = resp["hits"]["hits"]
    return hits[0]["_source"]["git"]["commit"] if hits else None


def should_index(es: Elasticsearch, org: str, repo: str, ref_type: str, ref: str, commit_sha: str) -> bool:
    """
    True if this exact (ref_type, ref, commit) needs (re)indexing. The id now encodes the
    commit, so a moved branch simply misses (NotFound -> index the new commit, old marker
    retained). A present+complete marker is guaranteed to be this commit; the only remaining
    reason to re-index is content GC'd out from under a surviving marker.
    """
    ref_id = build_ref_id(org, repo, ref_type, ref, commit_sha)
    try:
        marker = es.get(index=REFS_INDEX, id=ref_id)["_source"]
    except NotFoundError:
        return True
    if marker.get("status") != "complete":
        return True
    return not content_present(es, org, repo, commit_sha)


def write_ref_marker(
    es: Elasticsearch,
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
    ref_id = build_ref_id(org, repo, ref_type, ref, commit_sha)
    doc = {
        "git": {
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
    org: str,
    repo: str,
    branch: str | None,
    tag: str | None,
    commit: str | None,
    force: bool,
) -> tuple[bool, str | None, str | None]:
    """
    Cheap pre-clone decision -- no clone needed either way. Returns
    (skip, ref_for_id, remote_sha):
      - skip=True  -> the ref is already fully indexed; the caller should finish "skipped"
        (using the returned ref_for_id) without cloning.
      - skip=False -> the caller must clone/checkout and run the post-clone path.

    branch/tag resolve via `git ls-remote`. A pinned commit (-c, or a `type: commit` config
    selector) can't be resolved remotely that way, so it instead checks for an already-indexed
    marker whose commit starts with the given SHA/prefix (commit_prefix_indexed) -- a hit skips
    the clone just like a branch/tag whose tip is already indexed. --force bypasses both checks
    and falls through to cloning; so does an ls-remote failure for branch/tag.
    """
    if force:
        return False, None, None
    if commit:
        full_sha = commit_prefix_indexed(es, org, repo, commit.lower())
        if full_sha:
            return True, full_sha, full_sha
        return False, None, None
    remote_sha, default_branch = resolve_remote(org, repo, branch, tag)
    if not remote_sha:
        return False, None, None
    ref_type = "tag" if tag else "branch"  # branch, or the resolved remote HEAD (a branch)
    ref_for_id = branch or tag or default_branch
    if ref_for_id and not should_index(es, org, repo, ref_type, ref_for_id, remote_sha):
        return True, ref_for_id, remote_sha
    return False, ref_for_id, remote_sha


# --- v2 (incremental) mutable branch markers ------------------------------------------------
# The v2 refs index holds exactly ONE document per incremental branch (INV-004), keyed by a
# stable id that folds in only (org, repo, "branch", ref) -- never the commit -- so successive
# updates overwrite the same document in place. Its `status`/`git.commit`/`git.target_commit`
# fields make the update window observable without blocking readers (INV-005/INV-008).

ERROR_MAX_LEN = 2000  # bound stored failure text so a giant git/ES error can't bloat the doc


def build_v2_ref_id(org: str, repo: str, ref: str) -> str:
    """Stable id of an incremental branch's single v2 refs document (INV-004). Normalized
    org/repo lowercasing matches the content ids and ref-key so identity is consistent; the
    branch name stays case-sensitive."""
    return make_doc_id(org.lower(), repo.lower(), "branch", ref)


def read_v2_ref(es: Elasticsearch, org: str, repo: str, ref: str) -> dict | None:
    """The branch's v2 refs document _source, or None if it has never been indexed. A
    real-time GET, so it reflects the last write even without an index refresh."""
    try:
        return es.get(index=REFS_INDEX_V2, id=build_v2_ref_id(org, repo, ref))["_source"]
    except NotFoundError:
        return None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_v2_ref_doc(
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
    update_started_at: str | None = None,
    failed_at: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "git": {
            "ref_key": build_ref_key(org, repo, ref),
            "org": org.lower(),
            "repo": repo.lower(),
            "ref": ref,
            "ref_type": "branch",
            "commit": commit,
            "target_commit": target_commit,
            "commit_date": commit_date_iso,
        },
        "status": status,
        "update_mode": "incremental",
        "files_count": files_count,
        "lines_count": lines_count,
        "indexed_at": indexed_at,
        "update_started_at": update_started_at,
        "failed_at": failed_at,
        "error": error[:ERROR_MAX_LEN] if error else None,
    }


def write_v2_indexing(
    es: Elasticsearch,
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
    SHA (INV-005). Prior counts/commit_date/indexed_at are carried so readers keep meaningful
    metadata during the window. Not a publication boundary -- refresh defaults off; a real-time
    GET still sees it on retry."""
    prior = prior or {}
    pg = prior.get("git", {})
    doc = _build_v2_ref_doc(
        org, repo, ref,
        status="indexing",
        commit=completed_commit,
        target_commit=target_commit,
        commit_date_iso=pg.get("commit_date"),
        files_count=prior.get("files_count", 0),
        lines_count=prior.get("lines_count", 0),
        indexed_at=prior.get("indexed_at"),
        update_started_at=_now_iso(),
        failed_at=prior.get("failed_at"),
        error=prior.get("error"),
    )
    es.index(index=REFS_INDEX_V2, id=build_v2_ref_id(org, repo, ref), document=doc, refresh=refresh)


def write_v2_ready(
    es: Elasticsearch,
    org: str,
    repo: str,
    ref: str,
    commit: str,
    commit_date_iso: str | None,
    files_count: int,
    lines_count: int,
    refresh: bool = True,
) -> None:
    """Publish `status: ready` at the NEW completed commit, clearing the target and any prior
    failure fields (INV-005/INV-008). This is the pointer-advancing publication boundary, so it
    refreshes by default -- callers refresh the content indices first, then call this."""
    doc = _build_v2_ref_doc(
        org, repo, ref,
        status="ready",
        commit=commit,
        target_commit=None,
        commit_date_iso=commit_date_iso,
        files_count=files_count,
        lines_count=lines_count,
        indexed_at=_now_iso(),
        update_started_at=None,
        failed_at=None,
        error=None,
    )
    es.index(index=REFS_INDEX_V2, id=build_v2_ref_id(org, repo, ref), document=doc, refresh=refresh)


def write_v2_failed(
    es: Elasticsearch,
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
    `git.commit` remains the last completed SHA, and a bounded `error` + `failed_at` are stored
    for diagnosis (INV-005). The next run retries old->current and clears these on success."""
    prior = prior or {}
    pg = prior.get("git", {})
    doc = _build_v2_ref_doc(
        org, repo, ref,
        status="indexing",
        commit=completed_commit,
        target_commit=target_commit,
        commit_date_iso=pg.get("commit_date"),
        files_count=prior.get("files_count", 0),
        lines_count=prior.get("lines_count", 0),
        indexed_at=prior.get("indexed_at"),
        update_started_at=prior.get("update_started_at") or _now_iso(),
        failed_at=_now_iso(),
        error=error,
    )
    es.index(index=REFS_INDEX_V2, id=build_v2_ref_id(org, repo, ref), document=doc, refresh=refresh)


def _delete_by_query_sync(es: Elasticsearch, index: str, query: dict, refresh: bool) -> None:
    """Synchronous delete-by-query used by the incremental path. Unlike the async prune
    deletion, this waits for completion (`wait_for_completion=True`) so a subsequent re-index
    can't race a still-running delete, and uses `conflicts="proceed"` so a concurrent version
    bump doesn't abort the batch. Missing indices (a first index, before any content exists)
    are ignored."""
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


def delete_v2_paths(
    es: Elasticsearch, org: str, repo: str, ref: str, paths, refresh: bool = False,
) -> None:
    """Synchronously delete the file and line docs for `paths` on this exact branch from the v2
    content indices. Scoped by the exact `git.ref_key` (a single keyword term, so a branch
    whose name is a prefix of another can't bleed) plus a `file.path` terms filter -- never a
    wildcard. A no-op for an empty path set."""
    paths = list(paths)
    if not paths:
        return
    ref_key = build_ref_key(org, repo, ref)
    query = {
        "bool": {
            "filter": [
                {"term": {"git.ref_key": ref_key}},
                {"terms": {"file.path": paths}},
            ]
        }
    }
    for index in (files_index_v2(org, repo), lines_index_v2(org, repo)):
        _delete_by_query_sync(es, index, query, refresh)


def delete_v2_branch(
    es: Elasticsearch, org: str, repo: str, ref: str, refresh: bool = False,
) -> None:
    """Delete EVERY v2 content doc for this branch (full namespace), scoped by the exact
    `git.ref_key`. Used for the initial index and the missing-diff-base rebuild (INV-007)."""
    ref_key = build_ref_key(org, repo, ref)
    query = {"bool": {"filter": [{"term": {"git.ref_key": ref_key}}]}}
    for index in (files_index_v2(org, repo), lines_index_v2(org, repo)):
        _delete_by_query_sync(es, index, query, refresh)


def count_v2_branch_docs(es: Elasticsearch, org: str, repo: str, ref: str) -> tuple[int, int]:
    """Authoritative (files, lines) totals for a branch's current v2 view, counted by exact
    `git.ref_key`. Call AFTER refreshing the content indices so the counts reflect the just-
    applied deletes and indexes -- these become the ready marker's files_count/lines_count.
    Returns 0 for an index that does not exist yet."""
    ref_key = build_ref_key(org, repo, ref)
    query = {"bool": {"filter": [{"term": {"git.ref_key": ref_key}}]}}

    def _count(index: str) -> int:
        try:
            return int(es.count(index=index, query=query)["count"])
        except NotFoundError:
            return 0

    return _count(files_index_v2(org, repo)), _count(lines_index_v2(org, repo))


def refresh_v2_content(es: Elasticsearch, org: str, repo: str) -> None:
    """Make the branch's just-written v2 content visible before the ready pointer is published
    (INV-008: content refresh precedes the final refs write). Best-effort over missing indices."""
    es.indices.refresh(
        index=[files_index_v2(org, repo), lines_index_v2(org, repo)],
        ignore_unavailable=True,
        allow_no_indices=True,
    )


def resolve_head(es: Elasticsearch, org: str, repo: str, ref_type: str, ref: str) -> dict | None:
    """The current marker for a ref: the newest commit_date among its (possibly many)
    markers. For a branch with retained history this is its live tip; for a tag it's the
    sole marker. An agent searching `main` resolves branch -> this commit -> that commit's
    content indices, so retained older snapshots never leak into search results. Returns the
    marker _source, or None if the ref has no complete marker."""
    resp = es.search(
        index=REFS_INDEX,
        size=1,
        query={"bool": {"filter": [
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
