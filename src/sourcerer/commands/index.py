# Standard packages
import contextlib
import datetime
import fcntl
import fnmatch
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

# Third-party packages
import click
import yaml
from dotenv import find_dotenv, load_dotenv
from elastic_transport import TransportError
from elasticsearch import ApiError, Elasticsearch, NotFoundError
from elasticsearch.helpers import BulkIndexError, bulk, scan
from elasticsearch.helpers import parallel_bulk as es_parallel_bulk

# App packages
from ..config import RepoConfig, load_config
from ..planner import Marker, content_delete_set, plan_repo
from ..progress import ProgressReporter, Unit, make_reporter
from ..utils import make_client, make_doc_id

# Resolve `.env` from the working directory, not this package's install location
# (see cli.py). Matters when sourcerer runs as an installed uv tool.
load_dotenv(find_dotenv(usecwd=True))

GITHUB_URL_TEMPLATE = "https://github.com/{org}/{repo}.git"
FILES_INDEX_PREFIX = "sourcerer-v1-files"
LINES_INDEX_PREFIX = "sourcerer-v1-lines"
REFS_INDEX = "sourcerer-v1-refs"


def files_index(org: str, repo: str) -> str:
    """Return the per-repo files index name, e.g. sourcerer-v1-files~elastic~elasticsearch."""
    return f"{FILES_INDEX_PREFIX}~{org.lower()}~{repo.lower()}"


def lines_index(org: str, repo: str, commit_sha: str) -> str:
    """Return the per-commit lines index name, e.g. sourcerer-v1-lines~elastic~elasticsearch~<sha>."""
    return f"{LINES_INDEX_PREFIX}~{org.lower()}~{repo.lower()}~{commit_sha.lower()}"


# Bulk indexing throughput knobs. parallel_bulk keeps BULK_THREAD_COUNT bulk requests in
# flight so the client isn't blocked on a single round-trip at a time. Line docs are tiny and
# metadata-heavy (~400-500 B each: repeated path/commit/org plus a short line of content),
# so a larger chunk amortizes per-request + round-trip overhead. At ~450 B/doc the default 5000
# is ~2.25 MB/request -- toward Elasticsearch's 5-15 MB bulk-size guidance while staying well
# under the byte cap. BULK_MAX_BYTES caps the request body so a burst of very long lines (or
# docs larger than estimated) can't produce an oversized batch -- whichever limit hits first
# flushes, so the chunk count is only an upper bound, never a forced byte size. BULK_QUEUE_SIZE
# buffers chunks ahead of the senders so the (parallel) document producer isn't blocked waiting
# for a free thread. In-flight client memory scales as roughly
# (BULK_THREAD_COUNT + BULK_QUEUE_SIZE) * BULK_CHUNK_SIZE * avg_doc_bytes (~54 MB at the
# defaults); raising ELASTICSEARCH_BULK_CHUNK_SIZE toward 10000 on a capable cluster roughly doubles that.
# All env-overridable for tuning against a given cluster/RTT (lower it for small/Serverless
# clusters that return 429s).
BULK_CHUNK_SIZE = int(os.environ.get("ELASTICSEARCH_BULK_CHUNK_SIZE", "5000"))
BULK_THREAD_COUNT = int(os.environ.get("ELASTICSEARCH_BULK_THREADS", "8"))
BULK_MAX_BYTES = int(os.environ.get("ELASTICSEARCH_BULK_MAX_BYTES", str(10 * 1024 * 1024)))
BULK_QUEUE_SIZE = int(os.environ.get("ELASTICSEARCH_BULK_QUEUE_SIZE", str(BULK_THREAD_COUNT * 2)))

# Document generation is the real throughput ceiling: reading each file, decoding it, and
# building+hashing one doc per line is CPU/IO work that runs under the GIL, so it is farmed
# out to a pool of worker processes (true parallelism) that feed the bulk senders.
# INDEX_WORKERS processes, INDEX_WORKER_CHUNKSIZE file paths dispatched per IPC round-trip.
INDEX_WORKERS = int(os.environ.get("ELASTICSEARCH_INDEX_WORKERS", str(os.cpu_count() or 4)))
INDEX_WORKER_CHUNKSIZE = int(os.environ.get("ELASTICSEARCH_INDEX_WORKER_CHUNKSIZE", "8"))

# How many (org, repo) clones to process concurrently in the config-driven batch path, so one
# repo's clone (network/disk, releases the GIL) overlaps another's indexing. Kept low by
# default so concurrent repos don't oversubscribe the cluster or stack too many worker pools.
INDEX_REPO_CONCURRENCY = int(os.environ.get("INDEX_REPO_CONCURRENCY", "2"))

# The planning phase fires two `git ls-remote` calls per config entry to list its branches and
# tags. They are independent, light, GIL-releasing network round-trips, so they resolve at
# higher concurrency than the indexing phase.
RESOLVE_CONCURRENCY = int(os.environ.get("RESOLVE_CONCURRENCY", "16"))

# Persistent clone cache. By default the index command keeps each repo cloned under a stable
# cache dir and `git fetch`es it on later runs instead of re-cloning -- a regularly-scheduled
# (e.g. nightly) run then transfers only the day's new objects rather than a full clone of a
# large repo's history. Resolution precedence is --cache-dir flag > SOURCERER_CACHE_DIR env >
# $XDG_CACHE_HOME/sourcerer > ~/.cache/sourcerer.
SOURCERER_CACHE_DIR = os.environ.get("SOURCERER_CACHE_DIR")

# Remote/transient Elasticsearch failures (read timeouts, dropped connections, API errors,
# per-doc bulk failures) that should fail only the current ref -- so a long batch keeps going
# and reports that one ref as an error -- rather than crashing the whole run.
ES_ERRORS = (ApiError, TransportError, BulkIndexError)

# Set by the SIGINT handler the index commands install while they run (see handle_interrupts).
# The long-running loops poll it so a single Ctrl-C unwinds the whole run promptly: a
# ThreadPoolExecutor's worker threads never receive the signal themselves (Python delivers it
# only to the main thread), so without this shared flag a `--config` batch would keep indexing
# the in-flight repos to completion before the pool could join. The process-pool workers
# separately ignore SIGINT entirely (see _init_worker).
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


def resolve_cache_root(cache_dir: str | None = None) -> pathlib.Path:
    """Resolve the cache root, precedence: --cache-dir > SOURCERER_CACHE_DIR > $XDG_CACHE_HOME/
    sourcerer > ~/.cache/sourcerer. Returns the path; callers create the per-repo subdir lazily."""
    if cache_dir:
        return pathlib.Path(cache_dir).expanduser()
    if SOURCERER_CACHE_DIR:
        return pathlib.Path(SOURCERER_CACHE_DIR).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = pathlib.Path(xdg).expanduser() if xdg else pathlib.Path.home() / ".cache"
    return base / "sourcerer"


