import json
from unittest.mock import MagicMock, call

from elasticsearch import NotFoundError

from sourcerer.commands.setup.command import load_index_templates


def _not_found() -> NotFoundError:
    return NotFoundError("no matching indices", MagicMock(), {})


class TestLoadIndexTemplates:
    def test_loads_template_and_applies_its_alias_to_existing_indices(self, tmp_path):
        (tmp_path / "sourcerer-v1-files.json").write_text(json.dumps({
            "index_patterns": ["sourcerer-v1-files*"],
            "template": {"aliases": {"sourcerer-files": {}}},
        }))
        es = MagicMock()

        loaded = load_index_templates(es, tmp_path)

        assert loaded == ["sourcerer-v1-files"]
        es.indices.put_index_template.assert_called_once_with(
            name="sourcerer-v1-files",
            index_patterns=["sourcerer-v1-files*"],
            template={"aliases": {"sourcerer-files": {}}},
            _meta=None,
        )
        es.indices.update_aliases.assert_called_once_with(actions=[{
            "add": {
                "alias": "sourcerer-files",
                "index": "sourcerer-v1-files*",
            },
        }])

    def test_ignores_missing_indices_when_applying_template_alias(self, tmp_path):
        (tmp_path / "sourcerer-v1-files.json").write_text(json.dumps({
            "index_patterns": ["sourcerer-v1-files*"],
            "template": {"aliases": {"sourcerer-files": {}}},
        }))
        es = MagicMock()
        es.indices.update_aliases.side_effect = _not_found()

        assert load_index_templates(es, tmp_path) == ["sourcerer-v1-files"]
        assert es.indices.method_calls == [
            call.put_index_template(
                name="sourcerer-v1-files",
                index_patterns=["sourcerer-v1-files*"],
                template={"aliases": {"sourcerer-files": {}}},
                _meta=None,
            ),
            call.update_aliases(actions=[{
                "add": {
                    "alias": "sourcerer-files",
                    "index": "sourcerer-v1-files*",
                },
            }]),
        ]
