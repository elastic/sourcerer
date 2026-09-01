"""Unit tests for git helpers in sourcerer.commands.index.git. These
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
import datetime
import os
import pathlib
import shutil
import subprocess
import time

# Third-party packages
import pytest

# App packages
from sourcerer.commands.index import git as gitmod
from sourcerer.commands.index import runtime as runtimemod


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


@pytest.fixture
def local_origin(tmp_path):
    """A bare origin repo seeded with one branch and one lightweight + one annotated tag.
    Exposed via a plain file:// URL (no ext:: transport needed since we are only testing
    ls-remote, not blobless filtering)."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"

    _run("init", "-q", "-b", "main", str(work))
    _commit_file(work, "a.txt", b"a", "init")
    _run("-C", str(work), "-c", "user.email=test@example.com", "-c", "user.name=test",
        "tag", "v0.1")          # lightweight tag
    _run("-C", str(work), "-c", "user.email=test@example.com", "-c", "user.name=test",
        "tag", "-a", "v1.0", "-m", "release")  # annotated tag
    _run("-C", str(work), "checkout", "-q", "-b", "feature")
    _commit_file(work, "b.txt", b"b", "feature commit")

    _run("clone", "--bare", "-q", str(work), str(origin))
    url = f"file://{origin}"
    return origin, url, work


def _sha(repo: pathlib.Path, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _restore_git_timeout():
    """set_git_timeout writes process-wide state, so keep a test's choice from leaking into
    the next one (which would otherwise inherit a sub-second timeout)."""
    saved = (runtimemod._git_timeout_override, runtimemod._git_timeout_is_set)
    yield
    runtimemod._git_timeout_override, runtimemod._git_timeout_is_set = saved


@pytest.fixture
def git_shim(tmp_path, monkeypatch):
    """Put a scripted stand-in for `git` first on PATH.

    A shim rather than a mocked subprocess because what is under test lives in the gap between
    the argv and the process: whether a git that never exits is actually killed, what
    environment git is handed, and how its stderr is classified. `$REAL_GIT` is available to a
    shim body that wants to fail one subcommand and pass the rest through to real git."""
    import shutil as _shutil

    bin_dir = tmp_path / "shim-bin"
    bin_dir.mkdir()
    calls = bin_dir / "calls.log"
    env_dump = bin_dir / "env.txt"
    real_git = _shutil.which("git")

    class Shim:
        def install(self, body: str) -> None:
            script = bin_dir / "git"
            script.write_text(
                "#!/bin/sh\n"
                f'REAL_GIT="{real_git}"\n'
                f'echo "$@" >> "{calls}"\n'
                f'env > "{env_dump}"\n'
                f"{body}\n"
            )
            script.chmod(0o755)
            monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        @property
        def call_count(self) -> int:
            return len(self.arg_lines)

        @property
        def arg_lines(self) -> list[str]:
            """One line per invocation: the arguments git was called with."""
            return calls.read_text().splitlines() if calls.exists() else []

        @property
        def env(self) -> dict[str, str]:
            out = {}
            for line in env_dump.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    out[key] = value
            return out

    return Shim()


DENY = 'echo "fatal: Authentication failed for \'https://example.invalid/x.git\'" >&2; exit 128'


class TestGitTimeout:
    """A git command that never returns must be killed, not waited on forever.

    Regression test for indexing hanging indefinitely against a repo the run has no access to:
    stdout/stderr were captured but stdin was inherited, so git's (invisible) credential prompt
    blocked on a read nobody would ever answer -- and in the persistent-cache path it did so
    while holding the repo's advisory lock."""

    def test_hanging_git_is_killed_at_the_timeout(self, git_shim, tmp_path):
        git_shim.install("sleep 60")
        runtimemod.set_git_timeout(datetime.timedelta(milliseconds=500))

        started = time.monotonic()
        with pytest.raises(gitmod.GitTimeout):
            gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert time.monotonic() - started < 10

    def test_timeout_error_says_how_to_raise_the_limit(self, git_shim, tmp_path):
        git_shim.install("sleep 60")
        runtimemod.set_git_timeout(datetime.timedelta(milliseconds=500))

        with pytest.raises(gitmod.GitTimeout) as exc:
            gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        # Names the subcommand, not `-C`: the message is what lands in the per-ref error report.
        assert "git rev-parse timed out" in str(exc.value)
        assert "--git-timeout" in str(exc.value)

    def test_timeout_is_caught_by_existing_calledprocesserror_handlers(self, git_shim, tmp_path):
        """GitTimeout subclasses CalledProcessError, so the degrade-gracefully helpers (and
        every per-unit error handler) keep working without knowing the new types exist."""
        git_shim.install("sleep 60")
        runtimemod.set_git_timeout(datetime.timedelta(milliseconds=500))

        assert gitmod.commit_date(tmp_path) is None
        gitmod._git_gc(tmp_path)  # best-effort: must not raise

    def test_zero_duration_disables_the_timeout(self, git_shim, tmp_path):
        git_shim.install("exit 0")
        runtimemod.set_git_timeout(datetime.timedelta(0))

        assert runtimemod.git_timeout() is None
        assert runtimemod.git_metadata_timeout() is None
        gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])  # must not raise

    def test_metadata_timeout_is_capped_below_a_long_full_timeout(self):
        runtimemod.set_git_timeout(datetime.timedelta(hours=1))
        assert runtimemod.git_timeout() == 3600
        assert runtimemod.git_metadata_timeout() == runtimemod._METADATA_GIT_TIMEOUT_CAP

    def test_metadata_timeout_follows_a_shorter_full_timeout(self):
        runtimemod.set_git_timeout(datetime.timedelta(seconds=5))
        assert runtimemod.git_metadata_timeout() == 5

    def test_timed_out_clone_leaves_no_partial_directory(self, git_shim, tmp_path):
        """A killed clone had no chance to clean up after itself, so the dir it was writing must
        be removed -- otherwise the next run finds something that is neither absent nor a clone."""
        # The destination is git's last argument, whatever config flags precede it.
        git_shim.install('for a in "$@"; do last="$a"; done; mkdir -p "$last"; sleep 60')
        runtimemod.set_git_timeout(datetime.timedelta(milliseconds=500))

        dest = tmp_path / "dest"
        with pytest.raises(gitmod.GitTimeout):
            gitmod._git_clone("https://example.invalid/x.git", dest)
        assert not dest.exists()


