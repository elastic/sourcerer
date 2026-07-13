# sourcerer/commands/index/git.py
# All git-facing operations for the index command: the clone/fetch cache and its advisory
# locking, clone/checkout, local-clone inspection (commit/ref dates), and cheap remote ref
# resolution via `git ls-remote` (no clone). Nothing here touches Elasticsearch.

# Standard packages
import contextlib
import datetime
import fcntl
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator

# App packages
from ...queries import _parse_dt

GITHUB_URL_TEMPLATE = "https://github.com/{org}/{repo}.git"


def resolve_cache_root(cache_dir: str | None = None) -> pathlib.Path:
    """Resolve the cache root, precedence: --cache-dir > SOURCERER_CACHE_DIR > $XDG_CACHE_HOME/
    sourcerer > ~/.cache/sourcerer. Returns the path; callers create the per-repo subdir lazily."""
    if cache_dir:
        return pathlib.Path(cache_dir).expanduser()
    env_cache_dir = os.environ.get("SOURCERER_CACHE_DIR")
    if env_cache_dir:
        return pathlib.Path(env_cache_dir).expanduser()
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
    """Blobless partial clone: every commit, tree, and ref is present (so any ref/commit stays
    reachable and checkoutable), but blobs are not downloaded up front -- git faults them in
    on demand the first time a commit touching them is checked out."""
    subprocess.run(
        ["git", "clone", "--filter=blob:none", url, str(repo_dir)],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_gc(repo_dir: pathlib.Path) -> None:
    """Reclaim disk from blobs faulted in for commits that are no longer reachable (e.g. a
    branch moved on `fetch --prune`). Reflogs are expired first because checkouts leave HEAD
    reflog entries that would otherwise keep old commits (and their blobs) alive; the cache is
    a throwaway derived artifact, so this is safe. Best-effort: a gc failure must not fail the
    index run."""
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "reflog", "expire", "--expire=now", "--all"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "gc", "--prune=now", "--quiet"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        pass


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
    """Make a repo available on disk and yield its path. A blobless clone fetches all branches
    and tags (commits, trees, and refs, but not blobs), so the caller can `checkout_ref` any
    number of refs against this one clone -- blobs fault in on demand as each ref is checked out.

    ephemeral (or no repo_dir): blobless-clone into a temp dir and delete it on exit -- the
    original throwaway behaviour, for one-off/CI runs.

    persistent (repo_dir given, not ephemeral): keep the clone at the stable `repo_dir`. If it is
    already a valid clone of this repo, `git fetch` only the new objects (so a scheduled run
    transfers a day's commits, not the whole history), then `git gc` to reclaim blobs faulted in
    for commits that fell out of reachability since the last run; otherwise clone fresh. A fetch
    failure falls back to wipe + re-clone once (recovering a corrupt cache) before giving up. The
    dir is NOT deleted on exit. HEAD is left wherever the fetch/clone leaves it; callers check out
    what they need."""
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
            _git_gc(repo_dir)
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


def list_remote_ref_names(org: str, repo: str, kind: str) -> list[str] | None:
    """
    List the short names of a remote's refs of `kind` ("heads" or "tags") via `git ls-remote`,
    without cloning. Strips the `refs/<kind>/` prefix and the peeled `^{}` suffix (annotated
    tags appear twice), dedupes, and returns them sorted. Returns None on ls-remote failure
    after retries (caller should warn); returns [] when the remote has no refs of that kind.
    """
    url = GITHUB_URL_TEMPLATE.format(org=org, repo=repo)
    out = None
    for attempt in range(3):
        out = _ls_remote(url, flags=(f"--{kind}",))
        if out is not None:
            break
        if attempt < 2:
            time.sleep(2 ** attempt)  # 1s, 2s backoff before retry
    if out is None:
        return None
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


def _commit_date_of(repo_dir: pathlib.Path, rev: str) -> datetime.datetime | None:
    """Committer date of an arbitrary rev (SHA, tag, or branch) in the local clone, used to
    resolve a `since: {ref|commit}` anchor to a date. Tries the rev as given, then as a
    remote branch (origin/<rev>). Returns None if the rev can't be resolved."""
    info = _rev_info(repo_dir, rev)
    return info[1] if info else None
