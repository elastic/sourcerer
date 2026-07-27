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
        assert gh.clone_protocol == "https"
        assert gh.clone_url_template == "https://github.com/{git.org}/{git.repo}.git"
        assert set(gh.links) == {"directory", "file", "line", "line_range"}

    def test_auto_skill_false_for_placeholder_builtins(self):
        hosts = resolve_hosts(None)
        for host_id in ("aws-codecommit", "azure-devops", "gcp-ssm"):
            assert hosts[host_id].auto_skill is False, f"{host_id} should have auto_skill=False"

    def test_auto_skill_true_for_standard_builtins(self):
        hosts = resolve_hosts(None)
        for host_id in ("github", "gitlab", "bitbucket", "codeberg", "gitea",
                        "forgejo", "launchpad", "sourcehut", "gcp-cloud-source"):
            assert hosts[host_id].auto_skill is True, f"{host_id} should have auto_skill=True"


class TestResolveHostsMerge:
    def test_override_one_leaf_keeps_other_defaults(self):
        hosts = resolve_hosts([{"id": "github", "name": "GH Enterprise"}])
        gh = hosts["github"]
        assert gh.name == "GH Enterprise"
        # unspecified fields keep their defaults
        assert gh.clone_url_template == KNOWN_HOSTS["github"]["clone"]["url"]
        assert gh.links["file"] == KNOWN_HOSTS["github"]["links"]["file"]

    def test_override_clone_url_only(self):
        hosts = resolve_hosts([{"id": "github", "clone": {"url": "https://ghe.corp/{git.org}/{git.repo}.git"}}])
        gh = hosts["github"]
        assert gh.clone_url_template == "https://ghe.corp/{git.org}/{git.repo}.git"
        assert gh.clone_protocol == "https"  # default preserved

    def test_custom_host_requires_clone_url(self):
        with pytest.raises(ValueError, match="clone.url is required"):
            resolve_hosts([{"id": "custom", "links": {
                "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])

    def test_custom_host_requires_all_links(self):
        with pytest.raises(ValueError, match="links.line_range is required"):
            resolve_hosts([{"id": "custom", "clone": {"url": "u"},
                            "links": {"directory": "d", "file": "f", "line": "l"}}])

    def test_custom_host_name_defaults_to_id(self):
        hosts = resolve_hosts([{"id": "myhost", "clone": {"url": "u"}, "links": {
            "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])
        assert hosts["myhost"].name == "myhost"

    def test_custom_host_auto_skill_defaults_to_true(self):
        hosts = resolve_hosts([{"id": "myhost", "clone": {"url": "u"}, "links": {
            "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])
        assert hosts["myhost"].auto_skill is True

    def test_overriding_placeholder_builtin_preserves_auto_skill_false(self):
        # Overriding aws-codecommit via hosts: keeps auto_skill=False -- the user should define
        # a new host id (e.g. aws-codecommit-us-east-1) for their concrete deployment instead.
        hosts = resolve_hosts([{"id": "aws-codecommit",
                                "clone": {"url": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/{git.repo}"}}])
        assert hosts["aws-codecommit"].auto_skill is False

    def test_ssh_protocol_rejected(self):
        with pytest.raises(ValueError, match="ssh"):
            resolve_hosts([{"id": "github", "clone": {"protocol": "ssh"}}])

    def test_bad_protocol_rejected(self):
        with pytest.raises(ValueError, match="clone.protocol"):
            resolve_hosts([{"id": "github", "clone": {"protocol": "ftp"}}])

    def test_missing_id_raises(self):
        with pytest.raises(ValueError, match="'id' is required"):
            resolve_hosts([{"name": "x"}])

    def test_bad_id_raises(self):
        with pytest.raises(ValueError, match="uppercase"):
            resolve_hosts([{"id": "BadId", "clone": {"url": "u"}, "links": {
                "directory": "d", "file": "f", "line": "l", "line_range": "lr"}}])

    def test_duplicate_id_raises(self):
        with pytest.raises(ValueError, match="duplicate host id"):
            resolve_hosts([{"id": "github"}, {"id": "github"}])


class TestUrlSubstitution:
    def test_clone_url(self):
        gh = resolve_hosts(None)["github"]
        assert gh.clone_url("elastic", "elasticsearch") == "https://github.com/elastic/elasticsearch.git"

    def test_link_template(self):
        gh = resolve_hosts(None)["github"]
        assert gh.link_template("file") == "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}"