class TestAccessDenied:
    """A remote that refuses us is permanent: classified, reported with git's own words, and
    never retried."""

    @pytest.mark.parametrize("stderr", [
        "remote: Permission to elastic/private.git denied to someone.",
        "fatal: Authentication failed for 'https://github.com/elastic/private.git/'",
        "fatal: unable to access 'https://x/': The requested URL returned error: 403",
        "fatal: unable to access 'https://x/': The requested URL returned error: 401",
        "remote: Repository not found.",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "remote: Invalid username or password.",
        "remote: Support for password authentication was removed.",
        "ERROR: Permission denied (publickey).",
    ])
    def test_denial_messages_are_classified(self, git_shim, tmp_path, stderr):
        git_shim.install(f'echo "{stderr}" >&2; exit 128')
        with pytest.raises(gitmod.GitAccessDenied) as exc:
            gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert stderr.strip(".") in str(exc.value)

    def test_ordinary_failure_is_not_classified_as_denied(self, git_shim, tmp_path):
        git_shim.install('echo "fatal: not a git repository" >&2; exit 128')
        with pytest.raises(gitmod.GitError) as exc:
            gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert not isinstance(exc.value, gitmod.GitAccessDenied)
        # str(CalledProcessError) alone stops at the exit status; the reason must survive to
        # the per-unit error report, which prints str(e).
        assert "not a git repository" in str(exc.value)

    def test_ls_remote_denial_is_not_retried(self, git_shim, monkeypatch):
        """The 3-attempt backoff exists for a flaky network; a refusal will be refused again."""
        git_shim.install(DENY)
        monkeypatch.setattr(gitmod.time, "sleep", lambda *_: None)

        with pytest.raises(gitmod.GitAccessDenied):
            gitmod.list_remote_refs("https://example.invalid/x.git", "heads")
        assert git_shim.call_count == 1

    def test_ls_remote_transient_failure_still_retries(self, git_shim, monkeypatch):
        git_shim.install('echo "fatal: unable to access: Could not resolve host: x" >&2; exit 128')
        monkeypatch.setattr(gitmod.time, "sleep", lambda *_: None)

        assert gitmod.list_remote_refs("https://example.invalid/x.git", "heads") is None
        assert git_shim.call_count == 3

    def test_denied_fetch_does_not_wipe_and_reclone_the_cache(self, git_shim, tmp_path, local_origin):
        """The wipe-and-reclone fallback recovers a corrupt object store. On a denial it would
        throw away a good clone and then fail again, so the cached clone must survive intact."""
        _origin, url, _work = local_origin
        repo_dir = tmp_path / "cache" / "repo"
        repo_dir.parent.mkdir(parents=True)
        gitmod._git_clone(url, repo_dir)
        head_before = _sha(repo_dir, "HEAD")

        git_shim.install(
            'case " $* " in\n'
            f'  *" fetch "*) {DENY};;\n'
            '  *) exec "$REAL_GIT" "$@";;\n'
            "esac"
        )

        with pytest.raises(gitmod.GitAccessDenied):
            with gitmod.clone_repo(url, "repo", repo_dir, ephemeral=False):
                pass
        assert (repo_dir / ".git").exists()
        assert _sha(repo_dir, "HEAD") == head_before


