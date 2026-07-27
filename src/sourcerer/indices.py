# sourcerer/indices.py
# Index-name constants and builders shared by the index and prune commands: every physical
# per-repo content index is named from these, and the refs index name is the same constant
# everywhere. Kept dependency-free (no ES, no click) so both command packages -- and anything
# that reads index names without touching a cluster -- can import it without pulling in either
# command's logic.
#
# v2 (multi-host): content index names carry a leading git.host segment, so the same org/repo on
# two different hosts lands in distinct backing indices. See sourcerer/hosts.py.

FILES_INDEX_PREFIX = "sourcerer-v2-files"
LINES_INDEX_PREFIX = "sourcerer-v2-lines"
REFS_INDEX = "sourcerer-v2-refs"

# Read aliases span all versioned backing indices of their respective kinds. Writes, updates,
# and deletes deliberately use the physical names above so a future index version can coexist
# without receiving mutations intended for the current version.
FILES_ALIAS = "sourcerer-files"
LINES_ALIAS = "sourcerer-lines"
REFS_ALIAS = "sourcerer-refs"


def files_index(host: str, org: str, repo: str) -> str:
    """Per-repo files index name, e.g. sourcerer-v2-files~github~elastic~elasticsearch."""
    return f"{FILES_INDEX_PREFIX}~{host.lower()}~{org.lower()}~{repo.lower()}"


def lines_index(host: str, org: str, repo: str) -> str:
    """Per-repo lines index name, e.g. sourcerer-v2-lines~github~elastic~elasticsearch."""
    return f"{LINES_INDEX_PREFIX}~{host.lower()}~{org.lower()}~{repo.lower()}"