def repo_cache_dir(cache_root: pathlib.Path, org: str, repo: str) -> pathlib.Path:
    """The stable per-repo clone path under the cache root: <root>/repos/<org>/<repo>."""
    return cache_root / "repos" / org / repo


@contextlib.contextmanager
def repo_lock(repo_dir: pathlib.Path) -> Iterator[bool]:
    """Take a non-blocking advisory lock for a persistent repo dir so two overlapping runs
    (e.g. a nightly cron that overruns into the next) can't fetch/checkout the same clone at
    once and corrupt it. Yields True if the lock was acquired, False if another process holds
    it (the caller should then skip this repo rather than block). The lock file lives beside
    the clone so distinct repos never contend."""
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = repo_dir.parent / f"{repo_dir.name}.sourcerer.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _git_clone(url: str, repo_dir: pathlib.Path) -> None:
    subprocess.run(
        ["git", "clone", url, str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )


def _is_clone_of(repo_dir: pathlib.Path, url: str) -> bool:
    """True if `repo_dir` is a valid git working clone whose `origin` points at `url`."""
    if not (repo_dir / ".git").exists():
        return False
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return out.stdout.strip() == url


@contextlib.contextmanager
def clone_repo(
    org: str,
    repo: str,
    repo_dir: pathlib.Path | None = None,
    ephemeral: bool = False,
) -> Iterator[pathlib.Path]:
    """Make a repo available on disk and yield its path. A full clone fetches all branches and
    tags, so the caller can `checkout_ref` any number of refs against this one clone.

    ephemeral (or no repo_dir): full-clone into a temp dir and delete it on exit -- the original
    throwaway behaviour, for one-off/CI runs.

    persistent (repo_dir given, not ephemeral): keep the clone at the stable `repo_dir`. If it is
    already a valid clone of this repo, `git fetch` only the new objects (so a scheduled run
    transfers a day's commits, not the whole history); otherwise clone fresh. A fetch failure
    falls back to wipe + re-clone once (recovering a corrupt cache) before giving up. The dir is
    NOT deleted on exit. HEAD is left wherever the fetch/clone leaves it; callers check out what
    they need."""
    url = GITHUB_URL_TEMPLATE.format(org=org, repo=repo)
    if ephemeral or repo_dir is None:
        with tempfile.TemporaryDirectory(prefix="sourcerer-") as tmp:
            tmp_dir = pathlib.Path(tmp) / repo
            _git_clone(url, tmp_dir)
            yield tmp_dir
        return

    if _is_clone_of(repo_dir, url):
        try:
            subprocess.run(
                ["git", "-C", str(repo_dir), "fetch", "--prune", "--prune-tags", "--tags", "origin"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            # Fetch failed (e.g. a corrupt object store) -- wipe and re-clone once.
            shutil.rmtree(repo_dir, ignore_errors=True)
            _git_clone(url, repo_dir)
    else:
        # Missing, not a repo, or pointing at a different remote -- start clean.
        shutil.rmtree(repo_dir, ignore_errors=True)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        _git_clone(url, repo_dir)
    yield repo_dir


@contextlib.contextmanager
def prepared_repo(
    org: str,
    repo: str,
    cache_root: pathlib.Path | None,
    ephemeral: bool,
) -> Iterator[pathlib.Path | None]:
    """Yield a checked-out-ready repo dir, holding it for the whole `with` body so the caller can
    index any number of refs against one clone. Ephemeral (or no cache_root): a throwaway temp
    clone. Persistent: take a per-repo advisory lock, then clone-or-fetch the stable cache dir.
    Yields None if the persistent clone is locked by another run -- the caller should then skip
    this repo (the lock is released as soon as this manager exits)."""
    if ephemeral or cache_root is None:
        with clone_repo(org, repo, ephemeral=True) as repo_dir:
            yield repo_dir
        return
    repo_dir = repo_cache_dir(cache_root, org, repo)
    with repo_lock(repo_dir) as locked:
        if not locked:
            yield None
            return
        with clone_repo(org, repo, repo_dir, ephemeral=False) as ready:
            yield ready


def checkout_ref(repo_dir: pathlib.Path, ref: str) -> None:
    """Check out an immutable `ref` (a tag or commit SHA) into an existing clone. `--force`
    discards any working-tree state left by a previous ref's checkout so it can't bleed into
    the next index pass. Branches go through `checkout_branch` instead, which targets the
    fetched remote tip rather than a (possibly stale) local branch."""
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "--force", ref],
        check=True,
        capture_output=True,
        text=True,
    )


def checkout_branch(repo_dir: pathlib.Path, branch: str) -> None:
    """Check out a branch at its fetched remote tip. `-B <branch> origin/<branch>` resets the
    local branch to the freshly-fetched `origin/<branch>` -- on a reused (persistent) clone a
    plain `git checkout <branch>` would land on the *stale* local branch, since `git fetch`
    advances `origin/<branch>` but not the local branch. `--force` discards leftover working-tree
    state. Works the same on a fresh clone (origin/<branch> already exists)."""
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", "--force", "-B", branch, f"origin/{branch}"],
        check=True,
        capture_output=True,
        text=True,
    )