class TestNonInteractiveEnvironment:
    """git must never be able to stop and ask a question -- the actual fix for the hang."""

    def test_terminal_prompt_is_disabled(self, git_shim, tmp_path):
        git_shim.install("exit 0")
        gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert git_shim.env["GIT_TERMINAL_PROMPT"] == "0"

    def test_askpass_is_set_but_empty(self, git_shim, tmp_path):
        """Empty-but-set is the load-bearing detail: prompt.c takes the first non-NULL of
        GIT_ASKPASS, core.askpass, SSH_ASKPASS and runs it only when non-empty, so this both
        skips askpass and neutralizes a user's core.askpass / SSH_ASKPASS GUI dialog."""
        git_shim.install("exit 0")
        gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert git_shim.env["GIT_ASKPASS"] == ""

    def test_ssh_is_defaulted_to_batch_mode(self, git_shim, tmp_path):
        git_shim.install("exit 0")
        gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert "BatchMode=yes" in git_shim.env["GIT_SSH_COMMAND"]

    def test_user_ssh_command_is_preserved(self, git_shim, tmp_path, monkeypatch):
        git_shim.install("exit 0")
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/id_ed25519")
        gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert git_shim.env["GIT_SSH_COMMAND"] == "ssh -i /keys/id_ed25519"

    def test_remote_commands_get_stall_detection(self, git_shim):
        """Stall detection complements the timeout: a transfer that goes quiet dies in a minute
        instead of sitting out the (much longer) full timeout."""
        git_shim.install("exit 0")
        gitmod._run_git(["ls-remote", "https://example.invalid/x.git"], network=True)
        assert "http.lowSpeedLimit=1000" in git_shim.arg_lines[-1]
        assert "http.lowSpeedTime=60" in git_shim.arg_lines[-1]

    def test_local_commands_do_not_get_stall_detection(self, git_shim, tmp_path):
        git_shim.install("exit 0")
        gitmod._run_git(["-C", str(tmp_path), "rev-parse", "HEAD"])
        assert "lowSpeed" not in git_shim.arg_lines[-1]


