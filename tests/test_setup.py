import json
from unittest.mock import MagicMock, call

import pytest
from elasticsearch import NotFoundError

from sourcerer.commands.setup.command import (
    build_host_citation_referenced_content,
    load_index_templates,
    load_kibana_workflows,
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


class TestBuildHostCitationReferencedContent:
    def test_required_fields_and_url_templates(self):
        gh = resolve_hosts(None)["github"]
        item = build_host_citation_referenced_content(gh)
        assert item["name"] == "sourcerer-code-citations-github"
        assert item["relativePath"] == "./sourcerer-code-citations-github"
        # the four link templates appear in the content
        for kind in ("directory", "file", "line", "line_range"):
            assert gh.urls[kind] in item["content"]

    def test_one_item_per_auto_skill_host_and_deterministic_names(self):
        hosts = resolve_hosts(None)
        items = [
            build_host_citation_referenced_content(hosts[h])
            for h in sorted(hosts) if hosts[h].auto_skill
        ]
        names = [i["name"] for i in items]
        assert len(names) == len(set(names))  # unique
        assert "sourcerer-code-citations-gitlab" in names
        # placeholder hosts are excluded from auto_skill
        for excluded in ("aws-codecommit", "azure-devops", "gcp-ssm"):
            assert f"sourcerer-code-citations-{excluded}" not in names

    def test_placeholder_hosts_excluded_no_auto_skill(self):
        hosts = resolve_hosts(None)
        for host_id in ("aws-codecommit", "azure-devops", "gcp-ssm"):
            assert not hosts[host_id].auto_skill

    def test_per_deployment_custom_host_gets_auto_skill(self):
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
        item = build_host_citation_referenced_content(hosts["aws-codecommit-us-east-1"])
        assert item["name"] == "sourcerer-code-citations-aws-codecommit-us-east-1"
        assert item["relativePath"] == "./sourcerer-code-citations-aws-codecommit-us-east-1"

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
        item = build_host_citation_referenced_content(hosts["my_gitea"])
        assert item["name"] == "sourcerer-code-citations-my_gitea"
        assert item["relativePath"] == "./sourcerer-code-citations-my_gitea"


def _ok_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return resp


class TestLoadKibanaWorkflows:
    WORKFLOW_YAML = "version: \"1\"\nname: sourcerer-x\ndescription: test\n"

    def _make_session(self, search_results: list[dict]) -> MagicMock:
        session = MagicMock()
        session.get.return_value = _ok_response({"results": search_results})
        session.post.return_value = _ok_response({"id": "wf-new"})
        session.put.return_value = _ok_response({"id": "wf-123"})
        return session

    def test_creates_workflow_when_not_found(self, tmp_path):
        (tmp_path / "sourcerer-x.yml").write_text(self.WORKFLOW_YAML, encoding="utf-8")
        session = self._make_session(search_results=[])

        loaded = load_kibana_workflows(session, "http://kb:5601", tmp_path)

        assert loaded == ["sourcerer-x"]
        # GET was called once to look up by name
        session.get.assert_called_once_with(
            "http://kb:5601/api/workflows",
            params={"query": "sourcerer-x", "size": 100},
        )
        # create POST was called with the raw yaml text
        session.post.assert_called_once_with(
            "http://kb:5601/api/workflows/workflow",
            json={"yaml": self.WORKFLOW_YAML},
        )
        session.put.assert_not_called()

    def test_updates_workflow_when_name_matches(self, tmp_path):
        (tmp_path / "sourcerer-x.yml").write_text(self.WORKFLOW_YAML, encoding="utf-8")
        session = self._make_session(search_results=[{"id": "wf-123", "name": "sourcerer-x"}])

        loaded = load_kibana_workflows(session, "http://kb:5601", tmp_path)

        assert loaded == ["sourcerer-x"]
        session.put.assert_called_once_with(
            "http://kb:5601/api/workflows/workflow/wf-123",
            json={"yaml": self.WORKFLOW_YAML},
        )
        session.post.assert_not_called()

    def test_exact_name_match_guards_against_fuzzy_near_miss(self, tmp_path):
        """A near-miss name from the fuzzy search must not trigger a PUT to the wrong id."""
        (tmp_path / "sourcerer-x.yml").write_text(self.WORKFLOW_YAML, encoding="utf-8")
        # list returns a different workflow with a similar name
        session = self._make_session(search_results=[{"id": "wf-other", "name": "sourcerer-x-2"}])

        loaded = load_kibana_workflows(session, "http://kb:5601", tmp_path)

        assert loaded == ["sourcerer-x"]
        session.put.assert_not_called()
        # create POST called because there was no exact-name match
        session.post.assert_called_once_with(
            "http://kb:5601/api/workflows/workflow",
            json={"yaml": self.WORKFLOW_YAML},
        )

    def test_raises_when_no_workflow_files_found(self, tmp_path):
        session = MagicMock()
        with pytest.raises(FileNotFoundError, match="No workflow definitions"):
            load_kibana_workflows(session, "http://kb:5601", tmp_path)

    def test_raises_when_yaml_missing_name(self, tmp_path):
        (tmp_path / "no-name.yml").write_text("version: \"1\"\ndescription: oops\n", encoding="utf-8")
        session = MagicMock()
        with pytest.raises(ValueError, match="missing a 'name:' field"):
            load_kibana_workflows(session, "http://kb:5601", tmp_path)
