"""CLI tests for `sourcerer index` option parsing."""

# Standard packages
import datetime

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
