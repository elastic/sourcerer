# sourcerer/commands/index/runtime.py
# Run-scoped runtime state shared across the index command's other modules: environment-derived
# tuning knobs, the git subprocess timeout, the Ctrl-C abort flag + SIGINT handler, and the
# bulk-indexing ES settings context manager. Kept separate from documents.py/command.py so both
# can depend on it without a cycle: documents.py polls `_aborted` and reads `_tuning()`, git.py
# reads `git_timeout()`, while command.py installs the SIGINT handler and the bulk settings
# around a whole run.

# Standard packages
import contextlib
import datetime
import functools
import os
import signal
import threading
from collections.abc import Iterator
from types import SimpleNamespace

# Third-party packages
import click
from elasticsearch import ApiError, Elasticsearch, NotFoundError

# App packages
from ...indices import FILES_INDEX_PREFIX, LINES_INDEX_PREFIX

# Env-derived config (the `_tuning()` knobs below, and SOURCERER_CACHE_DIR in resolve_cache_root)
# is read at call time, not import, so it observes whatever `.env` cli.py's --env callback loaded.


@functools.lru_cache(maxsize=None)
def _tuning():
    """Bulk/index/resolve throughput knobs, read from the environment on first use.

    Deferred to first call (not import) so the values reflect whatever `.env` cli.py loaded
    for this invocation; cached so every caller in a run sees one stable set. All are
    env-overridable for tuning against a given cluster/RTT (lower them for small/Serverless
    clusters that return 429s). See `.env-reference` for the full descriptions.

    - bulk_*: parallel_bulk keeps `bulk_threads` requests in flight so the client isn't blocked
      on a single round-trip. Line docs are tiny/metadata-heavy (~400-500 B), so a larger chunk
      amortizes per-request overhead (default 5000 ~= 2.25 MB/request). `bulk_max_bytes` caps the
      body so a burst of long lines can't oversize a batch (whichever limit hits first flushes).
      `bulk_queue_size` buffers chunks ahead of the senders. In-flight memory ~=
      (bulk_threads + bulk_queue_size) * bulk_chunk_size * avg_doc_bytes (~54 MB at defaults).
    - index_*: document generation (read/decode/hash one doc per line) is CPU/IO work under the
      GIL, farmed out to `index_workers` processes, `index_worker_chunksize` paths per IPC hop.
    - index_ref_concurrency: max concurrent ref-index jobs in the batch path -- both how many
      repos are in flight at once AND, within one repo, how many of its refs check out into
      their own `git worktree` and index concurrently (see `git.ensure_worktree`). One number
      because a ref job either waits on another repo's clone or waits on a worktree slot within
      its own repo; either way it is a unit of the same budget. `effective_index_workers()` /
      `effective_bulk_threads()` divide `index_workers`/`bulk_threads` by this so raising it
      reallocates the existing CPU/connection budget across more concurrent jobs instead of
      multiplying it. Low by default to avoid oversubscribing the cluster.
    - resolve_concurrency: concurrent `git ls-remote` calls during planning; kept below GitHub's
      effective limit to avoid rate-limiting that silently returns empty ref lists. Also used
      for the `--dry-run` clone/resolve preview pool (report.py), which does no checkout or
      ingest and so is scoped like planning, not like indexing.
    """
    bulk_threads = int(os.environ.get("ELASTICSEARCH_BULK_THREADS", "8"))
    return SimpleNamespace(
        bulk_chunk_size=int(os.environ.get("ELASTICSEARCH_BULK_CHUNK_SIZE", "5000")),
        bulk_threads=bulk_threads,
        bulk_max_bytes=int(os.environ.get("ELASTICSEARCH_BULK_MAX_BYTES", str(10 * 1024 * 1024))),
        bulk_queue_size=int(os.environ.get("ELASTICSEARCH_BULK_QUEUE_SIZE", str(bulk_threads * 2))),
        index_workers=int(os.environ.get("ELASTICSEARCH_INDEX_WORKERS", str(os.cpu_count() or 4))),
        index_worker_chunksize=int(os.environ.get("ELASTICSEARCH_INDEX_WORKER_CHUNKSIZE", "8")),
        index_ref_concurrency=int(os.environ.get("INDEX_REF_CONCURRENCY", "2")),
        resolve_concurrency=int(os.environ.get("RESOLVE_CONCURRENCY", "4")),
    )