def ref_dates(repo_dir: pathlib.Path) -> dict[tuple[str, str], int]:
    """Return a mapping of (kind, short_name) -> Unix timestamp for every branch and tag in the
    clone, using `git for-each-ref --sort=-creatordate`. `creatordate` resolves to the tagger
    date for annotated tags and the committer date for lightweight tags and branches, so it gives
    meaningful recency regardless of whether refs are semver-tagged or not.

    Returns {} on any failure so callers degrade gracefully to their existing order."""
    try:
        result = subprocess.run(
            [
                "git", "-C", str(repo_dir), "for-each-ref",
                "--format=%(refname) %(creatordate:unix)",
                "refs/heads", "refs/tags",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return {}
    dates: dict[tuple[str, str], int] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        refname, ts = parts
        try:
            unix_ts = int(ts)
        except ValueError:
            continue
        if refname.startswith("refs/heads/"):
            dates[("branch", refname[len("refs/heads/"):])] = unix_ts
        elif refname.startswith("refs/tags/"):
            dates[("tag", refname[len("refs/tags/"):])] = unix_ts
    return dates


def default_branch(repo_dir: pathlib.Path) -> str:
    """The remote's default branch name (e.g. "main"), read from the `origin/HEAD` symbolic ref
    that both clone and fetch maintain. Used when no explicit ref is requested."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    # e.g. "origin/main" -> "main"
    return result.stdout.strip().split("/", 1)[1]


def resolve_commit(repo_dir: pathlib.Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def iter_tracked_files(repo_dir: pathlib.Path) -> Iterator[str]:
    proc = subprocess.Popen(
        ["git", "-C", str(repo_dir), "ls-files", "-z"],
        stdout=subprocess.PIPE,
    )
    try:
        buf = b""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            *complete, buf = buf.split(b"\0")
            for entry in complete:
                if entry:
                    yield entry.decode("utf-8", errors="surrogateescape")
        if buf:
            yield buf.decode("utf-8", errors="surrogateescape")
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, "git ls-files")
    finally:
        if proc.poll() is None:
            proc.kill()


def count_tracked_files(repo_dir: pathlib.Path) -> int:
    """Count git-tracked files via a quick `git ls-files` pass, so per-ref progress
    has a real total before indexing begins. Reuses iter_tracked_files."""
    return sum(1 for _ in iter_tracked_files(repo_dir))


def _ls_remote(url: str, *patterns: str, flags: tuple[str, ...] = ()) -> str | None:
    """
    Run `git ls-remote` against a remote without cloning. The URL must precede the ref
    patterns (HEAD/refs/...), so flags go before the URL and patterns after it. Returns
    stdout, or None on failure.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", *flags, url, *patterns],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout


def resolve_remote(
    org: str, repo: str, branch: str | None, tag: str | None
) -> tuple[str | None, str | None]:
    """
    Cheaply resolve a ref to its commit SHA via `git ls-remote` (no clone).

    Returns (commit_sha, default_branch). default_branch is only set when neither
    branch nor tag is given (resolving the remote HEAD). Returns (None, None) on any
    failure so callers fall through to cloning.
    """
    url = GITHUB_URL_TEMPLATE.format(org=org, repo=repo)
    if tag:
        # Prefer the peeled (^{}) line so annotated tags resolve to the underlying
        # commit, matching `git rev-parse HEAD` after checkout.
        out = _ls_remote(url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}", flags=("--tags",))
        if not out:
            return None, None
        sha = None
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 2:
                continue
            obj, name = parts
            if name == f"refs/tags/{tag}^{{}}":
                return obj, None
            if name == f"refs/tags/{tag}":
                sha = obj
        return sha, None
    if branch:
        out = _ls_remote(url, f"refs/heads/{branch}")
        if not out:
            return None, None
        line = out.splitlines()[0] if out.splitlines() else ""
        parts = line.split("\t")
        return (parts[0], None) if len(parts) == 2 else (None, None)
    # No ref: resolve the default branch name and its SHA from the symbolic HEAD.
    out = _ls_remote(url, "HEAD", flags=("--symref",))
    if not out:
        return None, None
    default_branch = None
    sha = None
    for line in out.splitlines():
        if line.startswith("ref:"):
            # e.g. "ref: refs/heads/main\tHEAD"
            target = line[len("ref:") :].split("\t")[0].strip()
            if target.startswith("refs/heads/"):
                default_branch = target[len("refs/heads/") :]
        else:
            parts = line.split("\t")
            if len(parts) == 2 and parts[1] == "HEAD":
                sha = parts[0]
    return sha, default_branch


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
    # only in the refs index (see write_ref_marker) since a branch moves.
    _id = make_doc_id(org, repo, commit_sha, rel_path)
    return _id, doc


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
    # pool down (see index_repo / handle_interrupts).
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
    l_index = lines_index(org, repo, commit_sha)
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
    with ProcessPoolExecutor(
        max_workers=max(1, INDEX_WORKERS),
        initializer=_init_worker,
        initargs=(org, repo, commit_sha, str(repo_dir)),
    ) as executor:
        def generate_actions():
            for file_actions in executor.map(
                build_file_actions, iter_tracked_files(repo_dir), chunksize=INDEX_WORKER_CHUNKSIZE
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
                thread_count=BULK_THREAD_COUNT,
                chunk_size=BULK_CHUNK_SIZE,
                max_chunk_bytes=BULK_MAX_BYTES,
                queue_size=BULK_QUEUE_SIZE,
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


def force_merge_lines_index(es: Elasticsearch, org: str, repo: str, commit_sha: str) -> None:
    """Kick off a force-merge of a commit's lines index down to a single segment, once all of
    that commit's line docs have been written. The lines index is write-once per commit (content
    is keyed by (org, repo, commit, path) and never rewritten for that commit), so merging to one
    segment after the load trades a one-off merge cost for smaller, faster-to-search segments.

    Fired with wait_for_completion=False so it runs as a background task on the cluster and the
    indexing run doesn't block on the (potentially long) merge. Best-effort: a cluster that
    rejects or can't schedule the merge just keeps its default segmenting rather than failing
    the ref."""
    index = lines_index(org, repo, commit_sha)
    try:
        es.indices.forcemerge(index=index, max_num_segments=1, wait_for_completion=False)
    except ES_ERRORS as e:
        click.echo(f"Note: could not force-merge {index}: {e}", err=True)


def commit_date(repo_dir: pathlib.Path) -> str | None:
    """Strict ISO-8601 committer date of the checked-out HEAD (git %cI), or None on failure.

    The "age of the code" clock: used for max_age pruning and for head-ordering in
    resolve_head. Distinct from indexed_at (rebuild recency, used by keep_recent) -- a tag
    cut three years ago is old regardless of when it was indexed."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "show", "-s", "--format=%cI", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout.strip() or None


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
    Cheap pre-clone decision via `git ls-remote` (no clone). Returns
    (skip, ref_for_id, remote_sha):
      - skip=True  -> the ref is already fully indexed; the caller should finish "skipped"
        (using the returned ref_for_id) without cloning.
      - skip=False -> the caller must clone/checkout and run the post-clone path.

    Not possible for -c (can't resolve a short hash remotely) or under --force; both
    bypass the check and fall through to cloning. An ls-remote failure also falls through.
    """
    if force or commit:
        return False, None, None
    remote_sha, default_branch = resolve_remote(org, repo, branch, tag)
    if not remote_sha:
        return False, None, None
    ref_type = "tag" if tag else "branch"  # branch, or the resolved remote HEAD (a branch)
    ref_for_id = branch or tag or default_branch
    if ref_for_id and not should_index(es, org, repo, ref_type, ref_for_id, remote_sha):
        return True, ref_for_id, remote_sha
    return False, ref_for_id, remote_sha


def index_ref_in_dir(
    es: Elasticsearch,
    org: str,
    repo: str,
    repo_dir: pathlib.Path,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
) -> None:
    """
    Index a single ref of one repo into an already-cloned `repo_dir`. Checks out the ref
    (a full clone holds every branch/tag, so one clone serves any number of refs), then
    runs the authoritative SHA guard and indexes, tags, or records the ref. At most one of
    branch/tag/commit should be set; none means the remote's default branch.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
        unit = Unit(org=org, repo=repo, ref=branch or tag or commit, kind=kind)

    # The repo is already cloned/fetched (callers clone once, then reuse this dir for every ref);
    # this only checks out the ref, so the stage is "checkout", not "cloning". Branches (and the
    # default branch) are checked out at their fetched remote tip so a reused clone can't land on
    # a stale local branch; tags/commits are immutable and checked out directly.
    reporter.set_stage(unit, "checkout")
    if branch:
        checkout_branch(repo_dir, branch)
    elif tag or commit:
        checkout_ref(repo_dir, tag or commit)
    else:
        branch = default_branch(repo_dir)
        checkout_branch(repo_dir, branch)

    commit_sha = resolve_commit(repo_dir)
    if branch:
        ref_for_id = branch
    elif tag:
        ref_for_id = tag
    else:  # commit
        ref_for_id = commit_sha
    ref_type = "branch" if branch else "tag" if tag else "commit"
    commit_date_iso = commit_date(repo_dir)
    unit.ref = ref_for_id

    # Post-clone guard: authoritative SHA check (covers -c and any ls-remote
    # peeling mismatch).
    if not force and not should_index(es, org, repo, ref_type, ref_for_id, commit_sha):
        reporter.finish(unit, "skipped")
        return

    if not force and commit_fully_indexed(es, org, repo, commit_sha):
        # Content is keyed by commit, so another ref already fully indexed this exact
        # snapshot. Don't rewrite the (large) content docs -- just record the ref marker.
        # "Fully" is load-bearing: an aborted run leaves partial content but no complete
        # marker, so it falls through to the else branch and re-indexes rather than
        # recording a half-populated commit as done.
        status = "tagged" if tag else "recorded"
        files_count = count_commit_docs(es, files_index(org, repo), org, repo, commit_sha)
        lines_count = count_commit_docs(es, lines_index(org, repo, commit_sha), org, repo, commit_sha)
    else:
        reporter.set_total_files(unit, count_tracked_files(repo_dir))
        reporter.set_stage(unit, "indexing")
        files_count, lines_count = index_repo(
            es, org, repo, repo_dir, commit_sha,
            on_progress=lambda f, l: reporter.update_counts(unit, f, l),
        )
        # All of this commit's line docs are now written; merge its lines index to a single
        # segment in the background (see force_merge_lines_index). Only on the freshly-indexed
        # path -- the tagged/recorded branch means another ref already indexed (and merged) this
        # same commit's content.
        force_merge_lines_index(es, org, repo, commit_sha)
        status = "indexed"
    write_ref_marker(es, org, repo, ref_type, ref_for_id, commit_sha, commit_date_iso, files_count, lines_count)
    reporter.finish(unit, status, files_count, lines_count)


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


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_markers(
    es: Elasticsearch, org: str, repo: str,
    ref_type: str | None = None, ref: str | None = None,
) -> list[Marker]:
    """Every ref marker for a repo (optionally narrowed to one ref), read via scan. Returns
    [] if the refs index does not exist yet. Shared by `prune` and the inline head-only GC."""
    filters = [{"term": {"git.org": org}}, {"term": {"git.repo": repo}}]
    if ref_type is not None:
        filters.append({"term": {"git.ref_type": ref_type}})
    if ref is not None:
        filters.append({"term": {"git.ref": ref}})
    body = {"query": {"bool": {"filter": filters}}}
    out: list[Marker] = []
    try:
        for hit in scan(es, index=REFS_INDEX, query=body, preserve_order=False):
            src = hit["_source"]
            g = src.get("git", {})
            out.append(Marker(
                id=hit["_id"],
                ref=g.get("ref"),
                ref_type=g.get("ref_type"),
                commit=g.get("commit"),
                commit_date=_parse_dt(g.get("commit_date")),
                indexed_at=_parse_dt(src.get("indexed_at")),
            ))
    except NotFoundError:
        return []
    return out


def execute_deletions(
    es: Elasticsearch, org: str, repo: str, decisions, force_merge: bool = True,
) -> tuple[int, int]:
    """Apply a prune plan. Deletes the marker docs, then drops content ONLY for commits that
    no surviving marker references (the commit-safety guard, in content_delete_set): the
    per-commit lines index is dropped whole, and the shared per-repo files index is pruned by
    a `git.commit` delete-by-query. Returns (markers_deleted, commits_content_dropped)."""
    deletes = [d.marker for d in decisions if d.action == "delete"]
    if not deletes:
        return (0, 0)
    drop_commits = content_delete_set(decisions)

    bulk(
        es,
        ({"_op_type": "delete", "_index": REFS_INDEX, "_id": m.id} for m in deletes),
        raise_on_error=False,
        refresh=False,
    )

    for sha in drop_commits:
        try:
            es.indices.delete(index=lines_index(org, repo, sha), ignore_unavailable=True)
        except NotFoundError:
            pass
        try:
            es.delete_by_query(
                index=files_index(org, repo),
                query={"bool": {"filter": [
                    {"term": {"git.org": org}},
                    {"term": {"git.repo": repo}},
                    {"term": {"git.commit": sha}},
                ]}},
                conflicts="proceed",
                refresh=False,
            )
        except NotFoundError:
            pass
    if force_merge and drop_commits:
        # Reclaim the deleted file docs from the shared files index. Best-effort: the marker
        # and content deletes are already applied even if the merge can't be scheduled.
        try:
            es.indices.forcemerge(index=files_index(org, repo), only_expunge_deletes=True)
        except (NotFoundError, *ES_ERRORS):
            pass
    return (len(deletes), len(drop_commits))


def index_one(
    es: Elasticsearch,
    org: str,
    repo: str,
    branch: str | None = None,
    tag: str | None = None,
    commit: str | None = None,
    force: bool = False,
    reporter: ProgressReporter | None = None,
    unit: Unit | None = None,
    cache_root: pathlib.Path | None = None,
    ephemeral: bool = False,
) -> None:
    """
    Index a single ref (branch, tag, or commit) of one repo into an existing ES client.
    At most one of branch/tag/commit should be set; none means the remote's default branch.
    Used by the single-repo CLI path (`run`); the config-driven batch path (`run_config`)
    clones once per repo and calls `index_ref_in_dir` directly for each ref.

    With `cache_root` set and `ephemeral` false, the clone is kept under the cache dir and
    fetched on later runs; otherwise it is a throwaway temp clone.

    Progress is reported through `reporter`/`unit`; when omitted, a quiet no-op reporter
    and a fresh unit are used so the function is still callable standalone.
    """
    if reporter is None:
        reporter = ProgressReporter()
    if unit is None:
        kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
        unit = Unit(org=org, repo=repo, ref=branch or tag or commit, kind=kind)

    reporter.start(unit)

    # Pre-clone skip: if the ref is already fully indexed, skip before paying the clone cost.
    skip, ref_for_id, _ = pre_clone_skip(es, org, repo, branch, tag, commit, force)
    if skip:
        unit.ref = ref_for_id
        reporter.finish(unit, "skipped")
        return

    reporter.set_stage(unit, "cloning")
    with prepared_repo(org, repo, cache_root, ephemeral) as repo_dir:
        if repo_dir is None:
            reporter.finish(unit, "locked", detail="another sourcerer run holds this repo's cache lock")
            return
        index_ref_in_dir(es, org, repo, repo_dir, branch, tag, commit, force, reporter, unit)


def run(
    repo_spec: str,
    branch: str | None,
    tag: str | None,
    commit: str | None,
    url: str,
    api_key: str | None,
    username: str | None,
    password: str | None,
    force: bool = False,
    quiet: bool = False,
    cache_dir: str | None = None,
    ephemeral: bool = False,
) -> None:
    parts = repo_spec.split("/", 1)
    if len(parts) != 2 or not all(parts):
        click.echo(f"Error: repo_spec must be '<org>/<repo>', got: {repo_spec!r}", err=True)
        sys.exit(1)
    org, repo = parts

    refs = {k: v for k, v in [("branch", branch), ("tag", tag), ("commit", commit)] if v}
    if len(refs) > 1:
        click.echo("Error: specify at most one of -b/--branch, -t/--tag, -c/--commit", err=True)
        sys.exit(1)

    es = make_client(url, api_key, username, password)
    cache_root = None if ephemeral else resolve_cache_root(cache_dir)

    kind = "branch" if branch else "tag" if tag else "commit" if commit else "default"
    unit = Unit(org=org, repo=repo, ref=branch or tag or commit, kind=kind)
    reporter = make_reporter(quiet)
    with handle_interrupts(), reporter, bulk_indexing_settings(es):
        reporter.set_plan([unit])
        try:
            index_one(
                es, org, repo, branch, tag, commit, force,
                reporter=reporter, unit=unit, cache_root=cache_root, ephemeral=ephemeral,
            )
        except FileNotFoundError as e:
            reporter.finish(unit, "error", detail=str(e))
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            reporter.finish(unit, "error", detail=f"git command failed (exit {e.returncode}): {e.stderr or ''}")
            sys.exit(1)
        except ValueError as e:
            reporter.finish(unit, "error", detail=str(e))
            sys.exit(1)
        except ES_ERRORS as e:
            reporter.finish(unit, "error", detail=f"Elasticsearch request failed: {e}")
            sys.exit(1)


def list_remote_ref_names(org: str, repo: str, kind: str) -> list[str]:
    """
    List the short names of a remote's refs of `kind` ("heads" or "tags") via `git ls-remote`,
    without cloning. Strips the `refs/<kind>/` prefix and the peeled `^{}` suffix (annotated
    tags appear twice), dedupes, and returns them sorted. Returns [] on any failure.
    """
    url = GITHUB_URL_TEMPLATE.format(org=org, repo=repo)
    out = _ls_remote(url, flags=(f"--{kind}",))
    if not out:
        return []
    prefix = f"refs/{kind}/"
    names = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name = parts[1]
        if not name.startswith(prefix):
            continue
        name = name[len(prefix) :]
        if name.endswith("^{}"):
            name = name[: -len("^{}")]
        names.add(name)
    return sorted(names)


def select_refs(names: list[str], patterns: list[str]) -> list[str]:
    """Return the names matching any of the fnmatch glob patterns, preserving `names` order."""
    return [n for n in names if any(fnmatch.fnmatch(n, p) for p in patterns)]


def _resolve_entry(cfg: RepoConfig) -> list[Unit]:
    """Resolve one RepoConfig into the Units it selects: list the remote branches/tags once
    per kind and keep those matching a selector's version-aware pattern (+ min/max_version).
    Pure network + filtering with no reporter calls, so it is safe to run concurrently.
    list_remote_ref_names returns [] on any ls-remote failure, so this never raises -- a
    bad/unreachable repo simply contributes no refs."""
    remote_names: dict[str, list[str] | None] = {"branch": None, "tag": None}
    seen: set[tuple[str, str]] = set()
    units: list[Unit] = []
    for sel in cfg.selectors:
        rt = sel.ref_type
        if remote_names[rt] is None:
            remote_names[rt] = list_remote_ref_names(
                cfg.org, cfg.repo, "heads" if rt == "branch" else "tags"
            )
        floor = sel.since_version_floor()  # version-based `since: {ref}`, name-only
        for name in remote_names[rt]:
            if (rt, name) in seen:
                continue
            v = sel.matches(rt, name)
            if v is None:
                continue
            if floor is not None and v.components < floor:
                continue  # below the since version floor
            seen.add((rt, name))
            units.append(Unit(org=cfg.org, repo=cfg.repo, ref=name, kind=rt))

    # Drop refs that retain would delete on version/prerelease grounds alone. These are
    # name-only (no commit dates), so applying them here shrinks the plan itself -- the user
    # sees the true set, and we skip the clone/skip-check churn on doomed refs. count/age/since
    # are date-based and refined post-clone (see process_group). Uses the same planner as
    # `sourcerer prune`, so cross-selector keeps (e.g. a keep-forever selector) still win.
    needs_filter = any(
        sel.retain is not None
        and (sel.retain.version is not None or sel.retain.prerelease == "superseded")
        for sel in cfg.selectors
    )
    if needs_filter and units:
        markers = [
            Marker(id=f"{u.kind}:{u.ref}", ref=u.ref, ref_type=u.kind,
                   commit="", commit_date=None, indexed_at=None)
            for u in units
        ]
        doomed = {
            d.marker.id for d in plan_repo(markers, cfg, date_independent_only=True)
            if d.action == "delete"
        }
        units = [u for u in units if f"{u.kind}:{u.ref}" not in doomed]
    return units


def _commit_date_of(repo_dir: pathlib.Path, rev: str) -> datetime.datetime | None:
    """Committer date of an arbitrary rev (SHA, tag, or branch) in the local clone, used to
    resolve a `since: {ref|commit}` anchor to a date. Tries the rev as given, then as a
    remote branch (origin/<rev>). Returns None if the rev can't be resolved."""
    info = _rev_info(repo_dir, rev)
    return info[1] if info else None


def _rev_info(repo_dir: pathlib.Path, rev: str) -> tuple[str, datetime.datetime | None] | None:
    """(commit_sha, committer_date) for a rev in the local clone, dereferencing tags to their
    commit so the date matches what write_ref_marker stores (git %cI). Tries the rev as given,
    then origin/<rev> for branches. None if the rev can't be resolved."""
    for candidate in (rev, f"origin/{rev}"):
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_dir), "log", "-1", "--format=%H%x09%cI", candidate],
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, OSError):
            continue
        parts = out.stdout.strip().split("\t")
        if len(parts) == 2:
            return parts[0], _parse_dt(parts[1])
    return None


