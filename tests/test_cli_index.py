"""CLI tests for `sourcerer index` option parsing."""

# Standard packages
import datetime
from unittest.mock import patch

# Third-party packages
import pytest
from click.testing import CliRunner

# App packages
from sourcerer.cli import index


_UTC = datetime.timezone.utc


class TestRetryWindowOption:
    """Tests for the --retry-window CLI flag."""

    def _invoke(self, *args):
        runner = CliRunner()
        # Invoke with --help to avoid needing a real ES connection; we just want to
        # test option parsing.  For parse-error cases we pass a real subcommand but
        # expect it to fail before any network I/O.
        return runner.invoke(index, list(args), catch_exceptions=False)

    def test_help_shows_retry_window(self):
        result = self._invoke("--help")
        assert result.exit_code == 0
        assert "--retry-window" in result.output

    def test_invalid_duration_exits_with_bad_parameter(self):
        # A bad value should produce a click.BadParameter error (exit code 2).
        # Don't mix --help: it short-circuits before the callback fires.
        runner = CliRunner()
        result = runner.invoke(index, ["--retry-window", "garbage"], catch_exceptions=False)
        assert result.exit_code == 2
        assert "retry-window" in result.output.lower() or "Error" in result.output

    def test_valid_duration_parses_without_error(self):
        # --retry-window 30m should parse cleanly (help exits 0).
        result = self._invoke("--retry-window", "30m", "--help")
        assert result.exit_code == 0

    def test_valid_duration_hours(self):
        result = self._invoke("--retry-window", "6h", "--help")
        assert result.exit_code == 0

    def test_valid_duration_days(self):
        result = self._invoke("--retry-window", "1d", "--help")
        assert result.exit_code == 0


class TestGitTimeoutOption:
    """Tests for --git-timeout / SOURCERER_GIT_TIMEOUT, which bounds each git command."""

    def _capture(self, argv, env=None):
        """Invoke `index` with a stubbed command.run and return the git_timeout it received."""
        runner = CliRunner()
        captured = {}

        def fake_run(*args, **kwargs):
            captured["git_timeout"] = kwargs.get("git_timeout")

        with patch("sourcerer.commands.index.command.run", side_effect=fake_run):
            result = runner.invoke(
                index,
                ["--url", "http://es:9200", "github/org/repo", *argv],
                env=env or {},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        return captured["git_timeout"]

    def test_help_shows_git_timeout(self):
        runner = CliRunner()
        result = runner.invoke(index, ["--help"])
        assert result.exit_code == 0
        assert "--git-timeout" in result.output

    def test_defaults_to_thirty_minutes(self):
        assert self._capture([]) == datetime.timedelta(minutes=30)

    def test_flag_is_parsed_as_a_duration(self):
        assert self._capture(["--git-timeout", "90s"]) == datetime.timedelta(seconds=90)

    def test_env_var_is_honored(self):
        assert self._capture([], env={"SOURCERER_GIT_TIMEOUT": "5m"}) == datetime.timedelta(minutes=5)

    def test_flag_overrides_env_var(self):
        got = self._capture(["--git-timeout", "10m"], env={"SOURCERER_GIT_TIMEOUT": "5m"})
        assert got == datetime.timedelta(minutes=10)

    @pytest.mark.parametrize("value", ["0", "none", "off"])
    def test_zero_disables_the_timeout(self, value):
        """A zero duration reaches set_git_timeout, which reads it as 'no limit'."""
        assert self._capture(["--git-timeout", value]) == datetime.timedelta(0)

    def test_invalid_duration_exits_with_bad_parameter(self):
        runner = CliRunner()
        result = runner.invoke(index, ["--git-timeout", "garbage"], catch_exceptions=False)
        assert result.exit_code == 2

    def test_it_reaches_the_runtime_setting(self):
        """End-to-end: the flag actually changes what git.py enforces."""
        from sourcerer.commands.index import runtime

        saved = (runtime._git_timeout_override, runtime._git_timeout_is_set)
        try:
            runner = CliRunner()
            with patch("sourcerer.commands.index.command.resolve_hosts", side_effect=SystemExit(0)):
                runner.invoke(
                    index,
                    ["--url", "http://es:9200", "github/org/repo", "--git-timeout", "45s"],
                    catch_exceptions=False,
                )
            assert runtime.git_timeout() == 45
        finally:
            runtime._git_timeout_override, runtime._git_timeout_is_set = saved


class TestInsecureOption:
    """Tests for the --insecure / ALLOW_INSECURE_TLS CLI option on the index command."""

    def test_help_shows_insecure_flag(self):
        runner = CliRunner()
        result = runner.invoke(index, ["--help"])
        assert result.exit_code == 0
        assert "--insecure" in result.output

    def test_insecure_env_var_true_resolves_to_true(self):
        """ALLOW_INSECURE_TLS=true in the environment sets insecure=True."""
        runner = CliRunner()
        captured = {}

        def fake_run(repo_spec, branch, tag, commit, url, api_key, username, password,
                     force=False, quiet=False, cache_dir=None, ephemeral=False,
                     retry_window=None, git_timeout=None, insecure=False):
            captured["insecure"] = insecure

        with patch("sourcerer.commands.index.command.run", side_effect=fake_run):
            result = runner.invoke(index, [
                "--url", "http://es:9200",
                "github/org/repo",
            ], env={"ALLOW_INSECURE_TLS": "true"}, catch_exceptions=False)

        assert captured.get("insecure") is True

    def test_insecure_env_var_absent_resolves_to_false(self):
        """Without ALLOW_INSECURE_TLS, insecure defaults to False."""
        runner = CliRunner()
        captured = {}

        def fake_run(repo_spec, branch, tag, commit, url, api_key, username, password,
                     force=False, quiet=False, cache_dir=None, ephemeral=False,
                     retry_window=None, git_timeout=None, insecure=False):
            captured["insecure"] = insecure

        with patch("sourcerer.commands.index.command.run", side_effect=fake_run):
            result = runner.invoke(index, [
                "--url", "http://es:9200",
                "github/org/repo",
            ], env={}, catch_exceptions=False)

        assert captured.get("insecure") is False