class TestListRemoteRefs:
    def test_branches_return_name_to_sha_map(self, local_origin):
        origin, url, work = local_origin
        result = gitmod.list_remote_refs(url, "heads")
        assert result is not None
        assert set(result) == {"main", "feature"}
        assert result["main"] == _sha(origin, "refs/heads/main")
        assert result["feature"] == _sha(origin, "refs/heads/feature")

    def test_tags_lightweight_resolves_to_commit_sha(self, local_origin):
        origin, url, work = local_origin
        result = gitmod.list_remote_refs(url, "tags")
        assert result is not None
        # v0.1 is a lightweight tag pointing directly to a commit
        assert result["v0.1"] == _sha(origin, "refs/tags/v0.1")

    def test_tags_annotated_resolves_to_commit_sha_not_tag_object(self, local_origin):
        origin, url, work = local_origin
        result = gitmod.list_remote_refs(url, "tags")
        assert result is not None
        # The annotated tag object SHA and the dereferenced commit SHA differ;
        # list_remote_refs must return the commit (^{}) SHA so it matches git rev-parse HEAD.
        tag_object_sha = _sha(origin, "refs/tags/v1.0")
        commit_sha = _sha(origin, "refs/tags/v1.0^{}")
        assert tag_object_sha != commit_sha
        assert result["v1.0"] == commit_sha

    def test_names_match_list_remote_ref_names(self, local_origin):
        """list_remote_ref_names must be a sorted list of the keys from list_remote_refs."""
        origin, url, work = local_origin
        ref_map = gitmod.list_remote_refs(url, "tags")
        names = gitmod.list_remote_ref_names(url, "tags")
        assert names == sorted(ref_map)

    def test_empty_repo_returns_empty_dict(self, tmp_path):
        empty = tmp_path / "empty.git"
        _run("init", "--bare", "-q", str(empty))
        result = gitmod.list_remote_refs(f"file://{empty}", "heads")
        assert result == {}

    def test_bad_url_returns_none(self, tmp_path):
        result = gitmod.list_remote_refs(f"file://{tmp_path}/does_not_exist", "heads")
        assert result is None


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

    def test_plan_changes_does_not_fault_in_blobs(self, tmp_path, promisor_origin):
        """Planning a delta must be a pure tree comparison -- no blob contents required.

        Regression test for rename/copy detection (`git diff -M -C`) in plan_changes. Both
        score candidates by content, so on a blobless clone either one blocks on a promisor
        fetch of the candidate blobs, turning a HEAD advance of a handful of files into a stall
        of tens of seconds before any indexing began. The diff below trips both: an unpaired
        add and delete for -M to score against each other, and a modified file for -C to treat
        as a copy source. Asserting on the missing-object set (rather than on wall time or on
        the constructed argv) pins the actual property: a diff base whose blobs were never
        downloaded stays undownloaded.
        """
        origin_dir, url = promisor_origin
        work = tmp_path / "work"
        _run("clone", "-q", str(origin_dir), str(work))
        _commit_file(work, "kept.txt", b"unchanged\n", "init kept")
        _commit_file(work, "mod.txt", b"version one\n", "init mod")
        _commit_file(work, "gone.txt", b"doomed\n", "init gone")
        _run("-C", str(work), "push", "-q", "origin", "HEAD:main")
        old_sha = _sha(work, "HEAD")

        # Staged with `add -A` rather than _commit_file, which stages only the file it names
        # and would silently drop the modification and the deletion from the commit.
        (work / "mod.txt").write_bytes(b"version two\n")
        (work / "gone.txt").unlink()
        (work / "added.txt").write_bytes(b"brand new\n")
        _run("-C", str(work), "add", "-A")
        _run("-C", str(work), "-c", "user.email=test@example.com", "-c", "user.name=test",
             "commit", "-q", "-m", "advance head")
        _run("-C", str(work), "push", "-q", "origin", "HEAD:main")
        new_sha = _sha(work, "HEAD")

        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)

        # The clone checked out the new tip, so only the old side's distinct blobs are absent.
        # These are exactly the blobs rename/copy detection would have reached for.
        old_mod_blob = _blob_sha(dest, old_sha, "mod.txt")
        gone_blob = _blob_sha(dest, old_sha, "gone.txt")
        missing_before = _missing_objects(dest)
        assert {old_mod_blob, gone_blob} <= missing_before

        plan = gitmod.plan_changes(dest, old_sha, new_sha)

        assert plan.base_missing is False
        assert set(plan.index_paths) == {"mod.txt", "added.txt"}
        assert set(plan.delete_paths) == {"mod.txt", "gone.txt"}
        # Nothing was faulted in: the plan came from tree/OID comparison alone.
        assert _missing_objects(dest) == missing_before


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


def _ls_files(repo_dir: pathlib.Path) -> set[str]:
    return set(gitmod.iter_tracked_files(repo_dir))