def _resolve_since_floor(since, repo_dir: pathlib.Path, now: datetime.datetime) -> datetime.datetime | None:
    """One selector's `since` -> a commit-date lower bound. age/date resolve directly;
    ref/commit resolve to the committer date of their commit in the local clone."""
    if since.kind == "age":
        return now - since.value            # timedelta
    if since.kind == "date":
        return since.value                  # datetime
    return _commit_date_of(repo_dir, str(since.value))  # ref | commit


def _effective_since_floor(
    cfg: RepoConfig | None, repo_dir: pathlib.Path, ref_type: str, ref: str, now: datetime.datetime,
) -> datetime.datetime | None:
    """The floor a ref must clear to be indexed, or None if unconstrained. A ref can match
    several selectors; inclusion is a union, so a matching selector with no `since` (or the
    most permissive floor) wins. An unresolvable anchor fails open (doesn't exclude)."""
    if cfg is None:
        return None
    floors: list[datetime.datetime] = []
    for sel in cfg.selectors:
        if sel.matches(ref_type, ref) is None:
            continue
        # No since, or a version-based `since: {ref}` already enforced in Phase 1 -> this
        # selector imposes no post-clone date floor, and inclusion is a union, so it wins.
        if sel.since is None or sel.since_version_floor() is not None:
            return None
        floor = _resolve_since_floor(sel.since, repo_dir, now)
        if floor is None:
            return None
        floors.append(floor)
    return min(floors) if floors else None


