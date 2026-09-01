# sourcerer/commands/index/git.py
# All git-facing operations for the index command: the clone/fetch cache and its advisory
# locking, clone/checkout, local-clone inspection (commit/ref dates), and cheap remote ref
# resolution via `git ls-remote` (no clone). Nothing here touches Elasticsearch.
#
# Every git invocation goes through _run_git, which runs it non-interactively (git can never
# stop to ask for credentials) under this run's --git-timeout, and raises a classified
# GitError / GitAccessDenied / GitTimeout on failure.

# Standard packages
import contextlib
import datetime
import fcntl
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

# App packages
from ...queries import _parse_dt
from .runtime import git_metadata_timeout, git_timeout


class GitError(subprocess.CalledProcessError):
    """A failed `git` invocation.

    Subclasses CalledProcessError so every existing handler keeps catching git failures
    unchanged, but str() appends the tail of git's captured stderr -- CalledProcessError's own
    message stops at the exit status, which is why a batch-path per-unit error (reported as
    str(e)) used to say nothing about *why* git failed."""

    def __str__(self) -> str:
        detail = _stderr_tail(self.stderr)
        return f"{super().__str__()} {detail}" if detail else super().__str__()


class GitAccessDenied(GitError):
    """git was refused access to the remote: 401/403/404, a rejected credential, or a prompt it
    was not allowed to show. Permanent for this run, so callers skip the repo immediately
    instead of retrying or falling back to a clone that will fail the same way."""


class GitTimeout(GitError):
    """git exceeded this run's --git-timeout and was killed."""

    def __str__(self) -> str:
        return _stderr_tail(self.stderr) or f"git command timed out: {self.cmd}"


# git's own denial messages, matched case-insensitively against stderr. Matching git's message
# shapes rather than a bare "403" avoids classifying an unrelated failure that happens to
# mention the number. "Repository not found" is included because that is what GitHub returns
# for a private repo the caller can't see (it 404s rather than leak its existence) -- equally
# permanent, and the reported error quotes git's stderr so a typo'd repo name stays diagnosable.
_ACCESS_DENIED_RE = re.compile(
    "|".join((
        r"requested url returned error:\s*40[134]",
        r"\b40[13]\s+(?:forbidden|unauthorized)",
        r"authentication failed",
        r"support for password authentication was removed",
        r"invalid username or password",
        r"permission (?:to\b.*)?denied",
        r"repository not found",
        r"could not read (?:username|password)",
        r"terminal prompts disabled",
        r"access denied",
        r"please ask the owner",
    )),
    re.IGNORECASE,
)

# Config injected into commands that talk to a remote: abort a transfer that has delivered less
# than 1 KB/s for a minute rather than leaving it to sit until the (much longer) full timeout.
# HTTP transports only; ssh relies on the timeout.
_STALL_CONFIG = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")
_TIMEOUT_RETURNCODE = 124  # the `timeout(1)` convention


def _as_text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")


def _stderr_tail(raw: str | bytes | None, lines: int = 3) -> str:
    """The last few non-empty lines of git's stderr, joined for single-line error reporting.
    The tail (not the head) because git's `remote:` explanation and its final `fatal:` land
    there, after any progress noise."""
    tail = [line.strip() for line in _as_text(raw).splitlines() if line.strip()]
    return "; ".join(tail[-lines:])


