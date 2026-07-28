import json
from unittest.mock import MagicMock, call

from elasticsearch import NotFoundError

from sourcerer.commands.setup.command import (
    build_host_citation_skill,
    host_citation_skill_id,
    load_index_templates,
)
from sourcerer.hosts import resolve_hosts


def _not_found() -> NotFoundError:
    return NotFoundError("no matching indices", MagicMock(), {})


class TestLoadIndexTemplates:
    def test_loads_template_and_applies_its_alias_to_existing_indices(self, tmp_path):
        (tmp_path / "sourcerer-v2-files.json").write_text(json.dumps({
            "index_patterns": ["sourcerer-v2-files*"],
            "template": {"aliases": {"sourcerer-files": {}}},
        }))
        es = MagicMock()

        loaded = load_index_templates(es, tmp_path)

        assert loaded == ["sourcerer-v2-files"]
        es.indices.put_index_template.assert_called_once_with(
            name="sourcerer-v2-files",
            index_patterns=["sourcerer-v2-files*"],
            template={"aliases": {"sourcerer-files": {}}},
            _meta=None,
        )
        es.indices.update_aliases.assert_called_once_with(actions=[{
            "add": {
                "alias": "sourcerer-files",
                "index": "sourcerer-v2-files*",
            },
        }])

    def test_ignores_missing_indices_when_applying_template_alias(self, tmp_path):
        (tmp_path / "sourcerer-v2-files.json").write_text(json.dumps({
            "index_patterns": ["sourcerer-v2-files*"],
            "template": {"aliases": {"sourcerer-files": {}}},
        }))
        es = MagicMock()
        es.indices.update_aliases.side_effect = _not_found()

        assert load_index_templates(es, tmp_path) == ["sourcerer-v2-files"]
        assert es.indices.method_calls == [
            call.put_index_template(
                name="sourcerer-v2-files",
                index_patterns=["sourcerer-v2-files*"],
                template={"aliases": {"sourcerer-files": {}}},
                _meta=None,
            ),
            call.update_aliases(actions=[{
                "add": {
                    "alias": "sourcerer-files",
                    "index": "sourcerer-v2-files*",
                },
            }]),
        ]


class TestBuildHostCitationSkill:
    def test_id_and_name_and_templates(self):
        gh = resolve_hosts(None)["github"]
        skill = build_host_citation_skill(gh)
        assert skill["id"] == "sourcerer-code-citations-github"
        assert skill["name"] == "sourcerer-code-citations-github"
        # the four link templates appear in the content
        for kind in ("directory", "file", "line", "line_range"):
            assert gh.urls[kind] in skill["content"]
        assert skill["tool_ids"] == []

    def test_one_skill_per_auto_skill_host_and_deterministic_id(self):
        hosts = resolve_hosts(None)
        skills = [build_host_citation_skill(hosts[h]) for h in sorted(hosts) if hosts[h].auto_skill]
        ids = [s["id"] for s in skills]
        assert len(ids) == len(set(ids))  # unique
        assert host_citation_skill_id("gitlab") == "sourcerer-code-citations-gitlab"
        assert "sourcerer-code-citations-gitlab" in ids
        # placeholder hosts are excluded
        for excluded in ("aws-codecommit", "azure-devops", "gcp-ssm"):
            assert host_citation_skill_id(excluded) not in ids

    def test_placeholder_hosts_excluded_no_auto_skill(self):
        hosts = resolve_hosts(None)
        for host_id in ("aws-codecommit", "azure-devops", "gcp-ssm"):
            assert not hosts[host_id].auto_skill

    def test_per_deployment_custom_host_gets_auto_skill(self):
        # A user-defined per-deployment entry (e.g. aws-codecommit-us-east-1) has auto_skill=True
        # and therefore gets a citation skill generated for it.
        hosts = resolve_hosts([{
            "id": "aws-codecommit-us-east-1",
            "name": "AWS CodeCommit (us-east-1)",
            "urls": {
                "clone": "https://git-codecommit.us-east-1.amazonaws.com/v1/repos/{git.repo}",
                "directory": "https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.directory}",
                "file": "https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}",
                "line": "https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}?lines={line.number}",
                "line_range": "https://us-east-1.console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}?lines={line.number_start}-{line.number_end}",
            },
        }])
        assert hosts["aws-codecommit-us-east-1"].auto_skill is True
        skill = build_host_citation_skill(hosts["aws-codecommit-us-east-1"])
        assert skill["id"] == "sourcerer-code-citations-aws-codecommit-us-east-1"

    def test_custom_host_uses_its_id(self):
        hosts = resolve_hosts([{
            "id": "my_gitea",
            "name": "My Gitea",
            "urls": {
                "clone": "https://g/{git.org}/{git.repo}.git",
                "directory": "https://g/{git.org}/{git.repo}/{file.directory}",
                "file": "https://g/{git.org}/{git.repo}/{file.path}",
                "line": "https://g/{git.org}/{git.repo}/{file.path}#L{line.number}",
                "line_range": "https://g/{git.org}/{git.repo}/{file.path}#L{line.number_start}",
            },
        }])
        skill = build_host_citation_skill(hosts["my_gitea"])
        assert skill["id"] == "sourcerer-code-citations-my_gitea"
        assert skill["name"] == "sourcerer-code-citations-my_gitea"