def _load_config(config_path: str) -> list[RepoConfig]:
    """Load and validate repos.yml into RepoConfig entries (the new `index:` schema, shared
    with `sourcerer prune`). Raises OSError/ValueError/yaml.YAMLError on a malformed file."""
    return load_config(config_path)


def run_config(
    config_path: str,
    url: str,
    api_key: str | None,
    username: str | None,
    password: str | None,
    force: bool = False,
    quiet: bool = False,
    cache_dir: str | None = None,
    ephemeral: bool = False,
    prune: bool = False,
    dry_run: bool = False,
) -> None:
    """
    Index every (repo, ref) the config selects. First list the remote branches and tags for
    every entry and keep the ones matching its glob patterns (an omitted or empty list selects
    nothing for that ref type), building the full plan; then index each selected ref. One bad
    ref or repo is reported and the batch continues; the command exits non-zero if any failed.

    With `prune` set, once ALL indexing is complete, run the prune step over the same config
    (equivalent to a following `sourcerer prune --config`), deleting already-indexed refs that
    now fall outside their retention policies. Skipped if the run was aborted (Ctrl-C).

    With `dry_run` set, nothing is written to Elasticsearch: the cached repos are cloned/fetched
    to resolve real commits + dates, and a combined report shows what would be indexed and (with
    `prune`) what the post-index prune step would delete. See `dry_run_config`.
    """
    try:
        entries = _load_config(config_path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        click.echo(f"Error: invalid config: {e}", err=True)
        sys.exit(1)

    # Look up a repo's selectors when resolving each ref's `since` floor below.
    cfg_by_repo = {(c.org, c.repo): c for c in entries}

    es = make_client(url, api_key, username, password)
    cache_root = None if ephemeral else resolve_cache_root(cache_dir)

    if dry_run:
        dry_run_config(es, entries, cfg_by_repo, cache_root, ephemeral, force, prune)
        return

    reporter = make_reporter(quiet)
    failures = 0

    with handle_interrupts(), reporter:
        # Phase 1: resolve the full plan (what will be indexed) up front so overall progress
        # has an accurate total and the user sees every org/repo/ref. Each entry needs two
        # `git ls-remote` round-trips, which are independent, so resolve up to
        # RESOLVE_CONCURRENCY entries at once. The completion counter is driven from this (main)
        # thread via as_completed, and results are gathered in submission order so the plan
        # order -- and Phase 2's grouping -- is identical to a serial resolve.
        units: list[Unit] = []
        with ThreadPoolExecutor(
            max_workers=max(1, min(RESOLVE_CONCURRENCY, len(entries) or 1))
        ) as pool:
            futures = [pool.submit(_resolve_entry, entry) for entry in entries]
            done = 0
            for _ in as_completed(futures):
                done += 1
                reporter.planning(f"resolving refs — {done}/{len(entries)} repos")
            for fut in futures:
                units.extend(fut.result())
        # Order the plan lexicographically by (org, repo, ref) regardless of config-file order,
        # so e.g. every elastic/elasticsearch ref precedes elastic/kibana, and a repo's refs go
        # by name. This drives Phase 2's grouping (dict insertion order) and the concurrent
        # dispatch order below -- repos are still indexed up to INDEX_REPO_CONCURRENCY at a time,
        # but they are started in lexicographical order. kind breaks ties between a same-named
        # branch and tag.
        units.sort(key=lambda u: (u.org, u.repo, u.ref or "", u.kind))
        reporter.set_plan(units)

        # Phase 2: index each selected ref, cloning each repo at most once. Group units by
        # (org, repo) -- preserving plan order, and collapsing a repo that appears across
        # multiple config entries. Each group is independent (its own clone + checkouts), so up
        # to INDEX_REPO_CONCURRENCY groups run concurrently, overlapping one repo's clone
        # (network/disk, GIL-releasing) with another's indexing. refresh is disabled across the
        # whole indexing phase (best-effort) for index-side bulk throughput.
        groups: dict[tuple[str, str], list[Unit]] = {}
        for unit in units:
            groups.setdefault((unit.org, unit.repo), []).append(unit)

        failures_lock = threading.Lock()

        def process_group(item: tuple[tuple[str, str], list[Unit]]) -> None:
            nonlocal failures
            # These run in pool worker threads, which never receive Ctrl-C themselves -- so they
            # bail by polling the abort flag. A group not yet started just returns; one in flight
            # stops at the next ref boundary (and index_repo's own poll stops a ref mid-stream).
            if _aborted.is_set():
                return
            (org, repo), group = item
            # 2a. Cheap per-ref skip for the whole group (no clone yet). A transient ES error
            # here fails just that ref (the skip check hits the cluster) and the batch goes on.
            pending: list[tuple[Unit, str | None, str | None]] = []
            for unit in group:
                if _aborted.is_set():
                    return
                reporter.start(unit)
                branch = unit.ref if unit.kind == "branch" else None
                tag = unit.ref if unit.kind == "tag" else None
                try:
                    skip, ref_for_id, _ = pre_clone_skip(es, org, repo, branch, tag, None, force)
                except ES_ERRORS as e:
                    with failures_lock:
                        failures += 1
                    reporter.finish(unit, "error", detail=str(e))
                    continue
                if skip:
                    unit.ref = ref_for_id
                    reporter.finish(unit, "skipped")
                else:
                    pending.append((unit, branch, tag))

            if not pending:
                return  # whole repo already indexed -> no clone at all

            # 2b. Clone/fetch once, then check out and index each pending ref. A failure on one
            # ref -- git, bad value, or a transient ES timeout/connection drop -- is reported and
            # the remaining refs (and other repos) continue. If the persistent cache dir is locked
            # by another run, prepared_repo yields None and the whole repo is skipped this round.
            try:
                reporter.set_stage(pending[0][0], "cloning")
                with prepared_repo(org, repo, cache_root, ephemeral) as repo_dir:
                    if repo_dir is None:
                        for unit, _branch, _tag in pending:
                            reporter.finish(unit, "locked", detail="another sourcerer run holds this repo's cache lock")
                        return
                    # Reorder pending refs newest-first by creation date so more-recent refs
                    # are indexed first. creatordate is available now that the clone exists;
                    # it was not available during Phase 1 (ls-remote returns no timestamps).
                    # Refs absent from the map (unusual) sink to the end with key -1.
                    dates = ref_dates(repo_dir)
                    pending.sort(key=lambda p: dates.get((p[0].kind, p[0].ref or ""), -1), reverse=True)
                    # Reorder the reporter's unit list to match, so the live
                    # display and actual processing order stay consistent.
                    reporter.reorder_group(org, repo, dates)
                    cfg = cfg_by_repo.get((org, repo))
                    now = datetime.datetime.now(datetime.timezone.utc)

                    # 2c. Prune-aware pre-filter. Among the pending refs, drop those `since`
                    # excludes and those `retain` would immediately delete, so we never
                    # pay to index a ref that prune would remove. This runs the SAME planner as
                    # `sourcerer prune`, over the candidate refs, so "indexed" and "survives
                    # prune" can't diverge. count/version/prerelease are cohort-relative, so the
                    # whole group's refs are scored together (e.g. v8.14.0-.2 are dropped once
                    # v8.14.3 is in the candidate set via patch:0).
                    prospective: list[tuple[Unit, str | None, str | None, Marker]] = []
                    for unit, branch, tag in pending:
                        ref_type = "branch" if branch else "tag"
                        info = _rev_info(repo_dir, unit.ref)
                        sha, cd = info if info else ("", None)
                        floor = _effective_since_floor(cfg, repo_dir, ref_type, unit.ref, now)
                        if floor is not None and cd is not None and cd < floor:
                            reporter.finish(unit, "skipped", detail=f"before since floor {floor.date()}")
                            continue
                        prospective.append((unit, branch, tag, Marker(
                            id=f"{ref_type}:{unit.ref}", ref=unit.ref, ref_type=ref_type,
                            commit=sha, commit_date=cd, indexed_at=now,
                        )))

                    doomed: set[str] = set()
                    if cfg is not None and prospective:
                        decisions = plan_repo([m for *_, m in prospective], cfg, now)
                        doomed = {d.marker.id for d in decisions if d.action == "delete"}

                    for unit, branch, tag, marker in prospective:
                        if _aborted.is_set():
                            break
                        if marker.id in doomed:
                            reporter.finish(unit, "skipped", detail="pruned by retain")
                            continue
                        try:
                            index_ref_in_dir(
                                es, org, repo, repo_dir, branch, tag, None,
                                force, reporter, unit,
                            )
                        except KeyboardInterrupt:
                            break  # aborted mid-ref -- stop this group, leave the rest unmarked
                        except (FileNotFoundError, subprocess.CalledProcessError, ValueError, *ES_ERRORS) as e:
                            with failures_lock:
                                failures += 1
                            reporter.finish(unit, "error", detail=str(e))
            except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as e:
                # Clone failed: fail every still-pending ref of this repo, continue others.
                for unit, _branch, _tag in pending:
                    if unit.status is None:
                        with failures_lock:
                            failures += 1
                        reporter.finish(unit, "error", detail=str(e))

        with bulk_indexing_settings(es), ThreadPoolExecutor(
            max_workers=max(1, INDEX_REPO_CONCURRENCY)
        ) as pool:
            # Drain the iterator so any unexpected error surfaces; expected per-ref/clone
            # errors are handled inside process_group and counted in `failures`.
            list(pool.map(process_group, groups.items()))

    # Prune only after ALL indexing is complete, so a ref newly indexed this run is present in
    # the refs index before it's scored for retention (e.g. it can be the cohort-newest that
    # supersedes an older sibling). Skipped on abort: the plan is incomplete, so its retention
    # cohorts would be too. Lazy import breaks the prune<->index module import cycle.
    if prune and not _aborted.is_set():
        from . import prune as prune_cmd
        prune_cmd.run(config_path, url, api_key, username, password, quiet=quiet)

    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)