def _git_env() -> dict[str, str]:
    """The environment for every git invocation, hardened so git can never stop and ask a
    question. This is what keeps a repo the run can't read from hanging indexing forever:
    stdout/stderr are captured but stdin is inherited, so git's credential prompt used to block
    on a read nobody would ever answer (and the prompt itself was invisible).

    GIT_ASKPASS is set to the empty string rather than unset: prompt.c takes the first non-NULL
    of GIT_ASKPASS, core.askpass, SSH_ASKPASS and only runs it when non-empty, so an empty-but-
    set value both skips askpass and neutralizes a user's core.askpass / SSH_ASKPASS -- a GUI
    dialog nobody can answer under cron. Non-interactive credential *helpers* (osxkeychain, gh,
    credential-store) are deliberately untouched: they are what makes legitimate private-repo
    access work. Only prompting is disabled, so git now fails immediately with e.g.
    "could not read Username for '<url>': terminal prompts disabled"."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    # Only a default: a user who has configured their own ssh invocation keeps it.
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


def _subcommand(args: list[str]) -> str:
    """The subcommand in a git argv, skipping the leading `-C <dir>` / `-c <k=v>` options, so an
    error message can name what timed out ("git fetch") rather than its first argument."""
    it = iter(args)
    for arg in it:
        if arg in ("-C", "-c"):
            next(it, None)
            continue
        if not arg.startswith("-"):
            return arg
    return "command"


_USE_DEFAULT_TIMEOUT = object()


def _run_git(
    args: list[str],
    *,
    network: bool = False,
    timeout: float | None | object = _USE_DEFAULT_TIMEOUT,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run one git command with the hardened (non-interactive) environment and a timeout, and
    raise a classified GitError on failure. The single funnel for every git invocation, so no
    call site can accidentally reintroduce a promptable or unbounded one.

    `network=True` marks commands that talk to the remote, adding the stall-abort config."""
    cmd = ["git", *(_STALL_CONFIG if network else ()), *args]
    if timeout is _USE_DEFAULT_TIMEOUT:
        timeout = git_timeout()
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=text,
            env=_git_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise GitTimeout(
            _TIMEOUT_RETURNCODE,
            cmd,
            stderr=(
                f"git {_subcommand(args)} timed out after {timeout:g}s"
                " (raise --git-timeout / SOURCERER_GIT_TIMEOUT to allow longer)"
            ),
        ) from e
    except subprocess.CalledProcessError as e:
        cls = GitAccessDenied if _ACCESS_DENIED_RE.search(_as_text(e.stderr)) else GitError
        raise cls(e.returncode, cmd, output=e.output, stderr=e.stderr) from e


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


