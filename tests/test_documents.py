"""Unit tests for the pure per-file/per-line doc builders in
sourcerer.commands.index.documents. Uses tmp_path for the small filesystem stat/read calls
(file size, symlink/executable bits, binary detection) -- no ES, no subprocess, no
multiprocessing pool."""

# Standard packages
import os
import pathlib
import stat
from unittest.mock import MagicMock, patch

# App packages
from sourcerer.commands.index import documents
from sourcerer.commands.index.documents import (
    build_file_actions,
    build_file_actions_v2,
    build_file_doc,
    build_file_doc_v2,
    file_attributes,
    index_paths_v2,
    iter_line_docs,
    iter_line_docs_v2,
)
from sourcerer.indices import (
    files_index,
    files_index_v2,
    lines_index,
    lines_index_v2,
)
from sourcerer.utils import build_ref_key, make_doc_id


def _set_worker_ctx(org: str, repo: str, commit_sha: str, repo_dir) -> None:
    # Populate the module-level worker context directly rather than going through
    # _init_worker, which also installs a SIGINT-ignoring signal handler meant for a
    # ProcessPoolExecutor worker -- not something a test running in the main process
    # should leave behind for the rest of the pytest session.
    documents._WORKER_CTX.update(
        org=org, repo=repo, commit_sha=commit_sha, repo_dir=pathlib.Path(repo_dir)
    )