def _dry_run_repo(
    es: Elasticsearch,
    cfg: RepoConfig | None,
    org: str,
    repo: str,
    group: list[Unit],
    cache_root: pathlib.Path | None,
    ephemeral: bool,
    force: bool,
    prune: bool,
    now: datetime.datetime,
) -> dict:
    """Compute one repo's dry-run preview. Clones/fetches its cached clone to resolve each
    planned ref's real commit + date (read-only -- writes nothing to ES), classifying every ref
    exactly as a real run would (index / reuse content / up-to-date / skip), then, when `prune`,
    plans the post-index prune over existing markers merged with the refs this run would add.
    Returns a result dict consumed by `_print_dry_run_repo`."""
    result: dict = {"org": org, "repo": repo, "rows": [], "prune": None, "locked": False}
    with prepared_repo(org, repo, cache_root, ephemeral) as repo_dir:
        if repo_dir is None:
            result["locked"] = True
            return result
        rows = result["rows"]
        synth: list[Marker] = []

        # Pass 1: resolve each ref and classify the already-decided cases (unresolvable,
        # already-indexed, below a `since` floor). Branches resolve to their fetched
        # origin tip -- the same commit checkout_branch would land on -- not a stale local
        # branch; tags dereference to their commit (matching write_ref_marker).
        pending: list[tuple[Unit, str, str, datetime.datetime | None]] = []
        for unit in group:
            ref_type = unit.kind
            rev = f"origin/{unit.ref}" if ref_type == "branch" else unit.ref
            info = _rev_info(repo_dir, rev)
            sha, cd = info if info else ("", None)
            if not sha:
                rows.append((unit, "error", "could not resolve ref", None, None))
                continue
            if not force and not should_index(es, org, repo, ref_type, unit.ref, sha):
                rows.append((unit, "up-to-date", "already indexed", sha, cd))
                continue
            floor = _effective_since_floor(cfg, repo_dir, ref_type, unit.ref, now)
            if floor is not None and cd is not None and cd < floor:
                rows.append((unit, "skip", f"before since floor {floor.date()}", sha, cd))
                continue
            pending.append((unit, ref_type, sha, cd))

        # Prune-aware pre-filter over the fresh candidates, exactly as process_group does before
        # indexing: a ref this run would immediately re-prune (e.g. an older patch superseded by a
        # newer one also new this run) is never indexed. Scored over the candidate set only.
        doomed: set[str] = set()
        if cfg is not None and pending:
            candidates = [
                Marker(id=f"{rt}:{u.ref}", ref=u.ref, ref_type=rt, commit=sha, commit_date=cd, indexed_at=now)
                for (u, rt, sha, cd) in pending
            ]
            doomed = {d.marker.id for d in plan_repo(candidates, cfg, now) if d.action == "delete"}

        for unit, ref_type, sha, cd in pending:
            if f"{ref_type}:{unit.ref}" in doomed:
                rows.append((unit, "skip", "pruned by retain", sha, cd))
                continue
            # Content is keyed by commit: if another ref already fully indexed this exact commit
            # (and we're not forcing a rewrite), a real run only records the marker -- no
            # re-ingest. A commit with only partial content from an aborted run has no complete
            # marker, so it shows as "index" (re-ingest), matching what the real run will do.
            shared = not force and commit_fully_indexed(es, org, repo, sha)
            rows.append((unit, "reuse" if shared else "index", "content shared" if shared else "", sha, cd))
            synth.append(Marker(
                id=build_ref_id(org, repo, ref_type, unit.ref, sha),
                ref=unit.ref, ref_type=ref_type, commit=sha, commit_date=cd, indexed_at=now,
            ))

        # Prune preview: the markers that would exist AFTER this run = existing markers (older
        # refs a policy now retires, already-indexed refs) merged with the refs this run adds
        # (dedup by id: a synthesized marker for an already-present ref collapses onto it). This
        # is exactly the set the post-index `--prune` step plans over.
        if prune and cfg is not None:
            by_id = {m.id: m for m in fetch_markers(es, org, repo)}
            for m in synth:
                by_id.setdefault(m.id, m)
            result["prune"] = plan_repo(list(by_id.values()), cfg, now)
    return result


