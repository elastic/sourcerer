"""Unit tests for the blobless-clone and gc behavior in sourcerer.commands.index.git. These
call the real `git` binary against local repos (no network) rather than mocking subprocess,
because the thing under test is git's own object-model behavior (does the filter really omit
blobs, does checkout really fault them back in, does gc really reclaim orphaned objects) --
asserting on the constructed argv would not catch a regression in any of that.

Note on the blobless-clone fixture: a plain local-path or file:// clone silently ignores
--filter=blob:none (git ignores it for path clones and gets no filtering over file:// on a
local bare repo, since these are handled differently to the wire protocol used against a real
server). Using the `ext::` transport to invoke `git upload-pack` as a subprocess forces the
same code path a real network remote uses, so the filter is honored -- confirmed against a
real GitHub clone during investigation. `GIT_ALLOW_PROTOCOL=ext` is required for git to permit
that transport at all."""

# Standard packages
import pathlib
import subprocess

# Third-party packages
import pytest

# App packages
from sourcerer.commands.index import git as gitmod


def _run(*args: str, cwd: pathlib.Path | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit_file(work_dir: pathlib.Path, name: str, content: bytes, message: str) -> None:
    (work_dir / name).write_bytes(content)
    _run("-C", str(work_dir), "add", name)
    _run("-C", str(work_dir), "-c", "user.email=test@example.com", "-c", "user.name=test",
         "commit", "-q", "-m", message)


def _blob_sha(repo_dir: pathlib.Path, rev: str, path: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-tree", "-r", rev],
        check=True, capture_output=True, text=True,
    ).stdout
    return next(line.split()[2] for line in out.splitlines() if line.endswith(f"\t{path}"))


def _missing_objects(repo_dir: pathlib.Path) -> set[str]:
    """Objects the clone knows about (from a commit/tree walk) but hasn't fetched -- a
    non-mutating check, unlike `cat-file`, which would lazily fetch a missing blob just by
    asking about it."""
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-list", "--objects", "--all", "--missing=print"],
        check=True, capture_output=True, text=True,
    ).stdout
    return {line[1:] for line in out.splitlines() if line.startswith("?")}


def _object_exists(repo_dir: pathlib.Path, sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo_dir), "cat-file", "-e", sha],
        capture_output=True,
    ).returncode == 0


@pytest.fixture
def promisor_origin(tmp_path, monkeypatch):
    """A bare repo usable as a local 'origin' that actually honors --filter=blob:none end to
    end (server-side filtering plus later blob fault-in via checkout), exposed over the
    `ext::` transport per the module docstring."""
    origin_dir = tmp_path / "origin.git"
    _run("init", "--bare", "-q", "-b", "main", str(origin_dir))
    _run("-C", str(origin_dir), "config", "uploadpack.allowFilter", "true")
    _run("-C", str(origin_dir), "config", "uploadpack.allowReachableSHA1InWant", "true")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext:file")  # file: needed to seed origin_dir via a plain clone
    return origin_dir, f"ext::git upload-pack {origin_dir}"


class TestBloblessClone:
    def test_other_branch_blob_missing_until_checked_out(self, tmp_path, promisor_origin):
        origin_dir, url = promisor_origin
        work = tmp_path / "work"
        _run("clone", "-q", str(origin_dir), str(work))
        _commit_file(work, "main.bin", b"main-content", "main commit")
        _run("-C", str(work), "push", "-q", "origin", "HEAD:main")
        _run("-C", str(work), "checkout", "-q", "-b", "feature")
        _commit_file(work, "feature.bin", b"feature-content", "feature commit")
        _run("-C", str(work), "push", "-q", "origin", "HEAD:feature")

        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)

        # main is the default branch, checked out automatically by clone -- its blob is present.
        main_sha = _blob_sha(dest, "main", "main.bin")
        assert main_sha not in _missing_objects(dest)

        # feature was never checked out -- its blob was never downloaded.
        feature_sha = _blob_sha(dest, "origin/feature", "feature.bin")
        assert feature_sha in _missing_objects(dest)

        gitmod.checkout_branch(dest, "feature")

        # Checking it out faults the blob in from the promisor remote.
        assert feature_sha not in _missing_objects(dest)
        assert (dest / "feature.bin").read_bytes() == b"feature-content"


class TestGitGc:
    def test_reclaims_objects_orphaned_by_a_moved_branch(self, tmp_path):
        repo = tmp_path / "repo"
        _run("init", "-q", "-b", "main", str(repo))
        _commit_file(repo, "a.txt", b"a", "commit A")
        orphaned_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert _object_exists(repo, orphaned_sha)

        # Move main to an unrelated history, abandoning commit A -- reachable only via reflog.
        _run("-C", str(repo), "checkout", "-q", "--orphan", "temp")
        _commit_file(repo, "b.txt", b"b", "commit B")
        _run("-C", str(repo), "branch", "-f", "main", "temp")
        _run("-C", str(repo), "checkout", "-q", "main")
        _run("-C", str(repo), "branch", "-D", "temp")

        assert _object_exists(repo, orphaned_sha)  # still around via reflog, pre-gc

        gitmod._git_gc(repo)

        assert not _object_exists(repo, orphaned_sha)  # reclaimed

    def test_is_best_effort_and_does_not_raise_on_failure(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        gitmod._git_gc(not_a_repo)  # must not raise
