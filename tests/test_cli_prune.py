"""CLI tests for `sourcerer prune` option parsing and wiring: --wait/--host/--org/--repo
validation, and that the parsed values are forwarded to commands/prune/command.py's run()/
run_ref() unchanged. Every test stubs out command.run/run_ref so no real ES connection is
needed."""

# Standard packages
from unittest.mock import patch

# Third-party packages
from click.testing import CliRunner

# App packages
from sourcerer.cli import prune


def _invoke(*args):
    runner = CliRunner()
    return runner.invoke(prune, ["--url", "http://localhost:9200", *args], catch_exceptions=False)


class TestScopeFlagValidation:
    """--org requires --host, --repo requires --org (regardless of ordering the user typed
    them); a REPO_SPEC prune rejects the scope flags entirely since it's already scoped by
    the REPO_SPEC itself."""

    def test_org_without_host_errors(self):
        result = _invoke("--org", "acme")
        assert result.exit_code == 2
        assert "--org requires --host" in result.output

    def test_repo_without_org_errors(self):
        result = _invoke("--host", "github", "--repo", "widgets")
        assert result.exit_code == 2
        assert "--repo requires --org" in result.output

    def test_host_org_repo_together_is_valid(self):
        with patch("sourcerer.cli.prune_cmd.run") as fake_run:
            result = _invoke("--host", "github", "--org", "acme", "--repo", "widgets", "--dry-run")
            assert result.exit_code == 0, result.output
            fake_run.assert_called_once()
            kwargs = fake_run.call_args.kwargs
            assert kwargs["scope_host"] == "github"
            assert kwargs["scope_org"] == "acme"
            assert kwargs["scope_repo"] == "widgets"

    def test_scope_flags_rejected_with_repo_spec(self):
        result = _invoke("github/acme/widgets", "-b", "main", "--host", "github")
        assert result.exit_code == 2
        assert "REPO_SPEC" in result.output


class TestWaitFlag:
    def test_wait_forwarded_to_config_run(self):
        with patch("sourcerer.cli.prune_cmd.run") as fake_run:
            result = _invoke("--wait", "--dry-run")
            assert result.exit_code == 0, result.output
            assert fake_run.call_args.kwargs["wait"] is True

    def test_wait_forwarded_to_run_ref(self):
        with patch("sourcerer.cli.prune_cmd.run_ref") as fake_run_ref:
            result = _invoke("github/acme/widgets", "-b", "main", "--wait", "--dry-run")
            assert result.exit_code == 0, result.output
            assert fake_run_ref.call_args.kwargs["wait"] is True

    def test_wait_defaults_to_false(self):
        with patch("sourcerer.cli.prune_cmd.run") as fake_run:
            result = _invoke("--dry-run")
            assert result.exit_code == 0, result.output
            assert fake_run.call_args.kwargs["wait"] is False