# How each dry-run row's status renders; the padding keeps the ref columns aligned.
_DRY_RUN_LABELS = {
    "index": "would index",
    "reuse": "reuse content",
    "up-to-date": "up to date",
    "skip": "skip",
    "error": "error",
}


def _print_dry_run_repo(result: dict, prune: bool) -> tuple[int, int, int]:
    """Print one repo's combined preview (what would be indexed, then what would be pruned) and
    return (index_count, prune_markers, prune_commits) for the run summary."""
    org, repo = result["org"], result["repo"]
    if result["locked"]:
        click.echo(f"{org}/{repo}: locked by another sourcerer run — skipped")
        return (0, 0, 0)

    click.echo(f"{org}/{repo}")
    click.echo("  index:")
    index_count = 0
    for unit, status, detail, sha, cd in result["rows"]:
        if status in ("index", "reuse"):
            index_count += 1
        label = _DRY_RUN_LABELS.get(status, status)
        short = sha[:10] if sha else "-"
        suffix = f"   ({detail})" if detail else ""
        click.echo(f"    {label:14} {unit.kind:6} {unit.ref:28} {short}{suffix}")
    if not result["rows"]:
        click.echo("    (no refs selected)")

    markers = commits = 0
    if prune:
        decisions = result["prune"] or []
        deletes = [d for d in decisions if d.action == "delete"]
        drop = content_delete_set(decisions)
        keeps = sum(1 for d in decisions if d.action == "keep")
        markers, commits = len(deletes), len(drop)
        click.echo("  prune (after indexing):")
        if not deletes:
            click.echo("    (nothing to prune)")
        for d in deletes:
            content = "marker + content" if d.marker.commit in drop else "marker only (commit shared)"
            click.echo(f"    would delete   {d.marker.ref_type:6} {d.marker.ref:28} "
                       f"{d.marker.commit[:10]}  {content}   [{d.reason}]")
        if deletes:
            click.echo(f"    -> {markers} marker(s), {commits} commit(s) of content, {keeps} kept")
    return (index_count, markers, commits)


