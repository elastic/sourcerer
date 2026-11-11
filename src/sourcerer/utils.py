# Standard packages
import hashlib
import time
import warnings
from collections.abc import Callable
from typing import TypeVar

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


_T = TypeVar("_T")


def retry_transient(
    fn: Callable[[], _T],
    *,
    is_retryable: Callable[[Exception], bool],
    attempts: int = 4,
    base_delay: float = 1.0,
    on_retry: Callable[[Exception, int], None] | None = None,
) -> _T:
    """Call `fn()`; on an exception for which `is_retryable(e)` is true, sleep
    `base_delay * 2**attempt` (1s, 2s, 4s, ... by default) and retry, up to `attempts` total
    calls. Re-raises immediately on a non-retryable exception, or once `attempts` is exhausted.

    Exists because the Elasticsearch client's own retry budget (CLIENT_OPTS below) has no
    inter-attempt backoff -- with a single-node Cloud/Serverless endpoint its retries fire back
    to back against the same node, so they exhaust in a fraction of a second and a blip lasting
    longer than that still surfaces. This helper is the thing that actually rides it out. Mirrors
    the ad hoc backoff loop that used to live only in commands/index/git.py's list_remote_refs,
    generalized so both git and Elasticsearch callers share one implementation."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if attempt + 1 >= attempts or not is_retryable(e):
                raise
            if on_retry is not None:
                on_retry(e, attempt)
            time.sleep(base_delay * (2 ** attempt))
    raise AssertionError("unreachable")  # attempts >= 1 guarantees return or raise above


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

# HTTP statuses worth retrying: 429 is backpressure (bulk/search thread pool rejected the
# request -- a Serverless coordinating node over its indexing-buffer limit is the common case),
# 502/503/504 are gateway/availability errors from a proxy or an unready node. Not 4xx statuses
# like 400/401/403/404 -- those are permanent for this request (bad input, auth, missing
# resource) and retrying would just fail the same way after wasting the backoff delay.
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

# ES error "type" strings worth retrying even on a status outside _RETRYABLE_STATUSES (defense in
# depth -- a top-level bulk rejection is already a 429, but a per-request circuit-breaker trip can
# in principle surface differently). elasticsearch-py sets ApiError.message/.error to
# resp_body["error"]["type"] when the response body is the standard ES error shape (see
# elasticsearch._sync.client._base's error handling), so this matches on exactly the string the
# real "es_rejected_execution_exception" failure (Serverless coordinating-node backpressure) uses.
_RETRYABLE_ES_ERROR_TYPES = frozenset({"es_rejected_execution_exception", "circuit_breaking_exception"})


def is_retryable_es_error(e: Exception) -> bool:
    """True if `e` (one of ES_ERRORS) is a transient failure worth retrying with backoff rather
    than failing the current ref/repo immediately.

    elastic_transport.ApiError and TransportError are siblings (both subclass Exception
    directly), not parent/child, so they're checked as two separate cases:

    Retried: ApiError whose status or ES error type marks it as backpressure/availability
    (_RETRYABLE_STATUSES / _RETRYABLE_ES_ERROR_TYPES). Also retried: any other TransportError --
    connection-level failures (ConnectionError, TlsError, ConnectionTimeout) where no HTTP
    response was ever received, e.g. a dropped TLS handshake mid-transfer.

    Not retried: other ApiError values and BulkIndexError containing permanent per-document
    failures (for example document_parsing_exception). A bulk failure is retryable only when
    every failed item has a retryable status or ES error type."""
    if isinstance(e, ApiError):
        if e.status_code in _RETRYABLE_STATUSES:
            return True
        return e.error in _RETRYABLE_ES_ERROR_TYPES
    if isinstance(e, TransportError):
        return True
    if isinstance(e, BulkIndexError):
        return bool(e.errors) and all(
            item.get("status") in _RETRYABLE_STATUSES
            or (
                isinstance(item.get("error"), dict)
                and item["error"].get("type") in _RETRYABLE_ES_ERROR_TYPES
            )
            for error in e.errors
            for item in error.values()
        )
    return False


# Cap how many distinct (type, reason) pairs format_es_error renders -- a bulk failure is
# typically one root cause repeated across every doc in the batch (e.g. a mapping conflict or
# a Serverless-specific field-count/size limit), so a handful of distinct messages is enough to
# diagnose it without flooding a single progress line.
_MAX_DISTINCT_BULK_ERRORS = 3


def format_es_error(e: Exception) -> str:
    """Render an ES_ERRORS exception for display, including per-document detail for
    BulkIndexError.

    elasticsearch-py raises BulkIndexError with the full per-item bulk response (status code,
    error type/reason) attached as `.errors`, but the exception's own `str()` is just an item
    count ("N document(s) failed to index."). Without unpacking `.errors`, that count is all an
    operator ever sees -- no way to tell a one-off mapping conflict from a systemic
    Serverless-only limit (e.g. a field-count or mapping-depth cap that stateful ES doesn't
    enforce by default).
    """
    if isinstance(e, BulkIndexError):
        distinct: dict[tuple[str, str], int] = {}
        for err in e.errors:
            item = next(iter(err.values()))
            info = item.get("error") or {}
            if isinstance(info, dict):
                key = (str(info.get("type", "unknown")), str(info.get("reason", "")))
            else:
                key = ("unknown", str(info))
            distinct[key] = distinct.get(key, 0) + 1
        shown = sorted(distinct.items(), key=lambda kv: -kv[1])[:_MAX_DISTINCT_BULK_ERRORS]
        details = "; ".join(
            f"{etype}: {reason} (x{count})" if count > 1 else f"{etype}: {reason}"
            for (etype, reason), count in shown
        )
        omitted = len(distinct) - len(shown)
        if omitted > 0:
            details += f"; ...and {omitted} more distinct error(s)"
        return f"{e.args[0]} {details}" if details else str(e)
    return str(e)


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
