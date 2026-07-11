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
from ...indices import REFS_INDEX, files_index
from ...utils import make_doc_id
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
