# sourcerer/commands/index/runtime.py
# Run-scoped runtime state shared across the index command's other modules: environment-derived
# tuning knobs, the Ctrl-C abort flag + SIGINT handler, and the bulk-indexing ES settings context
# manager. Kept separate from documents.py/command.py so both can depend on it without a cycle:
# documents.py polls `_aborted` and reads `_tuning()`, while command.py installs the SIGINT
# handler and the bulk settings around a whole run.

# Standard packages
import contextlib
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
    - index_repo_concurrency: (org, repo) clones processed concurrently in the batch path, so one
      repo's clone overlaps another's indexing. Low by default to avoid oversubscribing the cluster.
    - resolve_concurrency: concurrent `git ls-remote` calls during planning; kept below GitHub's
      effective limit to avoid rate-limiting that silently returns empty ref lists.
    """
    bulk_threads = int(os.environ.get("ELASTICSEARCH_BULK_THREADS", "8"))
    return SimpleNamespace(
        bulk_chunk_size=int(os.environ.get("ELASTICSEARCH_BULK_CHUNK_SIZE", "5000")),
        bulk_threads=bulk_threads,
        bulk_max_bytes=int(os.environ.get("ELASTICSEARCH_BULK_MAX_BYTES", str(10 * 1024 * 1024))),
        bulk_queue_size=int(os.environ.get("ELASTICSEARCH_BULK_QUEUE_SIZE", str(bulk_threads * 2))),
        index_workers=int(os.environ.get("ELASTICSEARCH_INDEX_WORKERS", str(os.cpu_count() or 4))),
        index_worker_chunksize=int(os.environ.get("ELASTICSEARCH_INDEX_WORKER_CHUNKSIZE", "8")),
        index_repo_concurrency=int(os.environ.get("INDEX_REPO_CONCURRENCY", "2")),
        resolve_concurrency=int(os.environ.get("RESOLVE_CONCURRENCY", "4")),
    )


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
