"""Unit tests for sourcerer.commands.index.command's retry wrapping: _is_retryable_unit_error
(combines the ES and git retryable-error predicates) and _retry_unit (the retry-with-backoff
wrapper applied around each per-ref indexing call). No ES, no subprocess -- the wrapped callable
is a plain Python function that raises/returns as directed."""

# Standard packages
from unittest.mock import MagicMock

# Third-party packages
import pytest
from elastic_transport import ApiResponseMeta, ConnectionError as ESConnectionError, HttpHeaders
from elasticsearch import ApiError

# App packages
from sourcerer.commands.index.command import _is_retryable_unit_error, _retry_unit
from sourcerer.commands.index.git import GitAccessDenied, GitError
from sourcerer.progress import ProgressReporter, Unit


def _api_error(status: int, error_type: str = "some_error") -> ApiError:
    meta = ApiResponseMeta(status=status, http_version="1.1", headers=HttpHeaders({}), duration=0.0, node=None)
    return ApiError(error_type, meta, {"error": {"type": error_type, "reason": "boom"}})


def _unit() -> Unit:
    return Unit(host="github", org="elastic", repo="beats", ref="v8.19.24", kind="tag")


class TestIsRetryableUnitError:
    def test_es_transport_error_is_retryable(self):
        assert _is_retryable_unit_error(ESConnectionError("TLS blip")) is True

    def test_es_429_is_retryable(self):
        assert _is_retryable_unit_error(_api_error(429, "es_rejected_execution_exception")) is True

    def test_es_400_is_not_retryable(self):
        assert _is_retryable_unit_error(_api_error(400, "illegal_argument_exception")) is False

    def test_git_transient_network_error_is_retryable(self):
        e = GitError(128, ["git", "checkout"], output=None,
                     stderr="fatal: the remote end hung up unexpectedly")
        assert _is_retryable_unit_error(e) is True

    def test_git_access_denied_is_not_retryable(self):
        e = GitAccessDenied(128, ["git", "clone"], output=None,
                            stderr="fatal: Authentication failed")
        assert _is_retryable_unit_error(e) is False

    def test_unrelated_exception_is_not_retryable(self):
        assert _is_retryable_unit_error(ValueError("local bug")) is False


class TestRetryUnit:
    """_retry_unit wraps a per-unit indexing callable: retries a transient ES/git failure with
    backoff and leaves a reporter trace; re-raises immediately (no retry) on anything else."""

    def test_succeeds_first_try_no_reporter_note(self, monkeypatch):
        monkeypatch.setattr("sourcerer.utils.time.sleep", lambda d: None)
        reporter = ProgressReporter()
        unit = _unit()
        stages = []
        reporter.set_stage = lambda u, s: stages.append(s)  # type: ignore[method-assign]

        result = _retry_unit(lambda: "indexed", reporter, unit)

        assert result == "indexed"
        assert stages == []  # no retry happened, so no retry note

    def test_retries_transient_es_error_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("sourcerer.utils.time.sleep", lambda d: None)
        reporter = ProgressReporter()
        unit = _unit()
        stages = []
        reporter.set_stage = lambda u, s: stages.append(s)  # type: ignore[method-assign]

        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ESConnectionError("TLS error caused by: SSLError(EOF occurred)")
            return "indexed"

        result = _retry_unit(flaky, reporter, unit)

        assert result == "indexed"
        assert attempts["n"] == 2
        assert len(stages) == 1
        assert "retrying after transient error" in stages[0]

    def test_non_retryable_error_raises_immediately_no_retry_note(self, monkeypatch):
        monkeypatch.setattr("sourcerer.utils.time.sleep", lambda d: None)
        reporter = ProgressReporter()
        unit = _unit()
        stages = []
        reporter.set_stage = lambda u, s: stages.append(s)  # type: ignore[method-assign]
        calls = {"n": 0}

        def fails_permanently():
            calls["n"] += 1
            raise _api_error(400, "illegal_argument_exception")

        with pytest.raises(ApiError):
            _retry_unit(fails_permanently, reporter, unit)

        assert calls["n"] == 1  # no retry attempted
        assert stages == []  # no retry note for a non-retryable failure

    def test_exhausts_retries_then_raises_original_error_type(self, monkeypatch):
        monkeypatch.setattr("sourcerer.utils.time.sleep", lambda d: None)
        reporter = ProgressReporter()
        unit = _unit()
        reporter.set_stage = lambda u, s: None  # type: ignore[method-assign]
        calls = {"n": 0}

        def always_transient():
            calls["n"] += 1
            raise _api_error(503, "unavailable")

        with pytest.raises(ApiError):
            _retry_unit(always_transient, reporter, unit)

        assert calls["n"] > 1  # retried at least once before giving up
