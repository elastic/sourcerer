"""Unit tests for the pure per-file/per-line doc builders in
sourcerer.commands.index.documents. Uses tmp_path for the small filesystem stat/read calls
(file size, symlink/executable bits, binary detection) -- no ES, no subprocess, no
multiprocessing pool."""

# Standard packages
import os
import pathlib
import stat

# App packages
from sourcerer.commands.index import documents
from sourcerer.commands.index.documents import (
    build_file_actions,
    build_file_doc,
    file_attributes,
    iter_line_docs,
)
from sourcerer.indices import files_index, lines_index


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
