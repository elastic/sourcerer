# sourcerer/commands/index/documents.py
# Turns a checked-out commit's tracked files into ES bulk actions and loads them: per-file/
# per-line doc construction (farmed out to worker processes, since generation is the throughput
# ceiling) and the parallel_bulk ingest loop that consumes them.

# Standard packages
import os
import pathlib
import signal
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor
from itertools import islice

# Third-party packages
from elasticsearch import Elasticsearch
from elasticsearch.helpers import parallel_bulk as es_parallel_bulk

# App packages
from ...indices import files_index, lines_index
from ...utils import build_ref_key, make_doc_id
from .git import get_symlink_paths, iter_tracked_files
from .runtime import _aborted, _tuning


def file_attributes(
    path: pathlib.Path, *, binary: bool = False, is_symlink: bool | None = None
) -> list[str]:
    attrs = []
    if is_symlink is None:
        is_symlink = path.is_symlink()
    if is_symlink:
        attrs.append("symlink")
    else:
        try:
            if path.stat().st_mode & 0o111:
                attrs.append("executable")
        except OSError:
            pass
    if binary:
        attrs.append("binary")
    return sorted(attrs)


def build_file_doc(
    host: str,
    org: str,
    repo: str,
    commit_sha: str,
    rel_path: str,
    abs_path: pathlib.Path,
    *,
    binary: bool = False,
    is_symlink: bool | None = None,
    target_path: str | None = None,
    target_size: int | None = None,
) -> tuple[str, dict]:
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    _is_symlink = abs_path.is_symlink() if is_symlink is None else is_symlink
    size = abs_path.lstat().st_size
    file_fields: dict = {
        "path": rel_path,
        "directory": directory,
        "name": p.name,
        "extension": extension,
        "size": size,
    }
    attrs = file_attributes(abs_path, binary=binary, is_symlink=_is_symlink)
    if attrs:
        file_fields["attributes"] = attrs
    if _is_symlink:
        if target_path is None:
            try:
                target_path = os.readlink(abs_path)
            except OSError:
                pass
        if target_path is not None:
            file_fields["target_path"] = target_path
        if target_size is None:
            try:
                target_size = abs_path.stat().st_size
            except OSError:
                pass
        if target_size is not None:
            file_fields["target_size"] = target_size
    doc = {
        "git": {
            "host": host,
            "org": org,
            "repo": repo,
            "commit": commit_sha,
            # Snapshot ref_key is the bare commit -- the content is addressed by commit, so the
            # commit itself is the stable join key (see build_ref_key for the incremental shape).
            "ref_key": commit_sha,
        },
        "file": file_fields,
    }
    # Content identity is (host, org, repo, commit, path): the same blob reached via any ref
    # (branch/tag/commit) collapses to one doc. Branch is intentionally absent -- it lives
    # only in the refs index (see markers.write_ref_marker) since a branch moves. host is folded
    # in so the same org/repo on two different hosts never collide.
    _id = make_doc_id(host, org, repo, commit_sha, rel_path)
    return _id, doc


def iter_line_docs(
    host: str,
    org: str,
    repo: str,
    commit_sha: str,
    rel_path: str,
    content: str,
    *,
    size: int | None = None,
    target_path: str | None = None,
    target_size: int | None = None,
    attributes: list[str] | None = None,
) -> Iterator[tuple[str, dict]]:
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    file_fields: dict = {
        "path": rel_path,
        "directory": directory,
        "name": p.name,
        "extension": extension,
    }
    if size is not None:
        file_fields["size"] = size
    if target_path is not None:
        file_fields["target_path"] = target_path
    if target_size is not None:
        file_fields["target_size"] = target_size
    if attributes is not None:
        file_fields["attributes"] = attributes
    base = {
        "git": {
            "host": host,
            "org": org,
            "repo": repo,
            "commit": commit_sha,
            "ref_key": commit_sha,
        },
        "file": file_fields,
    }
    for line_num, line_content in enumerate(content.splitlines(), start=1):
        _id = make_doc_id(host, org, repo, commit_sha, rel_path, str(line_num))
        yield _id, {**base, "line": {"number": line_num, "content": line_content}}


