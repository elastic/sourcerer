# sourcerer/commands/index/documents.py
# Turns a checked-out commit's tracked files into ES bulk actions and loads them: per-file/
# per-line doc construction (farmed out to worker processes, since generation is the throughput
# ceiling) and the parallel_bulk ingest loop that consumes them.

# Standard packages
import pathlib
import signal
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor

# Third-party packages
from elasticsearch import Elasticsearch
from elasticsearch.helpers import parallel_bulk as es_parallel_bulk

# App packages
from ...indices import files_index, files_index_v2, lines_index, lines_index_v2
from ...utils import build_ref_key, make_doc_id
from .git import iter_tracked_files
from .runtime import _aborted, _tuning


def file_attributes(path: pathlib.Path) -> list[str]:
    attrs = []
    if path.is_symlink():
        attrs.append("symlink")
    else:
        try:
            if path.stat().st_mode & 0o111:
                attrs.append("executable")
        except OSError:
            pass
    return attrs


def build_file_doc(
    org: str,
    repo: str,
    commit_sha: str,
    rel_path: str,
    abs_path: pathlib.Path,
) -> tuple[str, dict]:
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    try:
        size = abs_path.stat().st_size
    except OSError:
        size = abs_path.lstat().st_size
    doc = {
        "git": {
            "org": org,
            "repo": repo,
            "commit": commit_sha,
        },
        "file": {
            "path": rel_path,
            "directory": directory,
            "name": p.name,
            "extension": extension,
            "size": size,
            "attributes": file_attributes(abs_path) or None,
        }
    }
    # Content identity is (org, repo, commit, path): the same blob reached via any ref
    # (branch/tag/commit) collapses to one doc. Branch is intentionally absent -- it lives
    # only in the refs index (see markers.write_ref_marker) since a branch moves.
    _id = make_doc_id(org, repo, commit_sha, rel_path)
    return _id, doc


def build_file_doc_v2(
    org: str,
    repo: str,
    ref: str,
    rel_path: str,
    abs_path: pathlib.Path,
) -> tuple[str, dict]:
    """v2 (incremental) file doc: ref-addressed, NOT commit-addressed. Identity is
    (org, repo, "branch", ref, path) so the same branch/path is one stable doc across commits
    (INV-003); the completed commit lives only in the v2 refs lookup document. org/repo are
    normalized to lowercase for both the stored fields and the id, since the v2 mappings carry
    no normalizer, while ref stays case-sensitive."""
    org_l, repo_l = org.lower(), repo.lower()
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    try:
        size = abs_path.stat().st_size
    except OSError:
        size = abs_path.lstat().st_size
    doc = {
        "git": {
            "org": org_l,
            "repo": repo_l,
            "ref_key": build_ref_key(org, repo, ref),
            "ref": ref,
            "ref_type": "branch",
        },
        "file": {
            "path": rel_path,
            "directory": directory,
            "name": p.name,
            "extension": extension,
            "size": size,
            "attributes": file_attributes(abs_path) or None,
        },
    }
    _id = make_doc_id(org_l, repo_l, "branch", ref, rel_path)
    return _id, doc


def iter_line_docs_v2(
    org: str,
    repo: str,
    ref: str,
    rel_path: str,
    content: str,
) -> Iterator[tuple[str, dict]]:
    """v2 (incremental) per-line docs: ref-addressed line identity
    (org, repo, "branch", ref, path, line_number), with no commit in the id or source."""
    org_l, repo_l = org.lower(), repo.lower()
    ref_key = build_ref_key(org, repo, ref)
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    base = {
        "git": {
            "org": org_l,
            "repo": repo_l,
            "ref_key": ref_key,
            "ref": ref,
            "ref_type": "branch",
        },
        "file": {
            "path": rel_path,
            "directory": directory,
            "name": p.name,
            "extension": extension,
        },
    }
    for line_num, line_content in enumerate(content.splitlines(), start=1):
        _id = make_doc_id(org_l, repo_l, "branch", ref, rel_path, str(line_num))
        yield _id, {**base, "line": {"number": line_num, "content": line_content}}