def _is_detached(repo_dir: pathlib.Path) -> bool:
    """True if HEAD is not a symbolic ref (i.e. checked out at a raw commit, not a branch)."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
    ).returncode != 0


def _worktree_count(repo_dir: pathlib.Path) -> int:
    """Number of *linked* worktrees registered against `repo_dir` (excludes the primary
    working tree itself, which `git worktree list` always lists first)."""
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.count("\nworktree ")  # first entry has no leading blank line before it


class TestWorktrees:
    """`ensure_worktree` gives each concurrency slot its own working tree over one shared
    object store, which is what lets multiple refs of the same repo check out and index at
    once (see command.py's ref-level dispatch)."""

    def test_slot_zero_is_the_repo_dir_itself_and_needs_no_registration(self, tmp_path, local_origin):
        _origin, url, _work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)

        assert gitmod.ensure_worktree(dest, 0) == dest
        assert _worktree_count(dest) == 0  # no linked worktree was registered

    def test_higher_slot_registers_a_linked_worktree(self, tmp_path, local_origin):
        _origin, url, _work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)

        slot_dir = gitmod.ensure_worktree(dest, 1)

        assert slot_dir == gitmod.worktree_slot_dir(dest, 1)
        assert slot_dir != dest
        assert slot_dir.exists()
        assert (slot_dir / ".git").is_file()  # a worktree's gitdir is a FILE, not a directory
        assert _worktree_count(dest) == 1

    def test_reusing_a_slot_does_not_re_register_it(self, tmp_path, local_origin):
        """Slots persist across runs like the clone itself, so the per-ref cost on a later run
        is a single `checkout --force`, never a repeated `worktree add`."""
        _origin, url, _work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)

        first = gitmod.ensure_worktree(dest, 1)
        second = gitmod.ensure_worktree(dest, 1)

        assert first == second
        assert _worktree_count(dest) == 1  # still just one registration, not two

    def test_checkout_branch_in_a_worktree_is_detached_at_the_remote_tip(self, tmp_path, local_origin):
        _origin, url, work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)
        slot_dir = gitmod.ensure_worktree(dest, 1)

        # "main" (not the fixture's default/checked-out branch -- that's "feature", the last
        # branch checked out in `work` before the bare clone) has no local branch ref anywhere
        # in `dest` yet, only `origin/main`, so a local ref appearing after this checkout could
        # only have been created by it.
        gitmod.checkout_branch(slot_dir, "main")

        assert _is_detached(slot_dir)
        assert _sha(slot_dir, "HEAD") == _sha(work, "main")
        # No local "main" branch ref was created in the worktree (git refuses to check out the
        # same local branch in two worktrees, which is exactly why this must be detached).
        assert subprocess.run(
            ["git", "-C", str(slot_dir), "show-ref", "--verify", "-q", "refs/heads/main"],
            capture_output=True,
        ).returncode != 0

    def test_two_slots_hold_independent_checkouts_at_the_same_time(self, tmp_path, local_origin):
        """The whole point: two slots of the SAME clone can sit at two different refs
        simultaneously, each with its own working tree and index file."""
        _origin, url, _work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)

        slot_a = gitmod.ensure_worktree(dest, 1)
        slot_b = gitmod.ensure_worktree(dest, 2)
        gitmod.checkout_branch(slot_a, "main")
        gitmod.checkout_branch(slot_b, "feature")

        # main has only a.txt; feature has a.txt AND b.txt (committed on top of main).
        assert _ls_files(slot_a) == {"a.txt"}
        assert _ls_files(slot_b) == {"a.txt", "b.txt"}
        # Re-checking slot_a afterward proves the two working trees never interfered with
        # each other -- slot_a is still exactly where it was left.
        assert _ls_files(slot_a) == {"a.txt"}

    def test_gc_prunes_an_orphaned_worktree_registration(self, tmp_path, local_origin):
        """A crash between creating a slot directory and using it (or a manual `rm -rf` of a
        slot, since slots are documented as part of the safe-to-delete cache) leaves git's
        `.git/worktrees/<name>` registration pointing at a directory that no longer exists.
        `_git_gc`'s `worktree prune` call reclaims that registration; it must never touch a
        slot that is still actually present on disk."""
        _origin, url, _work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)
        slot_dir = gitmod.ensure_worktree(dest, 1)
        assert _worktree_count(dest) == 1

        shutil.rmtree(slot_dir)  # simulate the orphaning crash/manual delete
        gitmod._git_gc(dest)

        assert _worktree_count(dest) == 0

    def test_gc_does_not_prune_a_live_worktree(self, tmp_path, local_origin):
        _origin, url, _work = local_origin
        dest = tmp_path / "dest"
        gitmod._git_clone(url, dest)
        slot_dir = gitmod.ensure_worktree(dest, 1)
        gitmod.checkout_branch(slot_dir, "feature")

        gitmod._git_gc(dest)

        assert _worktree_count(dest) == 1
        assert slot_dir.exists()
        assert _ls_files(slot_dir) == {"a.txt", "b.txt"}