def build_incremental_file_doc(
    host: str,
    org: str,
    repo: str,
    ref: str,
    rel_path: str,
    abs_path: pathlib.Path,
    *,
    binary: bool = False,
    is_symlink: bool | None = None,
    target_path: str | None = None,
    target_size: int | None = None,
) -> tuple[str, dict]:
    """Ref-addressed (incremental) file doc: no `git.commit`; `git.ref_key` is the tilde-joined
    `build_ref_key(host, org, repo, ref)` and `_id` is stable across commits (derived from the
    branch name, not the commit), so a modified file's doc overwrites in place on the next
    HEAD advance rather than minting a new id."""
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    _is_symlink = abs_path.is_symlink() if is_symlink is None else is_symlink
    size = abs_path.lstat().st_size
    file_fields: dict = {
        "path": rel_path,
        "directory": directory,
        "name": p.name,
        "extension": extension,
        "size": size,
    }
    attrs = file_attributes(abs_path, binary=binary, is_symlink=_is_symlink)
    if attrs:
        file_fields["attributes"] = attrs
    if _is_symlink:
        if target_path is None:
            try:
                target_path = os.readlink(abs_path)
            except OSError:
                pass
        if target_path is not None:
            file_fields["target_path"] = target_path
        if target_size is None:
            try:
                target_size = abs_path.stat().st_size
            except OSError:
                pass
        if target_size is not None:
            file_fields["target_size"] = target_size
    doc = {
        "git": {
            "host": host,
            "org": org,
            "repo": repo,
            "ref": ref,
            "ref_key": build_ref_key(host, org, repo, ref),
        },
        "file": file_fields,
    }
    _id = make_doc_id(host, org, repo, "branch", ref, rel_path)
    return _id, doc


def iter_incremental_line_docs(
    host: str,
    org: str,
    repo: str,
    ref: str,
    rel_path: str,
    content: str,
    *,
    size: int | None = None,
    target_path: str | None = None,
    target_size: int | None = None,
    attributes: list[str] | None = None,
) -> Iterator[tuple[str, dict]]:
    """Ref-addressed (incremental) line docs -- same shape as `build_incremental_file_doc`."""
    p = pathlib.PurePosixPath(rel_path)
    directory = "" if str(p.parent) == "." else str(p.parent)
    extension = p.suffix.lstrip(".") or None
    file_fields: dict = {
        "path": rel_path,
        "directory": directory,
        "name": p.name,
        "extension": extension,
    }
    if size is not None:
        file_fields["size"] = size
    if target_path is not None:
        file_fields["target_path"] = target_path
    if target_size is not None:
        file_fields["target_size"] = target_size
    if attributes is not None:
        file_fields["attributes"] = attributes
    base = {
        "git": {
            "host": host,
            "org": org,
            "repo": repo,
            "ref": ref,
            "ref_key": build_ref_key(host, org, repo, ref),
        },
        "file": file_fields,
    }
    for line_num, line_content in enumerate(content.splitlines(), start=1):
        _id = make_doc_id(host, org, repo, "branch", ref, rel_path, str(line_num))
        yield _id, {**base, "line": {"number": line_num, "content": line_content}}


# Per-(repo, commit, tag) context for the worker processes, set once per pool by _init_worker
# so only the (small) file path crosses the process boundary on each task.
_WORKER_CTX: dict = {}


def _init_worker(
    host: str, org: str, repo: str, commit_sha: str, repo_dir: str, symlink_paths: frozenset[str] = frozenset(),
    index_level: str = "repo", index_suffix: str | None = None,
) -> None:
    # Ignore Ctrl-C in the worker processes. The terminal delivers SIGINT to the whole process
    # group, so without this every worker dumps its own KeyboardInterrupt traceback -- and, worse,
    # can be killed mid-task while holding an internal queue lock, which is what produced the
    # "leaked semaphore objects" warning on exit. The parent owns abort handling and tears the
    # pool down (see index_repo / runtime.handle_interrupts).
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _WORKER_CTX.update(
        host=host, org=org, repo=repo, commit_sha=commit_sha, repo_dir=pathlib.Path(repo_dir),
        symlink_paths=symlink_paths, index_level=index_level, index_suffix=index_suffix,
        mode="snapshot",
    )


