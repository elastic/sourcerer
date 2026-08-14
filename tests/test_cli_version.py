"""Tests for the `sourcerer --version` CLI flag. Not to be confused with
tests/test_version.py, which covers the version-aware ref pattern DSL."""

# Standard packages
from importlib.metadata import version

# Third-party packages
from click.testing import CliRunner

# App packages
from sourcerer.cli import cli


def test_version_flag_prints_installed_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == version("sourcerer")