class TestBuildFileDoc:
    def test_nested_dir(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "src/pkg/a.txt", p)
        assert doc["file"]["directory"] == "src/pkg"
        assert doc["file"]["name"] == "a.txt"
        assert doc["file"]["extension"] == "txt"

    def test_root_file_has_empty_directory(self, tmp_path):
        p = tmp_path / "README"
        p.write_text("hi")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "README", p)
        assert doc["file"]["directory"] == ""

    def test_no_extension_is_none(self, tmp_path):
        p = tmp_path / "Makefile"
        p.write_text("all:")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "Makefile", p)
        assert doc["file"]["extension"] is None

    def test_size_from_stat(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello world")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        assert doc["file"]["size"] == len(b"hello world")

    def test_deterministic_id(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        id1, _ = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        id2, _ = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        assert id1 == id2

    def test_git_fields(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        assert doc["git"] == {"org": "acme", "repo": "widgets", "commit": "deadbeef"}


class TestSnapshotDocsUnchanged:
    """Guard that the v1 (snapshot) builders keep commit-addressed identity and never grow
    v2's ref fields -- the isolation half of INV-009."""

    def test_snapshot_file_doc_keeps_commit_and_no_ref_key(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hi")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        assert doc["git"]["commit"] == "deadbeef"
        assert "ref_key" not in doc["git"]
        assert "ref" not in doc["git"]

    def test_snapshot_id_depends_on_commit(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hi")
        id1, _ = build_file_doc("acme", "widgets", "c0ffee", "a.txt", p)
        id2, _ = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        assert id1 != id2  # snapshot content is per-commit


class TestBuildFileDocV2:
    def test_source_has_ref_fields_and_no_commit(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        _id, doc = build_file_doc_v2("acme", "widgets", "main", "src/a.txt", p)
        assert doc["git"]["ref"] == "main"
        assert doc["git"]["ref_type"] == "branch"
        assert doc["git"]["ref_key"] == build_ref_key("acme", "widgets", "main")
        assert "commit" not in doc["git"]
        assert doc["file"]["directory"] == "src"
        assert doc["file"]["name"] == "a.txt"

    def test_org_repo_lowercased_in_source_and_id(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        _id, doc = build_file_doc_v2("ACME", "Widgets", "main", "a.txt", p)
        assert doc["git"]["org"] == "acme"
        assert doc["git"]["repo"] == "widgets"
        assert _id == make_doc_id("acme", "widgets", "branch", "main", "a.txt")

    def test_id_is_ref_addressed_not_commit_addressed(self, tmp_path):
        # INV-003: identity is (org, repo, "branch", ref, path) with no commit -- two runs at
        # two different commit SHAs for the same branch/path collapse to one id.
        p = tmp_path / "a.txt"
        p.write_text("hello")
        id1, _ = build_file_doc_v2("acme", "widgets", "main", "a.txt", p)
        id2, _ = build_file_doc_v2("acme", "widgets", "main", "a.txt", p)
        assert id1 == id2

    def test_branch_case_sensitivity_changes_id(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        lower, _ = build_file_doc_v2("acme", "widgets", "main", "a.txt", p)
        upper, _ = build_file_doc_v2("acme", "widgets", "Main", "a.txt", p)
        assert lower != upper


class TestIterLineDocsV2:
    def test_line_id_is_ref_addressed(self):
        docs = list(iter_line_docs_v2("acme", "widgets", "main", "a.txt", "one\ntwo"))
        first_id, first_doc = docs[0]
        assert first_id == make_doc_id("acme", "widgets", "branch", "main", "a.txt", "1")
        assert "commit" not in first_doc["git"]
        assert first_doc["git"]["ref"] == "main"

    def test_line_numbering_and_content(self):
        docs = list(iter_line_docs_v2("acme", "widgets", "main", "a.txt", "one\ntwo\nthree"))
        assert [d["line"]["number"] for _i, d in docs] == [1, 2, 3]
        assert [d["line"]["content"] for _i, d in docs] == ["one", "two", "three"]


class TestBuildFileActionsV2:
    def test_text_file_yields_file_and_line_actions(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\n")
        actions = build_file_actions_v2("acme", "widgets", "main", tmp_path, "a.txt")
        assert actions[0]["_index"] == files_index_v2("acme", "widgets")
        line_actions = [a for a in actions if a["_index"] == lines_index_v2("acme", "widgets")]
        assert len(line_actions) == 2

    def test_missing_path_yields_no_actions(self, tmp_path):
        # A path absent from the checked-out tree must not create a phantom document.
        actions = build_file_actions_v2("acme", "widgets", "main", tmp_path, "gone.txt")
        assert actions == []

    def test_binary_file_yields_only_file_doc(self, tmp_path):
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02binary")
        actions = build_file_actions_v2("acme", "widgets", "main", tmp_path, "b.bin")
        assert len(actions) == 1
        assert actions[0]["_index"] == files_index_v2("acme", "widgets")


class TestIndexPathsV2:
    def test_emits_actions_only_for_supplied_paths(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\n")
        (tmp_path / "b.txt").write_text("two\n")
        (tmp_path / "c.txt").write_text("three\n")  # present but NOT supplied
        captured = []

        def fake_parallel_bulk(es, actions, **kwargs):
            for a in actions:
                captured.append(a)
                yield True, {"index": {"_index": a["_index"], "_id": a["_id"]}}

        with patch.object(documents, "es_parallel_bulk", side_effect=fake_parallel_bulk):
            files_count, lines_count = index_paths_v2(
                MagicMock(), "acme", "widgets", tmp_path, "main", ["a.txt", "b.txt"],
            )
        paths = {a["_source"]["file"]["path"] for a in captured}
        assert paths == {"a.txt", "b.txt"}  # c.txt never indexed
        assert files_count == 2
        assert lines_count == 2


class TestIterLineDocs:
    def test_line_numbering_starts_at_one(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo\nthree"))
        numbers = [d["line"]["number"] for _id, d in docs]
        assert numbers == [1, 2, 3]

    def test_line_content_preserved(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo"))
        contents = [d["line"]["content"] for _id, d in docs]
        assert contents == ["one", "two"]

    def test_empty_content_yields_no_docs(self):
        assert list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "")) == []

    def test_trailing_newline_does_not_add_extra_line(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo\n"))
        assert len(docs) == 2


class TestFileAttributes:
    def test_plain_file_has_no_attributes(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hi")
        assert file_attributes(p) == []

    def test_executable_file(self, tmp_path):
        p = tmp_path / "run.sh"
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        assert file_attributes(p) == ["executable"]

    def test_symlink(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("hi")
        link = tmp_path / "link.txt"
        os.symlink(target, link)
        assert file_attributes(link) == ["symlink"]


class TestBuildFileActions:
    def test_text_file_yields_file_and_line_actions(self, tmp_path):
        (tmp_path / "a.txt").write_text("one\ntwo\n")
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path)
        actions = build_file_actions("a.txt")
        assert actions[0]["_index"] == files_index("acme", "widgets")
        line_actions = [a for a in actions if a["_index"] == lines_index("acme", "widgets")]
        assert len(line_actions) == 2

    def test_binary_file_yields_only_file_doc(self, tmp_path):
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02binarydata")
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path)
        actions = build_file_actions("b.bin")
        assert len(actions) == 1
        assert actions[0]["_index"] == files_index("acme", "widgets")

    def test_nul_beyond_8kb_window_is_treated_as_text(self, tmp_path):
        # Binary detection only sniffs the first 8 KB -- a NUL byte after that point should
        # not trigger binary classification.
        content = ("a" * 8192) + "\x00"
        (tmp_path / "c.txt").write_text(content)
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path)
        actions = build_file_actions("c.txt")
        line_actions = [a for a in actions if a["_index"] == lines_index("acme", "widgets")]
        assert len(line_actions) >= 1