def repo_cache_dir(cache_root: pathlib.Path, host: str, org: str, repo: str) -> pathlib.Path:
    """The stable per-repo clone path under the cache root: <root>/repos/<host>/<org>/<repo>.
    host is in the path so the same org/repo cloned from two hosts never share a working dir."""
    return cache_root / "repos" / host / org / repo


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
    on demand the first time a commit touching them is checked out.

    A timed-out clone was killed mid-write and had no chance to clean up after itself, so the
    partial directory is removed here -- otherwise the next run would find a dir that is neither
    absent nor a valid clone."""
    try:
        _run_git(["clone", "--filter=blob:none", url, str(repo_dir)], network=True)
    except GitTimeout:
        shutil.rmtree(repo_dir, ignore_errors=True)
        raise


def _git_gc(repo_dir: pathlib.Path) -> None:
    """Reclaim disk from blobs faulted in for commits that are no longer reachable (e.g. a
    branch moved on `fetch --prune`), and from worktree slot registrations orphaned by a
    crash. `worktree prune` runs first since it only touches administrative bookkeeping for
    slot directories that no longer exist -- it never removes a live slot -- and clears the way
    for gc to see accurate reachability. Reflogs are expired next because checkouts leave HEAD
    reflog entries that would otherwise keep old commits (and their blobs) alive; the cache is
    a throwaway derived artifact, so this is safe. Best-effort: a gc failure must not fail the
    index run. Runs once per group, before any ref work starts (see `prepared_repo`), so it
    never races an in-flight checkout in a live worktree slot."""
    try:
        _run_git(["-C", str(repo_dir), "worktree", "prune"])
        _run_git(["-C", str(repo_dir), "reflog", "expire", "--expire=now", "--all"])
        _run_git(["-C", str(repo_dir), "gc", "--prune=now", "--quiet"])
    except (subprocess.CalledProcessError, OSError):
        pass


def _is_clone_of(repo_dir: pathlib.Path, url: str) -> bool:
    """True if `repo_dir` is a valid git working clone whose `origin` points at `url`."""
    if not (repo_dir / ".git").exists():
        return False
    try:
        out = _run_git(["-C", str(repo_dir), "remote", "get-url", "origin"])
    except (subprocess.CalledProcessError, OSError):
        return False
    return out.stdout.strip() == url


@contextlib.contextmanager
def clone_repo(
    url: str,
    repo: str,
    repo_dir: pathlib.Path | None = None,
    ephemeral: bool = False,
) -> Iterator[pathlib.Path]:
    """Make a repo available on disk and yield its path. A blobless clone fetches all branches
    and tags (commits, trees, and refs, but not blobs), so the caller can `checkout_ref` any
    number of refs against this one clone -- blobs fault in on demand as each ref is checked out.
    `url` is the resolved clone URL (from the host registry); `repo` is used only to name the
    temp subdir in the ephemeral case.

    ephemeral (or no repo_dir): blobless-clone into a temp dir and delete it on exit -- the
    original throwaway behaviour, for one-off/CI runs.

    persistent (repo_dir given, not ephemeral): keep the clone at the stable `repo_dir`. If it is
    already a valid clone of this repo, `git fetch` only the new objects (so a scheduled run
    transfers a day's commits, not the whole history), then `git gc` to reclaim blobs faulted in
    for commits that fell out of reachability since the last run; otherwise clone fresh. A fetch
    failure falls back to wipe + re-clone once (recovering a corrupt cache) before giving up. The
    dir is NOT deleted on exit. HEAD is left wherever the fetch/clone leaves it; callers check out
    what they need."""
    if ephemeral or repo_dir is None:
        with tempfile.TemporaryDirectory(prefix="sourcerer-") as tmp:
            tmp_dir = pathlib.Path(tmp) / repo
            _git_clone(url, tmp_dir)
            yield tmp_dir
        return

    if _is_clone_of(repo_dir, url):
        try:
            _run_git(
                ["-C", str(repo_dir), "fetch", "--prune", "--prune-tags", "--tags", "origin"],
                network=True,
            )
            _git_gc(repo_dir)
        except (GitAccessDenied, GitTimeout):
            # Not a local-state problem: re-cloning would discard a good clone and then fail
            # (or time out) all over again, doubling the wall clock for no chance of recovery.
            raise
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
    host: str,
    org: str,
    repo: str,
    url: str,
    cache_root: pathlib.Path | None,
    ephemeral: bool,
) -> Iterator[pathlib.Path | None]:
    """Yield a checked-out-ready repo dir, holding it for the whole `with` body so the caller can
    index any number of refs against one clone. `url` is the resolved clone URL (from the host
    registry). Ephemeral (or no cache_root): a throwaway temp clone. Persistent: take a per-repo
    advisory lock, then clone-or-fetch the stable cache dir. Yields None if the persistent clone
    is locked by another run -- the caller should then skip this repo (the lock is released as
    soon as this manager exits)."""
    if ephemeral or cache_root is None:
        with clone_repo(url, repo, ephemeral=True) as repo_dir:
            yield repo_dir
        return
    repo_dir = repo_cache_dir(cache_root, host, org, repo)
    with repo_lock(repo_dir) as locked:
        if not locked:
            yield None
            return
        with clone_repo(url, repo, repo_dir, ephemeral=False) as ready:
            yield ready


def worktree_slot_dir(repo_dir: pathlib.Path, slot: int) -> pathlib.Path:
    """The working-tree directory for a concurrency slot. Slot 0 IS the clone's own working
    tree (`repo_dir` itself), so a concurrency cap of 1 checks out and indexes into exactly the
    same directory as before -- identical behavior and disk footprint. Slots >= 1 are linked
    worktrees living beside the clone (`<repo>.wt<N>`), sharing its object database, so a
    blobless clone's dedup and single-fetch-per-run properties are unaffected: only the checked-
    out files, not the git objects, are duplicated per slot."""
    return repo_dir if slot == 0 else repo_dir.parent / f"{repo_dir.name}.wt{slot}"


# git's `.git/worktrees/<name>` administrative files are not documented as safe under
# concurrent `worktree add`/`remove`/`prune` calls issued by sibling threads of this same
# process (as opposed to separate processes, which the advisory repo_lock already serializes).
# The checkout that follows registration is unaffected -- each worktree has its own working
# tree and index file -- only the add/prune bookkeeping itself is serialized here.
_WT_ADMIN_LOCK = threading.Lock()


def ensure_worktree(repo_dir: pathlib.Path, slot: int) -> pathlib.Path:
    """Materialize (or reuse) the working tree for `slot`, returning its directory. Slot 0
    needs nothing -- it's the primary clone, already present.

    Slots >= 1 get a `git worktree add` the first time they're used. `repo_dir` (and therefore
    its slot directories) persists across runs like the clone itself, so on every later run the
    slot directory already exists and is reused as-is -- the per-ref cost stays a single
    `checkout --force`, never a repeated add/remove, and successive runs get incremental
    (fetch-then-checkout) rather than from-scratch checkouts even in a linked worktree.

    `--detach ... HEAD` just registers the worktree at *some* valid commit; the caller's own
    `checkout_ref`/`checkout_branch` immediately afterward moves it to whatever ref it actually
    needs, so the starting point here is arbitrary."""
    path = worktree_slot_dir(repo_dir, slot)
    if slot == 0:
        return path
    with _WT_ADMIN_LOCK:
        # A linked worktree's gitdir is a `.git` FILE (not a directory) pointing back at the
        # main clone's `.git/worktrees/<name>`; its presence means the slot is already
        # registered and checkout-ready.
        if not (path / ".git").exists():
            _run_git(["-C", str(repo_dir), "worktree", "add", "--detach", str(path), "HEAD"])
    return path


def checkout_ref(repo_dir: pathlib.Path, ref: str) -> None:
    """Check out an immutable `ref` (a tag or commit SHA) into an existing clone or linked
    worktree. `--force` discards any working-tree state left by a previous ref's checkout so it
    can't bleed into the next index pass. Branches go through `checkout_branch` instead, which
    targets the fetched remote tip rather than a (possibly stale) local branch.

    Counts as a network command: on a blobless clone, checkout faults this ref's blobs in from
    the promisor remote."""
    _run_git(["-C", str(repo_dir), "checkout", "--force", ref], network=True)


def checkout_branch(repo_dir: pathlib.Path, branch: str) -> None:
    """Check out a branch at its fetched remote tip, into an existing clone or linked worktree.
    Detached (not onto a local branch pointer): `git worktree` refuses to check out the same
    local branch name in two worktrees at once, and nothing downstream needs a local branch ref
    -- `list_branch_commits` and `_rev_info` both read `origin/<branch>` directly for exactly
    this reason. Detaching also sidesteps the staleness a plain `git checkout <branch>` would
    have on a reused (persistent) clone, since `git fetch` advances `origin/<branch>` but not a
    local branch pointer. `--force` discards leftover working-tree state. Works the same
    whether `repo_dir` is the primary clone or a linked worktree slot. Network: blobs fault in
    from the promisor remote (see checkout_ref)."""
    _run_git(
        ["-C", str(repo_dir), "checkout", "--force", "--detach", f"origin/{branch}"],
        network=True,
    )


def ref_dates(repo_dir: pathlib.Path) -> dict[tuple[str, str], int]:
    """Return a mapping of (kind, short_name) -> Unix timestamp for every branch and tag in the
    clone, using `git for-each-ref --sort=-creatordate`. `creatordate` resolves to the tagger
    date for annotated tags and the committer date for lightweight tags and branches, so it gives
    meaningful recency regardless of whether refs are semver-tagged or not.

    Returns {} on any failure so callers degrade gracefully to their existing order."""
    try:
        result = _run_git([
            "-C", str(repo_dir), "for-each-ref",
            "--format=%(refname) %(creatordate:unix)",
            "refs/heads", "refs/tags",
        ])
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
    result = _run_git(["-C", str(repo_dir), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    # e.g. "origin/main" -> "main"
    return result.stdout.strip().split("/", 1)[1]


def resolve_commit(repo_dir: pathlib.Path) -> str:
    result = _run_git(["-C", str(repo_dir), "rev-parse", "HEAD"])
    return result.stdout.strip()


def get_symlink_paths(repo_dir: pathlib.Path) -> frozenset[str]:
    """Return the set of repo-relative paths that git tracks as symlinks (mode 120000).
    Works regardless of core.symlinks -- when git checks out symlinks as plain text files
    (core.symlinks=false), path.is_symlink() returns False, but this identifies them via
    the git object mode.

    Streamed rather than run through _run_git, so it carries the hardened environment but no
    hard timeout: it reads the local index only, with no remote to stall on."""
    proc = subprocess.Popen(
        ["git", "-C", str(repo_dir), "ls-files", "--stage", "-z"],
        stdout=subprocess.PIPE,
        env=_git_env(),
    )
    symlinks: set[str] = set()
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
                    text = entry.decode("utf-8", errors="surrogateescape")
                    if text.startswith("120000 "):
                        symlinks.add(text.split("\t", 1)[1])
        if buf:
            text = buf.decode("utf-8", errors="surrogateescape")
            if text.startswith("120000 "):
                symlinks.add(text.split("\t", 1)[1])
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, "git ls-files --stage")
    finally:
        if proc.poll() is None:
            proc.kill()
    return frozenset(symlinks)


def iter_tracked_files(repo_dir: pathlib.Path) -> Iterator[str]:
    """Local-index read, streamed like get_symlink_paths (hardened env, no hard timeout)."""
    proc = subprocess.Popen(
        ["git", "-C", str(repo_dir), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        env=_git_env(),
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


@dataclass
class ChangePlan:
    """A pure plan for turning one incremental branch update (old commit -> new commit) into
    Elasticsearch work. `delete_paths` are the prior file/line docs to remove synchronously;
    `index_paths` are the current tree paths to (re)index. A modified or type-changed file
    appears in BOTH (delete its stale docs, then re-index every current line). `base_missing`
    is True when the old commit object is unavailable locally -- the caller must then fall back
    to full branch-namespace reconciliation instead of trusting an empty diff."""

    delete_paths: list[str] = field(default_factory=list)
    index_paths: list[str] = field(default_factory=list)
    base_missing: bool = False


def base_commit_available(repo_dir: pathlib.Path, old_sha: str) -> bool:
    """True if `old_sha` resolves to a commit object present in the local clone. Uses
    `git cat-file -e <sha>^{commit}` so a tag or partial object still fails closed. A False
    here means the diff base is gone (e.g. force-push, shallow clone) and the caller must
    rebuild rather than treat the missing base as an empty diff."""
    try:
        _run_git(["-C", str(repo_dir), "cat-file", "-e", f"{old_sha}^{{commit}}"], text=False)
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def _dedupe(paths: list[str]) -> list[str]:
    """Order-preserving de-duplication (dict keeps first-seen order)."""
    return list(dict.fromkeys(paths))


def _parse_name_status_z(raw: bytes) -> tuple[list[str], list[str]]:
    """Parse `git diff --name-status -z` output into (delete_paths, index_paths).

    The `-z` stream is a flat run of NUL-terminated tokens: a status token, then one path
    (add/modify/delete/type-change) or two paths (rename/copy: old then new). Parsing by NUL
    boundary -- never by whitespace -- preserves spaces, tabs, and unusual bytes in paths.
    Mapping (rename detection is an optimization; delete+add of the same paths is equivalent):
      A (add)          -> index new
      M (modify)       -> delete + index (replace at file granularity)
      T (type change)  -> delete + index
      D (delete)       -> delete
      R (rename)       -> delete old + index new
      C (copy)         -> index new (source is unchanged, stays indexed)
    An unrecognized status fails safe as delete + index of its path."""
    tokens = raw.split(b"\x00")
    # Each real token is NUL-terminated, so a well-formed stream ends in an empty tail token;
    # drop trailing empties so a complete record's last path isn't misread as "present but
    # empty" and a truncated record is detected by running past the end.
    while tokens and tokens[-1] == b"":
        tokens.pop()
    delete_paths: list[str] = []
    index_paths: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        status = tokens[i]
        if not status:
            i += 1
            continue
        code = status.decode("utf-8", errors="surrogateescape")[0]
        if code in ("R", "C"):
            if i + 2 >= n:
                break  # truncated record
            old = tokens[i + 1].decode("utf-8", errors="surrogateescape")
            new = tokens[i + 2].decode("utf-8", errors="surrogateescape")
            i += 3
            if code == "R":
                delete_paths.append(old)
            index_paths.append(new)
        else:
            if i + 1 >= n:
                break  # truncated record
            path = tokens[i + 1].decode("utf-8", errors="surrogateescape")
            i += 2
            if code == "A":
                index_paths.append(path)
            elif code == "D":
                delete_paths.append(path)
            else:  # M, T, or an unknown status -> replace the whole file
                delete_paths.append(path)
                index_paths.append(path)
    return _dedupe(delete_paths), _dedupe(index_paths)


def plan_changes(repo_dir: pathlib.Path, old_sha: str, new_sha: str) -> ChangePlan:
    """Build the ChangePlan for advancing an incremental branch from `old_sha` to `new_sha`.
    Returns a `base_missing` plan (no paths) when the old commit is unavailable locally; the
    caller then rebuilds the branch namespace. Otherwise runs a NUL-delimited name-status diff
    and maps each record (see _parse_name_status_z).

    Rename and copy detection (-M / -C) are deliberately disabled, so this is a pure tree/OID
    comparison that never reads a blob. Both flags score candidates by *content*, and clones
    are blobless (`clone --filter=blob:none`), so on a partial clone either one blocks on a
    promisor fetch of every candidate blob -- measured at tens of seconds before a single file
    was indexed. -C is the worse of the two (it scans the whole preimage, so its cost tracks
    tree size rather than diff size), but -M faults blobs too whenever a diff has unpaired
    adds and deletes to score against each other.

    Neither flag changes the resulting plan, which is why disabling them is free: an `R`
    record yields "delete old + index new", exactly what the `D` + `A` pair it degrades to
    produces, and a `C` record yields "index the destination", exactly what a plain `A`
    produces. _parse_name_status_z still handles `R` and `C` so a caller that enables
    detection stays correct."""
    if not base_commit_available(repo_dir, old_sha):
        return ChangePlan(base_missing=True)
    result = _run_git(
        ["-C", str(repo_dir), "diff", "--name-status", "-z", "--no-renames", old_sha, new_sha],
        text=False,
    )
    delete_paths, index_paths = _parse_name_status_z(result.stdout)
    return ChangePlan(delete_paths=delete_paths, index_paths=index_paths)


def _ls_remote(url: str, *patterns: str, flags: tuple[str, ...] = ()) -> str | None:
    """
    Run `git ls-remote` against a remote without cloning. The URL must precede the ref
    patterns (HEAD/refs/...), so flags go before the URL and patterns after it. Returns
    stdout, or None on a transient failure.

    GitAccessDenied is *not* swallowed: a remote that refuses us will refuse us again, so it
    propagates instead of masquerading as a transient failure that callers retry and then
    silently skip. Metadata-only, so it uses the shorter metadata timeout.
    """
    try:
        result = _run_git(
            ["ls-remote", *flags, url, *patterns],
            network=True,
            timeout=git_metadata_timeout(),
        )
    except GitAccessDenied:
        raise
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout


def resolve_remote(
    url: str, branch: str | None, tag: str | None
) -> tuple[str | None, str | None]:
    """
    Cheaply resolve a ref to its commit SHA via `git ls-remote` (no clone) against `url`.

    Returns (commit_sha, default_branch). default_branch is only set when neither
    branch nor tag is given (resolving the remote HEAD). Returns (None, None) on a transient
    failure so callers fall through to cloning; a GitAccessDenied propagates instead, since
    falling through would only reach a clone the remote refuses the same way -- the caller's
    per-unit error handler reports it with git's own message.
    """
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


def list_remote_refs(url: str, kind: str) -> dict[str, str] | None:
    """
    List the short names and commit SHAs of a remote's refs of `kind` ("heads" or "tags") via
    `git ls-remote`, without cloning, against `url`. Returns a mapping of {short_name: commit_sha}.

    For annotated tags the remote emits two lines -- the tag object SHA and the peeled commit SHA
    (suffixed with `^{}`). The peeled line wins so the SHA matches `git rev-parse HEAD` after
    checkout, matching the behaviour of `resolve_remote`.

    Returns None on ls-remote failure after retries; returns {} when the remote has no refs of
    that kind. A GitAccessDenied propagates out of the retry loop on the first attempt -- there
    is nothing to back off for when the remote has refused us.
    """
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
    result: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha, refname = parts
        if not refname.startswith(prefix):
            continue
        short = refname[len(prefix):]
        if short.endswith("^{}"):
            # Peeled commit SHA for an annotated tag -- always overwrites the tag-object SHA.
            result[short[:-len("^{}")]] = sha
        else:
            # Only set if not already set by a peeled entry (peeled line wins).
            result.setdefault(short, sha)
    return result


def list_remote_ref_names(url: str, kind: str) -> list[str] | None:
    """
    List the short names of a remote's refs of `kind` ("heads" or "tags") via `git ls-remote`,
    without cloning, against `url`. Strips the `refs/<kind>/` prefix and the peeled `^{}` suffix
    (annotated tags appear twice), dedupes, and returns them sorted. Returns None on ls-remote
    failure after retries (caller should warn); returns [] when the remote has no refs of that
    kind.
    """
    refs = list_remote_refs(url, kind)
    if refs is None:
        return None
    return sorted(refs)


def commit_date(repo_dir: pathlib.Path) -> str | None:
    """Strict ISO-8601 committer date of the checked-out HEAD (git %cI), or None on failure.

    The "age of the code" clock: used for max_age pruning and for head-ordering in
    resolve_head. Distinct from indexed_at (rebuild recency, used by keep_recent) -- a tag
    cut three years ago is old regardless of when it was indexed."""
    try:
        result = _run_git(["-C", str(repo_dir), "show", "-s", "--format=%cI", "HEAD"])
    except (subprocess.CalledProcessError, OSError):
        return None
    return result.stdout.strip() or None


def _rev_info(repo_dir: pathlib.Path, rev: str) -> tuple[str, datetime.datetime | None] | None:
    """(commit_sha, committer_date) for a rev in the local clone, dereferencing tags to their
    commit so the date matches what write_ref_marker stores (git %cI). Tries the rev as given,
    then origin/<rev> for branches. None if the rev can't be resolved."""
    for candidate in (rev, f"origin/{rev}"):
        try:
            out = _run_git(["-C", str(repo_dir), "log", "-1", "--format=%H%x09%cI", candidate])
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