def _init_worker_incremental(
    host: str, org: str, repo: str, ref: str, repo_dir: str, symlink_paths: frozenset[str] = frozenset(),
    index_level: str = "repo", index_suffix: str | None = None,
) -> None:
    """Same as `_init_worker`, but for the incremental (ref-addressed) path: `ref` replaces
    `commit_sha` and `mode` routes `_build_one_file_actions` to the incremental doc builders."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _WORKER_CTX.update(
        host=host, org=org, repo=repo, ref=ref, repo_dir=pathlib.Path(repo_dir),
        symlink_paths=symlink_paths, index_level=index_level, index_suffix=index_suffix,
        mode="incremental",
    )


def build_file_actions(rel_paths: list[str]) -> list[dict]:
    """Build the bulk actions for a batch of files: for each, its file-metadata doc plus a line
    doc per line of text. Runs in a worker process (see _init_worker for the shared context). A
    batch (rather than one path) is the unit crossing the process boundary -- it replaces the old
    executor.map(..., chunksize=...) IPC batching now that index_repo submits paths in windowed
    groups. A binary file or one that can't be read contributes only its file doc."""
    out: list[dict] = []
    for rel_path in rel_paths:
        out.extend(_build_one_file_actions(rel_path))
    return out


def _build_one_file_actions(rel_path: str) -> list[dict]:
    """Build the bulk actions for one file: its file-metadata doc plus a line doc per line of
    text. Runs in a worker process (see _init_worker for the shared context). Mirrors the old
    inline generator -- a binary file or one that can't be read yields only its file doc."""
    ctx = _WORKER_CTX
    incremental = ctx.get("mode", "snapshot") == "incremental"
    host, org, repo = ctx["host"], ctx["org"], ctx["repo"]
    commit_sha = ctx.get("ref") if incremental else ctx["commit_sha"]
    level, suffix = ctx.get("index_level", "repo"), ctx.get("index_suffix")
    abs_path = ctx["repo_dir"] / rel_path
    # Incremental content is ref-addressed (no commit-level index name); the physical index name
    # never includes the commit for this path since ref-keyed docs don't carry git.commit.
    f_index = files_index(host, org, repo, None if incremental else commit_sha, level, suffix)
    l_index = lines_index(host, org, repo, None if incremental else commit_sha, level, suffix)
    # Read the file's bytes once: detect binary from the first 8 KB, and (if text) decode the
    # same buffer for line splitting -- no second read of the file. Binary flag must be computed
    # before build_file_doc so it reaches file.attributes.
    try:
        raw = abs_path.read_bytes()
    except OSError:
        raw = None
    binary = raw is not None and b"\x00" in raw[:8192]
    # Use git object mode (120000) to detect symlinks regardless of core.symlinks. When
    # core.symlinks=false, git checks out symlinks as plain text files containing the target
    # path, so path.is_symlink() returns False even though git treats them as symlinks.
    is_git_symlink = rel_path in ctx.get("symlink_paths", frozenset())
    if is_git_symlink:
        if abs_path.is_symlink():
            # core.symlinks=true: actual filesystem symlink
            try:
                git_target_path: str | None = os.readlink(abs_path)
            except OSError:
                git_target_path = None
            try:
                git_target_size: int | None = abs_path.stat().st_size
            except OSError:
                git_target_size = None
        else:
            # core.symlinks=false: file content IS the target path (no trailing newline)
            git_target_path = raw.decode("utf-8", errors="surrogateescape") if raw is not None else None
            if git_target_path is not None:
                resolved = abs_path.parent / git_target_path
                try:
                    raw = resolved.read_bytes()
                    git_target_size = resolved.stat().st_size
                except OSError:
                    raw = None
                    git_target_size = None
            else:
                git_target_size = None
    else:
        git_target_path = None
        git_target_size = None
    doc_builder = build_incremental_file_doc if incremental else build_file_doc
    file_id, file_doc = doc_builder(
        host, org, repo, commit_sha, rel_path, abs_path, binary=binary,
        is_symlink=True if is_git_symlink else None,
        target_path=git_target_path,
        target_size=git_target_size,
    )
    actions = [{"_index": f_index, "_id": file_id, "_source": file_doc}]
    if raw is None or binary:
        return actions
    content = raw.decode("utf-8", errors="surrogateescape")
    ff = file_doc["file"]
    line_iter = iter_incremental_line_docs if incremental else iter_line_docs
    for line_id, line_doc in line_iter(
        host, org, repo, commit_sha, rel_path, content,
        size=ff["size"],
        target_path=ff.get("target_path"),
        target_size=ff.get("target_size"),
        attributes=ff.get("attributes"),
    ):
        actions.append({"_index": l_index, "_id": line_id, "_source": line_doc})
    return actions