def _commit_dated(work_dir: pathlib.Path, name: str, content: bytes, message: str, date_iso: str) -> str:
    """Commit a file at an explicit author + committer date (ISO-8601) and return the SHA."""
    (work_dir / name).write_bytes(content)
    _run("-C", str(work_dir), "add", name)
    env_extra = f"GIT_COMMITTER_DATE={date_iso} GIT_AUTHOR_DATE={date_iso}"
    subprocess.run(
        f"{env_extra} git -C {work_dir} -c user.email=test@example.com -c user.name=test"
        f" commit -q -m '{message}'",
        shell=True, check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(work_dir), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def branch_history_repo(tmp_path):
    """A bare repo cloned locally with a branch of 5 commits spread across known dates.
    Returns (clone_dir, [sha_newest, ..., sha_oldest], [date_newest, ..., date_oldest]).
    The clone has an `origin/main` remote ref (bare clone with --bare).
    Dates are 5 days apart ending today-ish; exact values don't matter, only their order."""
    import datetime as dt

    work = tmp_path / "work"
    _run("init", "-q", "-b", "main", str(work))

    # Commit oldest-first (2024-01-02 → 2024-01-06) so git history is coherent.
    # shas[0] will be the oldest commit, shas[4] the newest.
    dates_iso = [
        f"2024-01-0{2 + i}T12:00:00+00:00" for i in range(5)
    ]  # 2024-01-02, 03, 04, 05, 06 (oldest → newest)
    shas = []
    for i, d in enumerate(dates_iso):
        sha = _commit_dated(work, f"f{i}.txt", f"content {i}".encode(), f"commit {i}", d)
        shas.append(sha)

    # shas[4]=newest(2024-01-06), shas[0]=oldest(2024-01-02).
    # list_branch_commits returns newest-first, so expected order = reversed(shas).
    shas_newest_first = list(reversed(shas))  # shas_newest_first[0] = newest

    bare = tmp_path / "bare.git"
    _run("clone", "--bare", "-q", str(work), str(bare))

    # Create a non-bare clone so we have an `origin/main` ref to walk.
    clone = tmp_path / "clone"
    _run("clone", "-q", str(bare), str(clone))

    return clone, shas_newest_first, dates_iso


class TestListBranchCommits:
    def test_returns_all_commits_when_floor_is_before_oldest(self, branch_history_repo):
        """With a floor earlier than all commits, every commit is returned newest-first."""
        import datetime as dt
        clone, shas, _ = branch_history_repo
        floor = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        result_shas = [sha for sha, _ in result]
        assert result_shas == shas, f"Expected {shas}, got {result_shas}"

    def test_far_past_floor_returns_all_commits(self, branch_history_repo):
        """Regression: a pre-1970 floor (e.g. from `since.age: 100y`) must return ALL commits.

        The original implementation used `git log --since`, whose approxidate parser silently
        returns nothing for dates before the Unix epoch -- so a `100y` floor (~1926) yielded zero
        commits and the branch fell back to tip-only. Filtering in Python fixes this."""
        import datetime as dt
        clone, shas, _ = branch_history_repo
        floor = dt.datetime(1900, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        result_shas = [sha for sha, _ in result]
        assert result_shas == shas, f"Expected all {len(shas)} commits, got {len(result_shas)}"

    def test_floor_at_exact_commit_date_is_inclusive(self, branch_history_repo):
        """A floor set to exactly a commit's committer date must INCLUDE that commit.

        Asserts the inclusive `cd >= floor` boundary (matching _effective_since_floor's `cd < floor`
        exclusion). git's `--since` was fuzzy/exclusive at the exact-timestamp boundary."""
        import datetime as dt
        clone, shas, _ = branch_history_repo
        # shas[4] is the oldest commit (2024-01-02T12:00:00+00:00). Floor at its exact date
        # must still include it -> all 5 commits returned.
        floor = dt.datetime(2024, 1, 2, 12, 0, 0, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        result_shas = [sha for sha, _ in result]
        assert shas[-1] in result_shas, "Commit at the exact floor date must be included"
        assert result_shas == shas, f"Expected all {len(shas)} commits, got {len(result_shas)}"

    def test_excludes_commits_before_floor(self, branch_history_repo):
        """Floor between commit 2 and 3 (oldest-to-newest numbering) returns only the newer half."""
        import datetime as dt
        clone, shas, _ = branch_history_repo
        # shas[0]=newest(2024-01-06), shas[4]=oldest(2024-01-02)
        # Floor 2024-01-04 includes shas[0], [1], [2] (dates 06, 05, 04).
        floor = dt.datetime(2024, 1, 4, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        result_shas = [sha for sha, _ in result]
        assert result_shas == shas[:3], f"Expected {shas[:3]}, got {result_shas}"

    def test_returns_empty_when_all_commits_before_floor(self, branch_history_repo):
        """Floor after all commits returns an empty list."""
        import datetime as dt
        clone, shas, _ = branch_history_repo
        floor = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        assert result == []

    def test_results_are_newest_first(self, branch_history_repo):
        """Commit dates in results must be non-increasing (newest-first order)."""
        import datetime as dt
        clone, shas, _ = branch_history_repo
        floor = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        dates = [cd for _, cd in result]
        assert dates == sorted(dates, reverse=True), "Results must be newest-first"

    def test_returns_empty_for_nonexistent_branch(self, branch_history_repo):
        """A nonexistent branch name degrades gracefully to an empty list."""
        import datetime as dt
        clone, _, _ = branch_history_repo
        floor = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "does-not-exist", floor)
        assert result == []

    def test_returns_empty_for_non_repo(self, tmp_path):
        """A path that is not a git repo degrades gracefully to an empty list."""
        import datetime as dt
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        floor = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(not_a_repo, "main", floor)
        assert result == []

    def test_first_parent_only_does_not_include_merged_commits(self, tmp_path):
        """With a merge commit, --first-parent returns only the mainline (not feature branch)."""
        import datetime as dt

        work = tmp_path / "work"
        _run("init", "-q", "-b", "main", str(work))
        _commit_dated(work, "a.txt", b"a", "mainline commit A", "2024-01-01T12:00:00+00:00")
        main_a_sha = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Feature branch: diverges from mainline A, adds 2 commits.
        _run("-C", str(work), "checkout", "-q", "-b", "feature")
        _commit_dated(work, "f1.txt", b"f1", "feature commit F1", "2024-01-02T12:00:00+00:00")
        _commit_dated(work, "f2.txt", b"f2", "feature commit F2", "2024-01-03T12:00:00+00:00")

        # Merge feature back into main.
        _run("-C", str(work), "checkout", "-q", "main")
        subprocess.run(
            ["git", "-C", str(work), "-c", "user.email=test@example.com", "-c", "user.name=test",
             "merge", "--no-ff", "-m", "Merge feature", "feature"],
            check=True, capture_output=True,
        )
        merge_sha = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        bare = tmp_path / "bare.git"
        _run("clone", "--bare", "-q", str(work), str(bare))
        clone = tmp_path / "clone"
        _run("clone", "-q", str(bare), str(clone))

        floor = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
        result = gitmod.list_branch_commits(clone, "main", floor)
        result_shas = [sha for sha, _ in result]

        # --first-parent: only merge commit + mainline A, NOT the feature branch's F1/F2.
        assert merge_sha in result_shas
        assert main_a_sha in result_shas
        assert len(result_shas) == 2, (
            f"Expected 2 first-parent commits (merge + A), got {len(result_shas)}: {result_shas}"
        )