def iter_line_docs(
    org: str,
    repo: str,
    commit_sha: str,
    rel_path: str,
    content: str,
) -> Iterator[tuple[str, dict]]:
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    base = {
        "git": {
            "org": org,
            "repo": repo,
            "commit": commit_sha,
        },
        "file": {
            "path": rel_path,
            "directory": directory,
            "name": p.name,
            "extension": extension,
        }
    }
    for line_num, line_content in enumerate(content.splitlines(), start=1):
        _id = make_doc_id(org, repo, commit_sha, rel_path, str(line_num))
        yield _id, {**base, "line": {"number": line_num, "content": line_content}}


# Per-(repo, commit, tag) context for the worker processes, set once per pool by _init_worker
# so only the (small) file path crosses the process boundary on each task.
_WORKER_CTX: dict = {}


def _init_worker(org: str, repo: str, commit_sha: str, repo_dir: str) -> None:
    # Ignore Ctrl-C in the worker processes. The terminal delivers SIGINT to the whole process
    # group, so without this every worker dumps its own KeyboardInterrupt traceback -- and, worse,
    # can be killed mid-task while holding an internal queue lock, which is what produced the
    # "leaked semaphore objects" warning on exit. The parent owns abort handling and tears the
    # pool down (see index_repo / runtime.handle_interrupts).
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _WORKER_CTX.update(
        org=org, repo=repo, commit_sha=commit_sha, repo_dir=pathlib.Path(repo_dir)
    )


def build_file_actions(rel_path: str) -> list[dict]:
    """Build the bulk actions for one file: its file-metadata doc plus a line doc per line of
    text. Runs in a worker process (see _init_worker for the shared context). Mirrors the old
    inline generator -- a binary file or one that can't be read yields only its file doc."""
    ctx = _WORKER_CTX
    org, repo, commit_sha = ctx["org"], ctx["repo"], ctx["commit_sha"]
    abs_path = ctx["repo_dir"] / rel_path
    f_index = files_index(org, repo)
    l_index = lines_index(org, repo)
    file_id, file_doc = build_file_doc(org, repo, commit_sha, rel_path, abs_path)
    actions = [{"_index": f_index, "_id": file_id, "_source": file_doc}]
    # Read the file's bytes once: detect binary from the first 8 KB, and (if text) decode the
    # same buffer for line splitting -- no second read of the file.
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return actions
    if b"\x00" in raw[:8192]:
        return actions  # binary: index file metadata only, no line docs
    content = raw.decode("utf-8", errors="surrogateescape")
    for line_id, line_doc in iter_line_docs(org, repo, commit_sha, rel_path, content):
        actions.append({"_index": l_index, "_id": line_id, "_source": line_doc})
    return actions


