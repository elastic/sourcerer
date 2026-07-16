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


def _set_worker_ctx(org: str, repo: str, commit_sha: str, repo_dir, symlink_paths=frozenset()) -> None:
    # Populate the module-level worker context directly rather than going through
    # _init_worker, which also installs a SIGINT-ignoring signal handler meant for a
    # ProcessPoolExecutor worker -- not something a test running in the main process
    # should leave behind for the rest of the pytest session.
    documents._WORKER_CTX.update(
        org=org, repo=repo, commit_sha=commit_sha, repo_dir=pathlib.Path(repo_dir),
        symlink_paths=symlink_paths,
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

    def test_plain_file_omits_attributes_target_path_target_size(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("hello")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "a.txt", p)
        assert "attributes" not in doc["file"]
        assert "target_path" not in doc["file"]
        assert "target_size" not in doc["file"]

    def test_binary_flag_sets_binary_attribute(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"\x00data")
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "a.bin", p, binary=True)
        assert doc["file"]["attributes"] == ["binary"]

    def test_symlink_sets_target_path_and_target_size(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("hello world")
        link = tmp_path / "link.txt"
        os.symlink(target, link)
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "link.txt", link)
        assert doc["file"]["target_path"] == os.readlink(link)
        assert doc["file"]["target_size"] == len(b"hello world")
        assert doc["file"]["attributes"] == ["symlink"]

    def test_symlink_size_is_lstat(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("hello world")
        link = tmp_path / "link.txt"
        os.symlink(target, link)
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "link.txt", link)
        assert doc["file"]["size"] == link.lstat().st_size

    def test_broken_symlink_has_target_path_but_no_target_size(self, tmp_path):
        link = tmp_path / "dangling.txt"
        os.symlink(tmp_path / "nonexistent.txt", link)
        _id, doc = build_file_doc("acme", "widgets", "deadbeef", "dangling.txt", link)
        assert doc["file"]["target_path"] == str(tmp_path / "nonexistent.txt")
        assert "target_size" not in doc["file"]
        assert doc["file"]["attributes"] == ["symlink"]


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

    def test_size_threaded_to_all_line_docs(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo", size=42))
        assert all(d["file"]["size"] == 42 for _id, d in docs)

    def test_target_path_threaded_to_all_line_docs(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo", target_path="../real.txt"))
        assert all(d["file"]["target_path"] == "../real.txt" for _id, d in docs)

    def test_target_size_threaded_to_all_line_docs(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo", target_size=100))
        assert all(d["file"]["target_size"] == 100 for _id, d in docs)

    def test_attributes_threaded_to_all_line_docs(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one\ntwo", attributes=["symlink"]))
        assert all(d["file"]["attributes"] == ["symlink"] for _id, d in docs)

    def test_no_optional_fields_when_omitted(self):
        docs = list(iter_line_docs("acme", "widgets", "deadbeef", "a.txt", "one"))
        for _id, d in docs:
            assert "size" not in d["file"]
            assert "target_path" not in d["file"]
            assert "target_size" not in d["file"]
            assert "attributes" not in d["file"]


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

    def test_binary_flag_plain_file(self, tmp_path):
        p = tmp_path / "a.bin"
        p.write_bytes(b"\x00data")
        assert file_attributes(p, binary=True) == ["binary"]

    def test_binary_flag_executable(self, tmp_path):
        p = tmp_path / "run"
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        assert file_attributes(p, binary=True) == ["binary", "executable"]

    def test_binary_flag_symlink(self, tmp_path):
        target = tmp_path / "target.bin"
        target.write_bytes(b"\x00data")
        link = tmp_path / "link.bin"
        os.symlink(target, link)
        assert file_attributes(link, binary=True) == ["binary", "symlink"]

    def test_attributes_sorted_lexicographically(self, tmp_path):
        p = tmp_path / "run.sh"
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        result = file_attributes(p, binary=True)
        assert result == sorted(result)


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

    def test_binary_file_has_binary_attribute(self, tmp_path):
        (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02binarydata")
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path)
        actions = build_file_actions("b.bin")
        assert actions[0]["_source"]["file"]["attributes"] == ["binary"]

    def test_text_file_actions_carry_size(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("one\ntwo\n")
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path)
        actions = build_file_actions("a.txt")
        expected_size = p.lstat().st_size
        for action in actions:
            assert action["_source"]["file"]["size"] == expected_size

    def test_symlink_to_text_carries_target_fields_on_all_actions(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("hello\nworld\n")
        link = tmp_path / "link.txt"
        os.symlink(target, link)
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path, symlink_paths=frozenset({"link.txt"}))
        actions = build_file_actions("link.txt")
        # Both files and lines docs should carry target_path and target_size
        for action in actions:
            assert action["_source"]["file"]["target_path"] == os.readlink(link)
            assert action["_source"]["file"]["target_size"] == len(b"hello\nworld\n")
        # Line docs should be produced (content is dereferenced) and carry the same attributes
        line_actions = [a for a in actions if a["_index"] == lines_index("acme", "widgets")]
        assert len(line_actions) == 2
        assert all(a["_source"]["file"]["attributes"] == ["symlink"] for a in line_actions)

    def test_broken_symlink_yields_only_file_doc_with_target_path(self, tmp_path):
        link = tmp_path / "dangling.txt"
        os.symlink(tmp_path / "nonexistent.txt", link)
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path, symlink_paths=frozenset({"dangling.txt"}))
        actions = build_file_actions("dangling.txt")
        assert len(actions) == 1
        assert actions[0]["_index"] == files_index("acme", "widgets")
        file_fields = actions[0]["_source"]["file"]
        assert "target_path" in file_fields
        assert "target_size" not in file_fields

    def test_core_symlinks_false_plain_text_file_treated_as_symlink(self, tmp_path):
        # Simulate core.symlinks=false: git checks out a symlink as a plain text file whose
        # content is the target path. The file is NOT a filesystem symlink, but symlink_paths
        # (from git ls-files --stage mode 120000) tells us it's a git symlink.
        target = tmp_path / "AGENTS.md"
        target.write_text("agent content\n")
        fake_link = tmp_path / "CLAUDE.md"
        fake_link.write_text("AGENTS.md")  # content = target path, no newline (git blob)
        _set_worker_ctx("acme", "widgets", "deadbeef", tmp_path, symlink_paths=frozenset({"CLAUDE.md"}))
        actions = build_file_actions("CLAUDE.md")
        file_action = actions[0]
        assert file_action["_source"]["file"]["attributes"] == ["symlink"]
        assert file_action["_source"]["file"]["target_path"] == "AGENTS.md"
        assert file_action["_source"]["file"]["target_size"] == len(b"agent content\n")
        assert file_action["_source"]["file"]["size"] == len(b"AGENTS.md")
        # Line doc produced from file content ("AGENTS.md" is the one line)
        line_actions = [a for a in actions if a["_index"] == lines_index("acme", "widgets")]
        assert len(line_actions) == 1
        assert line_actions[0]["_source"]["file"]["target_path"] == "AGENTS.md"
