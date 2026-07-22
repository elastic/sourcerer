# sourcerer/indices.py
# Index-name constants and builders shared by the index and prune commands: every physical
# per-repo content index is named from these, and the refs index name is the same constant
# everywhere. Kept dependency-free (no ES, no click) so both command packages -- and anything
# that reads index names without touching a cluster -- can import it without pulling in either
# command's logic.

FILES_INDEX_PREFIX = "sourcerer-v1-files"
LINES_INDEX_PREFIX = "sourcerer-v1-lines"
REFS_INDEX = "sourcerer-v1-refs"

# v2 (incremental) index names. The v2 path is an isolated evaluation schema: content is
# ref-addressed (no commit in the id) and the refs index is a mutable lookup index with one
# document per incremental branch. v1 constants above are preserved unchanged for the default
# snapshot path -- incremental writes never touch a v1 index.
FILES_INDEX_PREFIX_V2 = "sourcerer-v2-files"
LINES_INDEX_PREFIX_V2 = "sourcerer-v2-lines"
REFS_INDEX_V2 = "sourcerer-v2-refs"


def files_index(org: str, repo: str) -> str:
    """Return the per-repo files index name, e.g. sourcerer-v1-files~elastic~elasticsearch."""
    return f"{FILES_INDEX_PREFIX}~{org.lower()}~{repo.lower()}"


def lines_index(org: str, repo: str) -> str:
    """Return the per-repo lines index name, e.g. sourcerer-v1-lines~elastic~elasticsearch."""
    return f"{LINES_INDEX_PREFIX}~{org.lower()}~{repo.lower()}"


def files_index_v2(org: str, repo: str) -> str:
    """Per-repo v2 (incremental) files index, e.g. sourcerer-v2-files~elastic~elasticsearch."""
    return f"{FILES_INDEX_PREFIX_V2}~{org.lower()}~{repo.lower()}"


def lines_index_v2(org: str, repo: str) -> str:
    """Per-repo v2 (incremental) lines index, e.g. sourcerer-v2-lines~elastic~elasticsearch."""
    return f"{LINES_INDEX_PREFIX_V2}~{org.lower()}~{repo.lower()}"