def index_repo(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    repo_dir: pathlib.Path,
    commit_sha: str,
    on_progress: Callable[[int, int], None] | None = None,
    index_level: str = "repo",
    index_suffix: str | None = None,
) -> tuple[int, int]:
    files_count = 0
    lines_count = 0
    # Compute the files index name once (matching this source's index.level/suffix, and the
    # commit for commit-level names) so we can distinguish file vs line docs in the response
    # metadata without relying on a fixed constant.
    f_index = files_index(host, org, repo, commit_sha, index_level, index_suffix)

    # Generation (read + decode + per-line dict build + id hashing) is the throughput ceiling,
    # so fan it out across worker processes; each returns one batch's actions, which we flatten
    # into the lazy stream parallel_bulk consumes. Doc ids are deterministic and independent
    # (make_doc_id), so file/line docs need not stay adjacent or ordered.
    t = _tuning()
    symlink_paths = get_symlink_paths(repo_dir)
    with ProcessPoolExecutor(
        max_workers=max(1, t.index_workers),
        initializer=_init_worker,
        initargs=(host, org, repo, commit_sha, str(repo_dir), symlink_paths,
                  index_level, index_suffix),
    ) as executor:
        def _batched(paths: Iterator[str], n: int) -> Iterator[list[str]]:
            it = iter(paths)
            while batch := list(islice(it, n)):
                yield batch

        def generate_actions():
            # Bounded sliding window of in-flight futures: keep at most `max_inflight` batches
            # submitted-but-unconsumed so completed per-file action lists (each carrying every
            # line doc) cannot pile up in this process while parallel_bulk is slower than
            # generation -- the unbounded backlog that made peak memory scale with repo size.
            # Peak generation backlog is ~ max_inflight * index_worker_chunksize files of docs,
            # independent of repo size. The window derives from existing tuning (no new knob).
            batches = _batched(iter_tracked_files(repo_dir), t.index_worker_chunksize)
            max_inflight = max(1, t.index_workers) * 2
            inflight: deque = deque()
            for batch in islice(batches, max_inflight):
                inflight.append(executor.submit(build_file_actions, batch))
            while inflight:
                file_actions = inflight.popleft().result()  # blocks: backpressure on a slow sink
                for batch in islice(batches, 1):
                    inflight.append(executor.submit(build_file_actions, batch))
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
            # Cancel the batch tasks still queued in the pool so the `with` block's join below
            # returns in seconds. The windowed submit above keeps only ~max_inflight batches
            # queued (not every tracked file), so a plain shutdown(wait=True) would already be
            # quick -- but cancel_futures makes teardown immediate regardless of window size.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    if on_progress is not None:
        on_progress(files_count, lines_count)
    return files_count, lines_count


def index_incremental_paths(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    repo_dir: pathlib.Path,
    ref: str,
    rel_paths: list[str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    index_level: str = "repo",
    index_suffix: str | None = None,
) -> tuple[int, int]:
    """Index a set of paths for an incremental (ref-addressed) branch source.

    `rel_paths=None` walks the whole checked-out tree (first index / full rebuild, e.g. when a
    diff base is unavailable). A given `rel_paths` list indexes only those paths -- the delta
    indexer's changed/added set (see `commands/index/git.py:plan_changes`), which is what makes
    an incremental HEAD advance only touch the files git reports changed. Deletions for removed
    paths are the caller's responsibility (see `markers.delete_by_ref_key`) since they need no
    doc generation. Mirrors `index_repo`'s worker-pool ingest loop.
    """
    files_count = 0
    lines_count = 0
    f_index = files_index(host, org, repo, None, index_level, index_suffix)

    t = _tuning()
    symlink_paths = get_symlink_paths(repo_dir)
    paths = list(iter_tracked_files(repo_dir)) if rel_paths is None else list(rel_paths)
    with ProcessPoolExecutor(
        max_workers=max(1, t.index_workers),
        initializer=_init_worker_incremental,
        initargs=(host, org, repo, ref, str(repo_dir), symlink_paths, index_level, index_suffix),
    ) as executor:
        def _batched(items: Iterator[str], n: int) -> Iterator[list[str]]:
            it = iter(items)
            while batch := list(islice(it, n)):
                yield batch

        def generate_actions():
            batches = _batched(iter(paths), t.index_worker_chunksize)
            max_inflight = max(1, t.index_workers) * 2
            inflight: deque = deque()
            for batch in islice(batches, max_inflight):
                inflight.append(executor.submit(build_file_actions, batch))
            while inflight:
                file_actions = inflight.popleft().result()
                for batch in islice(batches, 1):
                    inflight.append(executor.submit(build_file_actions, batch))
                yield from file_actions

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
            executor.shutdown(wait=False, cancel_futures=True)
            raise
    if on_progress is not None:
        on_progress(files_count, lines_count)
    return files_count, lines_count
