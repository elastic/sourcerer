# Standard packages
import hashlib
import warnings

# Elastic packages
from elasticsearch import Elasticsearch, ApiError, TransportError
from elasticsearch.helpers import BulkIndexError

# Joins the fields of a document id before hashing. NUL is the only byte that cannot
# appear in an org, repo, git ref, or file path (paths may contain any byte but NUL and
# "/"), so it is an unambiguous separator. Mirrors `git ls-files -z` in index.py.
ID_SEP = b"\x00"

# Digest size in bytes -> 2x hex chars in the id. These ids are content addresses, not
# security tokens, so only accidental-collision resistance matters: 16 bytes (128 bits,
# 32 hex chars) puts the birthday bound at ~2^64 docs -- far beyond any realistic index.
ID_DIGEST_SIZE = 16


def build_ref_key(host: str, org: str, repo: str, ref_type: str, ref: str) -> str:
    """Deterministic `_id` string for incremental join docs:
    `{host}~{org}~{repo}~{ref_type}~{ref}` (host/org/repo lowercased, ref case-preserved).

    `ref_type` is "branch" or "tag"; folding it in keeps a same-named branch and tag in delta
    mode as two distinct join docs with non-overlapping id spaces (mirrors `build_ref_id` in
    markers.py, which already includes ref_type for snapshot marker ids).

    Used exclusively as the Elasticsearch `_id` for the incremental refs join doc. Not a stored
    field -- `git.ref_key` was removed from all index mappings and content builders. The string
    is opaque to queries; the join uses `(git.host, git.org, git.repo, git.ref, git.ref_type)`
    natively.

    `~` is safe as a delimiter because it is illegal in git ref names (see
    `git check-ref-format`) and matches the index-name segment delimiter used for
    host/org/repo elsewhere (see `indices.py`), so it cannot collide with any joined value.
    """
    return "~".join((host.lower(), org.lower(), repo.lower(), ref_type, ref))


def make_doc_id(*parts: str) -> str:
    """Deterministic, URL-safe document id: BLAKE2b hex of the NUL-joined fields.

    The same field tuple always yields the same id (idempotent re-indexing), while the
    hex output keeps slashes out of the id so get/delete-by-id work via the URL path.
    surrogateescape matches the decode in iter_tracked_files so non-UTF-8 paths round-trip.
    """
    joined = ID_SEP.join(p.encode("utf-8", errors="surrogateescape") for p in parts)
    return hashlib.blake2b(joined, digest_size=ID_DIGEST_SIZE).hexdigest()


# Larger bulk batches sent concurrently (see parallel_bulk in commands/index/documents.py) take
# longer per request, so give them a generous timeout and let the client retry transient timeouts.
CLIENT_OPTS = {"request_timeout": 120, "max_retries": 3, "retry_on_timeout": True}

# Remote/transient Elasticsearch failures (read timeouts, dropped connections, API errors,
# per-doc bulk failures) that should fail only the current unit of work -- so a long batch keeps
# going and reports that one ref/repo as an error -- rather than crashing the whole run. Shared
# by the index and prune commands.
ES_ERRORS = (ApiError, TransportError, BulkIndexError)


def make_client(
    url: str,
    api_key: str | None,
    username: str | None,
    password: str | None,
    insecure: bool = False,
) -> Elasticsearch:
    opts = {**CLIENT_OPTS}
    if insecure:
        opts["verify_certs"] = False
        # Suppress the per-request InsecureRequestWarning that urllib3 would otherwise spam.
        warnings.filterwarnings("ignore", message="Unverified HTTPS request", category=Warning)
    if api_key:
        return Elasticsearch(url, api_key=api_key, **opts)
    if username and password:
        return Elasticsearch(url, basic_auth=(username, password), **opts)
    return Elasticsearch(url, **opts)


def is_serverless(es: Elasticsearch) -> bool:
    """True if the target deployment is Elastic Cloud Serverless.

    Serverless reports `build_flavor: "serverless"` in the root `info()` response, and it
    rejects some stateful-only APIs (e.g. force-merge). Callers use this to skip those APIs
    rather than emit a per-run error. Cached on the client instance so repeated checks in one
    run cost a single round-trip; a failed probe returns False (assume stateful) without
    caching, so a transient error doesn't stick for the rest of the run."""
    cached = getattr(es, "_sourcerer_serverless", None)
    if cached is not None:
        return cached
    try:
        flavor = es.info().get("version", {}).get("build_flavor")
    except (ApiError, TransportError):
        return False
    result = flavor == "serverless"
    es._sourcerer_serverless = result
    return result
