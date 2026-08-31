# sourcerer/indices.py
# Index-name constants and builders shared by the index and prune commands: every physical
# per-repo content index is named from these, and the refs index name is the same constant
# everywhere. Kept dependency-free (no ES, no click) so both command packages -- and anything
# that reads index names without touching a cluster -- can import it without pulling in either
# command's logic.
#
# v2 (multi-host): content index names carry a leading git.host segment, so the same org/repo on
# two different hosts lands in distinct backing indices. See sourcerer/hosts.py. Bumped to v3 for
# incremental (ref-addressed) branch indexing.

FILES_INDEX_PREFIX = "sourcerer-v3-files"
LINES_INDEX_PREFIX = "sourcerer-v3-lines"
REFS_INDEX = "sourcerer-v3-refs"

# Read aliases span all versioned backing indices of their respective kinds. Writes, updates,
# and deletes deliberately use the physical names above so a future index version can coexist
# without receiving mutations intended for the current version.
FILES_ALIAS = "sourcerer-files"
LINES_ALIAS = "sourcerer-lines"
REFS_ALIAS = "sourcerer-refs"

# How many git-identity segments each index `level` keeps, in order host, org, repo, commit.
# Governs sources[i].index.level (see specs/sourcerer-yml.md): "repo" is the historical default
# (host~org~repo), "commit" adds the commit for a per-commit index, and the coarser "org"/"host"
# levels collocate a whole org (or host) into one index to keep the shard count low.
_LEVEL_SEGMENTS = {"host": 1, "org": 2, "repo": 3, "commit": 4}


def _content_index(
    prefix: str, host: str, org: str, repo: str,
    commit: str | None = None, level: str = "repo", suffix: str | None = None,
) -> str:
    """Build a content (files/lines) index name at the given `level`, with an optional `^suffix`.

    `level` picks how many git-identity segments are kept (see _LEVEL_SEGMENTS): "repo" reproduces
    the historical `prefix~host~org~repo`; "commit" appends the commit for a per-commit index;
    "org"/"host" drop the finer segments to collocate a whole org (or host) in one index. A
    trailing `^{suffix}` further partitions a level into named siblings (e.g. `~repo^deploy`).

    Segments are lowercased to match the git.host/org/repo normalizer in the index mappings and the
    `_id` scheme, so the same identity always resolves to the same physical name. `suffix` is
    assumed already validated (lowercase, no delimiter/forbidden chars, empty == None) by config
    parsing; an empty string is treated as absent. A "commit" level requires a commit sha.

    A suffix may be configured as a version template ("{major}.{minor}.x"), but it is rendered to
    a concrete value at unit selection, so any brace reaching here is a bug -- rejected rather
    than silently creating an index named after the template."""
    n = _LEVEL_SEGMENTS[level]
    segments = [host, org, repo, commit]
    if n == 4 and not commit:
        raise ValueError("commit-level index name requires a commit sha")
    if suffix and ("{" in suffix or "}" in suffix):
        raise ValueError(f"index suffix {suffix!r} still contains an unrendered version variable")
    name = prefix + "~" + "~".join(s.lower() for s in segments[:n])  # type: ignore[union-attr]
    if suffix:
        name += "^" + suffix.lower()
    return name


def files_index(
    host: str, org: str, repo: str,
    commit: str | None = None, level: str = "repo", suffix: str | None = None,
) -> str:
    """Files index name for a source's `index.level`/`index.suffix`. The 3-arg call reproduces the
    historical repo-level name, e.g. sourcerer-v3-files~github~elastic~elasticsearch."""
    return _content_index(FILES_INDEX_PREFIX, host, org, repo, commit, level, suffix)


def lines_index(
    host: str, org: str, repo: str,
    commit: str | None = None, level: str = "repo", suffix: str | None = None,
) -> str:
    """Lines index name for a source's `index.level`/`index.suffix`. The 3-arg call reproduces the
    historical repo-level name, e.g. sourcerer-v3-lines~github~elastic~elasticsearch."""
    return _content_index(LINES_INDEX_PREFIX, host, org, repo, commit, level, suffix)


def files_index_pattern(host: str, org: str, repo: str) -> str:
    """Wildcard pattern matching ALL physical v3 files indices for a repo, regardless of
    index.level or index.suffix.

    Examples:
    - repo-level (default):  sourcerer-v3-files~github~elastic~logstash*  (matches that exact index)
    - commit-level:          sourcerer-v3-files~github~elastic~logstash*  (matches all commit shards)
    - suffixed:              sourcerer-v3-files~github~elastic~logstash*  (matches ^deploy, ^prod, …)

    Scoping a query to this pattern confines it to the current v3 physical indices and excludes any
    older-version files indices (e.g. sourcerer-v2-files~…) that may be reachable via the
    sourcerer-files alias during a version upgrade. Lowercased to match the normalizer applied by
    `_content_index`."""
    return f"{FILES_INDEX_PREFIX}~{host.lower()}~{org.lower()}~{repo.lower()}*"