def effective_index_workers() -> int:
    """Per-ref-job worker-process budget: `index_workers` divided across up to
    `index_ref_concurrency` concurrently-running ref jobs, so raising the concurrency knob
    reallocates the existing CPU budget instead of multiplying the total worker-process count.
    At the default concurrency of 2 this reproduces exactly today's per-job sizing."""
    t = _tuning()
    return max(1, t.index_workers // max(1, t.index_ref_concurrency))


def effective_bulk_threads() -> int:
    """Per-ref-job bulk-sender budget: `bulk_threads` divided across up to
    `index_ref_concurrency` concurrently-running ref jobs, so raising the concurrency knob
    reallocates the existing connection budget instead of multiplying the total sender count.
    At the default concurrency of 2 this reproduces exactly today's per-job sizing."""
    t = _tuning()
    return max(1, t.bulk_threads // max(1, t.index_ref_concurrency))


# Wall-clock ceiling for a single `git` invocation. Without one, a git that stops to ask for
# credentials (a private repo the run can't read) blocks forever on an inherited stdin nobody
# will answer -- and in the persistent-cache path it does so while holding the repo's advisory
# lock, so later runs skip that repo too. git.py hardens the environment so prompting can't
# happen at all; this is the backstop for everything else that can wedge (a half-open
# connection, a hung credential helper, a promisor fetch that never finishes).
_DEFAULT_GIT_TIMEOUT = 30 * 60.0  # seconds; a full blobless clone of a large repo takes minutes
_METADATA_GIT_TIMEOUT_CAP = 120.0  # seconds; ls-remote is never legitimately slower than this
_git_timeout_override: float | None = None
_git_timeout_is_set = False


def set_git_timeout(timeout: datetime.timedelta) -> None:
    """Set this run's per-git-command timeout (from --git-timeout / SOURCERER_GIT_TIMEOUT).

    A zero or negative duration disables the timeout entirely. Called once per run before any
    git work starts; every git call in git.py runs on the main thread or a resolve/index worker
    *thread* of this process, so a plain module global is enough (the ProcessPoolExecutor
    workers in documents.py only build documents and never invoke git)."""
    global _git_timeout_override, _git_timeout_is_set
    seconds = timeout.total_seconds()
    _git_timeout_override = seconds if seconds > 0 else None
    _git_timeout_is_set = True


def git_timeout() -> float | None:
    """Seconds a single git command may run, or None for no limit.

    Falls back to SOURCERER_GIT_TIMEOUT and then to the built-in default when a run hasn't
    called set_git_timeout, so a library or test caller of git.py still gets a bounded command
    rather than the original indefinite hang."""
    if _git_timeout_is_set:
        return _git_timeout_override
    raw = os.environ.get("SOURCERER_GIT_TIMEOUT")
    if not raw:
        return _DEFAULT_GIT_TIMEOUT
    from ...config import parse_duration

    seconds = parse_duration(raw).total_seconds()
    return seconds if seconds > 0 else None


def git_metadata_timeout() -> float | None:
    """The timeout for metadata-only remote commands (`git ls-remote`), capped well below the
    full timeout. These are a couple of round-trips with no transfer, so a slow one is a broken
    one -- and they run in planning, where waiting out the full timeout would delay every repo's
    indexing behind one bad remote. An explicitly disabled timeout still disables this one."""
    full = git_timeout()
    return None if full is None else min(full, _METADATA_GIT_TIMEOUT_CAP)


# Set by the SIGINT handler the index commands install while they run (see handle_interrupts).
# The long-running loops poll it so a single Ctrl-C unwinds the whole run promptly: a
# ThreadPoolExecutor's worker threads never receive the signal themselves (Python delivers it
# only to the main thread), so without this shared flag a `--config` batch would keep indexing
# the in-flight repos to completion before the pool could join. The process-pool workers
# separately ignore SIGINT entirely (see documents._init_worker).
_aborted = threading.Event()


@contextlib.contextmanager
def handle_interrupts() -> Iterator[None]:
    """Install a SIGINT handler for the duration of a run that flags the abort *before* raising
    KeyboardInterrupt as usual. Flagging first is what lets the worker threads (and the
    process-pool feed loop) notice and stop within a second or two; otherwise only the main
    thread unwinds while the pools drain tens of thousands of already-queued tasks -- the lag
    that made Ctrl-C feel unresponsive and tempted a second (and third) press. The previous
    handler is restored on exit so importing this module never permanently alters the process's
    signal disposition."""
    _aborted.clear()
    previous = signal.getsignal(signal.SIGINT)

    def _on_sigint(signum, frame):
        _aborted.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def bulk_indexing_settings(es: Elasticsearch) -> Iterator[None]:
    """Disable refresh on the content indices for the duration of a bulk load, then restore
    the default. With refresh off, Elasticsearch isn't building a searchable segment after
    every batch, which is the single biggest index-side throughput win for bulk ingest.

    Best-effort: the put_settings calls are wrapped so a cluster that rejects the change
    (e.g. a stricter Serverless constraint or a missing index/permission) degrades to the
    default behaviour instead of aborting the run. `-1` is a valid refresh_interval on
    Elastic Cloud Serverless (which otherwise requires >= 5s). Freshly-indexed docs are not
    searchable until the restore (which resets to the default and triggers a refresh)."""
    # Wildcard patterns match all current and future per-repo/commit indices.
    # ignore_unavailable and allow_no_indices prevent errors on a fresh run before any
    # content indices exist yet (they're created on demand by the first write).
    indices = [f"{FILES_INDEX_PREFIX}*", f"{LINES_INDEX_PREFIX}*"]

    def _set(value: str | None) -> bool:
        try:
            es.indices.put_settings(
                index=indices,
                settings={"index": {"refresh_interval": value}},
                ignore_unavailable=True,
                allow_no_indices=True,
            )
            return True
        except (ApiError, NotFoundError) as e:
            click.echo(f"Note: could not adjust refresh_interval ({value!r}) for bulk load: {e}", err=True)
            return False

    disabled = _set("-1")
    try:
        yield
    finally:
        if disabled:
            _set(None)  # None resets refresh_interval to its default.
