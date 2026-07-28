"""Unit tests for the git-host registry in sourcerer.hosts: id validation, defaults merge,
and URL substitution."""

# Third-party packages
import pytest

# App packages
from sourcerer.hosts import (
    KNOWN_HOSTS,
    Host,
    resolve_hosts,
    validate_host_id,
)


class TestValidateHostId:
    @pytest.mark.parametrize("host_id", ["github", "my_gitea", "gitea-2", "a.b", "host+x"])
    def test_valid(self, host_id):
        assert validate_host_id(host_id) == host_id

    @pytest.mark.parametrize("bad", list('~\\/*?"<>|:'))
    def test_forbidden_chars(self, bad):
        with pytest.raises(ValueError, match="forbidden character"):
            validate_host_id(f"git{bad}hub")

    def test_uppercase_rejected(self):
        with pytest.raises(ValueError, match="uppercase"):
            validate_host_id("GitHub")

    def test_whitespace_rejected(self):
        with pytest.raises(ValueError, match="whitespace"):
            validate_host_id("git hub")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            validate_host_id("")


class TestResolveHostsDefaults:
    def test_none_returns_all_builtins(self):
        hosts = resolve_hosts(None)
        assert set(hosts) == set(KNOWN_HOSTS)
        assert all(isinstance(h, Host) for h in hosts.values())

    def test_github_defaults(self):
        gh = resolve_hosts(None)["github"]
        assert gh.name == "GitHub"
        assert gh.url_template("clone") == "https://github.com/{git.org}/{git.repo}.git"
        assert set(gh.urls) == {"clone", "directory", "file", "line", "line_range"}

    def test_auto_skill_false_for_placeholder_builtins(self):
        hosts = resolve_hosts(None)
        for host_id in ("aws-codecommit", "azure-devops", "gcp-ssm"):
            assert hosts[host_id].auto_skill is False, f"{host_id} should have auto_skill=False"

    def test_auto_skill_true_for_standard_builtins(self):
        hosts = resolve_hosts(None)
        for host_id in ("github", "gitlab", "bitbucket", "codeberg", "gitea",
                        "launchpad", "sourcehut", "gcp-cloud-source"):
            assert hosts[host_id].auto_skill is True, f"{host_id} should have auto_skill=True"


class TestResolveHostsMerge:
    def test_override_one_leaf_keeps_other_defaults(self):
        hosts = resolve_hosts([{"id": "github", "name": "GH Enterprise"}])
        gh = hosts["github"]
        assert gh.name == "GH Enterprise"
        # unspecified fields keep their defaults
        assert gh.url_template("clone") == KNOWN_HOSTS["github"]["urls"]["clone"]
        assert gh.urls["file"] == KNOWN_HOSTS["github"]["urls"]["file"]

    def test_override_clone_url_only(self):
        hosts = resolve_hosts([{"id": "github", "urls": {"clone": "https://ghe.corp/{git.org}/{git.repo}.git"}}])
        gh = hosts["github"]
        assert gh.url_template("clone") == "https://ghe.corp/{git.org}/{git.repo}.git"

    def test_custom_host_requires_clone_url(self):
        with pytest.raises(ValueError, match="urls.clone is required"):
            resolve_hosts([{"id": "custom", "urls": {
                "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])

    def test_custom_host_requires_all_urls(self):
        with pytest.raises(ValueError, match="urls.line_range is required"):
            resolve_hosts([{"id": "custom",
                            "urls": {"clone": "u", "directory": "d", "file": "f", "line": "l"}}])

    def test_custom_host_name_defaults_to_id(self):
        hosts = resolve_hosts([{"id": "myhost", "urls": {
            "clone": "u", "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])
        assert hosts["myhost"].name == "myhost"

    def test_custom_host_auto_skill_defaults_to_true(self):
        hosts = resolve_hosts([{"id": "myhost", "urls": {
            "clone": "u", "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])
        assert hosts["myhost"].auto_skill is True

    def test_overriding_placeholder_builtin_preserves_auto_skill_false(self):
        # Overriding aws-codecommit via hosts: keeps auto_skill=False -- the user should define
        # a new host id (e.g. aws-codecommit-us-east-1) for their concrete deployment instead.
        hosts = resolve_hosts([{"id": "aws-codecommit",
                                "clone": {"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/{git.repo}"}}])
        assert hosts["aws-codecommit"].auto_skill is False

    def test_missing_id_raises(self):
        with pytest.raises(ValueError, match="'id' is required"):
            resolve_hosts([{"name": "x"}])

    def test_bad_id_raises(self):
        with pytest.raises(ValueError, match="uppercase"):
            resolve_hosts([{"id": "BadId", "urls": {
                "clone": "u", "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])

    def test_duplicate_id_raises(self):
        with pytest.raises(ValueError, match="duplicate host id"):
            resolve_hosts([{"id": "github"}, {"id": "github"}])


class TestUrlSubstitution:
    def test_clone_url(self):
        gh = resolve_hosts(None)["github"]
        assert gh.clone_url("elastic", "elasticsearch") == "https://github.com/elastic/elasticsearch.git"

    def test_url_template(self):
        gh = resolve_hosts(None)["github"]
        assert gh.url_template("file") == "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}"