def list_branch_commits(
    repo_dir: pathlib.Path,
    branch: str,
    since_floor: datetime.datetime,
) -> list[tuple[str, datetime.datetime]]:
    """Return (commit_sha, committer_date) pairs for commits on the first-parent mainline of
    origin/<branch> whose committer date is >= since_floor (inclusive), newest-first.

    Uses `--first-parent` so only the branch's own mainline is walked — merged feature-branch
    commits are collapsed to the merge commit and not enumerated separately. This matches the
    intuitive "history of main" and avoids a combinatorial explosion on repos with heavy branching.

    The date cutoff is applied in Python (cd >= since_floor), NOT via `git log --since`. Git's
    `--since` uses approxidate, which silently returns nothing for pre-1970 floors (e.g.
    `since.age: 100y` -> a ~1926 floor) and is fuzzy/exclusive at the exact-timestamp boundary.
    Filtering here gives exact, inclusive semantics matching `_effective_since_floor`'s `cd < floor`
    exclusion, for any floor. Reading the full first-parent list is cheap -- it is commit metadata
    (SHA + date) only, no blobs -- and the retention pre-filter still bounds what actually gets
    indexed.

    Returns [] on any subprocess failure so callers degrade gracefully (the tip-only fallback
    is used instead)."""
    try:
        out = _run_git([
            "-C", str(repo_dir), "log",
            "--first-parent",
            "--format=%H%x09%cI",
            f"origin/{branch}",
        ])
    except (subprocess.CalledProcessError, OSError):
        return []
    results: list[tuple[str, datetime.datetime]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        sha, iso = parts
        cd = _parse_dt(iso)
        # git log is newest-first, so the kept subset stays newest-first.
        if cd is not None and cd >= since_floor:
            results.append((sha, cd))
    return results