def dry_run_config(
    es: Elasticsearch,
    entries: list[RepoConfig],
    cfg_by_repo: dict[tuple[str, str], RepoConfig],
    cache_root: pathlib.Path | None,
    ephemeral: bool,
    force: bool,
    prune: bool,
) -> None:
    """Preview an `index [--prune]` run without writing anything to Elasticsearch. Resolves the
    plan (ls-remote), then per repo clones/fetches its cached clone to resolve real commits +
    dates and classify each ref, and -- with `prune` -- plans the post-index prune. Prints one
    combined report (what would be indexed, then what would be pruned) and exits non-zero if any
    repo could not be previewed."""
    now = datetime.datetime.now(datetime.timezone.utc)
    # refs is tiny; refresh once so the prune preview sees the latest existing markers.
    es.indices.refresh(index=REFS_INDEX, ignore_unavailable=True)

    # Phase 1: resolve the plan, identical to the real run (see run_config).
    units: list[Unit] = []
    with ThreadPoolExecutor(max_workers=max(1, min(RESOLVE_CONCURRENCY, len(entries) or 1))) as pool:
        for fut in [pool.submit(_resolve_entry, entry) for entry in entries]:
            units.extend(fut.result())
    units.sort(key=lambda u: (u.org, u.repo, u.ref or "", u.kind))
    groups: dict[tuple[str, str], list[Unit]] = {}
    for unit in units:
        groups.setdefault((unit.org, unit.repo), []).append(unit)

    click.echo("DRY RUN — no changes will be made to Elasticsearch.\n")
    if not groups:
        click.echo("Nothing selected by the config.")
        return

    failures = 0

    def work(item: tuple[tuple[str, str], list[Unit]]) -> dict:
        (org, repo), group = item
        cfg = cfg_by_repo.get((org, repo))
        try:
            return _dry_run_repo(es, cfg, org, repo, group, cache_root, ephemeral, force, prune, now)
        except (FileNotFoundError, subprocess.CalledProcessError, ValueError, *ES_ERRORS) as e:
            return {"org": org, "repo": repo, "error": str(e)}

    # Clone/fetch + resolve repos concurrently (fetches release the GIL); collect all results and
    # print in sorted plan order so the combined report is deterministic and never interleaved.
    with ThreadPoolExecutor(max_workers=max(1, INDEX_REPO_CONCURRENCY)) as pool:
        results = list(pool.map(work, groups.items()))

    tot_index = tot_markers = tot_commits = 0
    for r in results:
        if r.get("error"):
            failures += 1
            click.echo(f"{r['org']}/{r['repo']}: error: {r['error']}", err=True)
            continue
        ci, mk, cm = _print_dry_run_repo(r, prune)
        tot_index += ci
        tot_markers += mk
        tot_commits += cm
        click.echo("")

    summary = f"Summary: would index {tot_index} ref(s)"
    if prune:
        summary += f"; would prune {tot_markers} marker(s) and {tot_commits} commit(s) of content"
    click.echo(summary + ".")
    if failures:
        click.echo(f"Completed with {failures} failure(s)", err=True)
        sys.exit(1)