def index_repo(
    es: Elasticsearch,
    org: str,
    repo: str,
    repo_dir: pathlib.Path,
    commit_sha: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    files_count = 0
    lines_count = 0
    # Compute the per-repo files index name once so we can distinguish file vs line docs
    # in the response metadata without relying on a fixed constant.
    f_index = files_index(org, repo)

    # Generation (read + decode + per-line dict build + id hashing) is the throughput ceiling,
    # so fan it out across worker processes; each returns one file's actions, which we flatten
    # into the lazy stream parallel_bulk consumes. Doc ids are deterministic and independent
    # (make_doc_id), so file/line docs need not stay adjacent or ordered.
    t = _tuning()
    with ProcessPoolExecutor(
        max_workers=max(1, t.index_workers),
        initializer=_init_worker,
        initargs=(org, repo, commit_sha, str(repo_dir)),
    ) as executor:
        def generate_actions():
            for file_actions in executor.map(
                build_file_actions, iter_tracked_files(repo_dir), chunksize=t.index_worker_chunksize
            ):
                yield from file_actions

        # parallel_bulk is lazy -- it does no work until the returned generator is consumed.
        # raise_on_error/raise_on_exception default True, so a failed batch raises BulkIndexError
        # and aborts the run rather than silently dropping docs. Counts are tallied from each
        # result's index name (the producer no longer runs in this thread to count as it goes).
        processed = 0
        try:
            for _ok, info in es_parallel_bulk(
                es,
                generate_actions(),
                thread_count=t.bulk_threads,
                chunk_size=t.bulk_chunk_size,
                max_chunk_bytes=t.bulk_max_bytes,
                queue_size=t.bulk_queue_size,
            ):
                # Ctrl-C reaches this (main or batch worker) thread either as a raised
                # KeyboardInterrupt or, in a batch worker thread that never gets the signal,
                # only as this flag -- so poll it to stop indexing this ref promptly.
                if _aborted.is_set():
                    raise KeyboardInterrupt
                meta = next(iter(info.values())) if info else {}
                if meta.get("_index") == f_index:
                    files_count += 1
                else:
                    lines_count += 1
                processed += 1
                if on_progress is not None and processed % 1000 == 0:
                    on_progress(files_count, lines_count)
        except KeyboardInterrupt:
            # Cancel the file tasks still queued in the pool so the `with` block's join below
            # returns in seconds. executor.map queues ~every tracked file up front; a plain
            # shutdown(wait=True) would process all of them (tens of thousands on a large repo)
            # before quitting -- the lag that made Ctrl-C take minutes to take hold.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    if on_progress is not None:
        on_progress(files_count, lines_count)
    return files_count, lines_count


def build_file_actions_v2(
    org: str, repo: str, ref: str, repo_dir: pathlib.Path, rel_path: str,
) -> list[dict]:
    """Bulk actions for one v2 (incremental) path: its ref-addressed file doc plus a line doc
    per line of text. Paths absent from the checked-out tree are rejected -- they yield no
    actions -- so a stale diff entry can never create a phantom document (INV/Task 3). Binary
    files (NUL in the first 8 KB) or unreadable files get only their file doc, mirroring v1."""
    abs_path = repo_dir / rel_path
    # A tracked file may be a regular file or a symlink; a broken symlink still exists as a
    # tracked entry, so is_symlink() must be checked before exists() (which follows the link).
    if not (abs_path.is_symlink() or abs_path.exists()):
        return []
    f_index = files_index_v2(org, repo)
    l_index = lines_index_v2(org, repo)
    file_id, file_doc = build_file_doc_v2(org, repo, ref, rel_path, abs_path)
    actions = [{"_index": f_index, "_id": file_id, "_source": file_doc}]
    try:
        raw = abs_path.read_bytes()
    except OSError:
        return actions
    if b"\x00" in raw[:8192]:
        return actions  # binary: file metadata only, no line docs
    content = raw.decode("utf-8", errors="surrogateescape")
    for line_id, line_doc in iter_line_docs_v2(org, repo, ref, rel_path, content):
        actions.append({"_index": l_index, "_id": line_id, "_source": line_doc})
    return actions


def index_paths_v2(
    es: Elasticsearch,
    org: str,
    repo: str,
    repo_dir: pathlib.Path,
    ref: str,
    paths: Iterable[str],
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Index a supplied iterable of tracked paths into the v2 content indices for `ref`, rather
    than walking the whole repository. Used for both the changed-path update (10-20 files) and
    full branch reconciliation (all tracked files); the caller decides which paths to pass.
    Returns (files_count, lines_count) of the docs written.

    Doc generation is inline (not farmed to a process pool like v1's index_repo): the typical
    incremental batch is tiny, and a full rebuild still streams lazily into parallel_bulk, whose
    worker threads overlap the network round-trips. Deterministic ref-addressed ids mean a
    re-index overwrites in place, so this is safe to retry."""
    files_count = 0
    lines_count = 0
    f_index = files_index_v2(org, repo)
    t = _tuning()

    def generate_actions() -> Iterator[dict]:
        for rel_path in paths:
            yield from build_file_actions_v2(org, repo, ref, repo_dir, rel_path)

    processed = 0
    for _ok, info in es_parallel_bulk(
        es,
        generate_actions(),
        thread_count=t.bulk_threads,
        chunk_size=t.bulk_chunk_size,
        max_chunk_bytes=t.bulk_max_bytes,
        queue_size=t.bulk_queue_size,
    ):
        if _aborted.is_set():
            raise KeyboardInterrupt
        meta = next(iter(info.values())) if info else {}
        if meta.get("_index") == f_index:
            files_count += 1
        else:
            lines_count += 1
        processed += 1
        if on_progress is not None and processed % 1000 == 0:
            on_progress(files_count, lines_count)
    if on_progress is not None:
        on_progress(files_count, lines_count)
    return files_count, lines_count
