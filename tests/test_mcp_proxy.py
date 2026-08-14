"""Unit tests for `sourcerer mcp-proxy`: endpoint construction, auth header building,
and the run() entry point (patched so no server is actually started).
"""

# Standard packages
import base64
import sys
from unittest.mock import MagicMock, patch

# Third-party packages
import pytest

# App packages
from sourcerer.commands.mcp_proxy import command as proxy


class TestEndpoint:
    @pytest.mark.parametrize("base", [
        "https://kb.example.com",
        "https://kb.example.com/",
    ])
    def test_trailing_slash_tolerated(self, base):
        assert proxy._endpoint(base) == "https://kb.example.com/api/agent_builder/mcp"

    def test_uses_mcp_endpoint_path(self):
        result = proxy._endpoint("https://kb.example.com")
        assert result.endswith("/api/agent_builder/mcp")


class TestAuthHeader:
    def test_api_key(self):
        hdr = proxy._auth_header("mykey", None, None)
        assert hdr == "ApiKey mykey"

    def test_api_key_takes_priority_over_username(self):
        hdr = proxy._auth_header("mykey", "user", "pass")
        assert hdr == "ApiKey mykey"

    def test_basic_auth_user_and_password(self):
        hdr = proxy._auth_header(None, "alice", "secret")
        expected_b64 = base64.b64encode(b"alice:secret").decode()
        assert hdr == f"Basic {expected_b64}"

    def test_basic_auth_empty_password(self):
        hdr = proxy._auth_header(None, "alice", None)
        expected_b64 = base64.b64encode(b"alice:").decode()
        assert hdr == f"Basic {expected_b64}"

    def test_no_creds_returns_none(self):
        assert proxy._auth_header(None, None, None) is None

    def test_no_creds_empty_strings_returns_none(self):
        # Click passes "" when env var is unset and default=None is overridden to ""
        assert proxy._auth_header("", "", "") is None


class TestRun:
    def _mock_fastmcp(self):
        """Return (mock_create_proxy, mock_mcp_instance) with create_proxy wired up."""
        mcp_instance = MagicMock()
        create_proxy = MagicMock()
        create_proxy.return_value = mcp_instance
        return create_proxy, mcp_instance

    def _fake_modules(self, mock_create_proxy, mock_transport_cls, mock_proxy_client_cls):
        """Build fake fastmcp submodules matching the imports inside run()."""
        import types
        fake_transport_mod = types.ModuleType("fastmcp.client.transports")
        fake_transport_mod.StreamableHttpTransport = mock_transport_cls
        fake_server_mod = types.ModuleType("fastmcp.server")
        fake_server_mod.create_proxy = mock_create_proxy
        fake_proxy_mod = types.ModuleType("fastmcp.server.providers.proxy")
        fake_proxy_mod.ProxyClient = mock_proxy_client_cls
        return {
            "fastmcp.client.transports": fake_transport_mod,
            "fastmcp.server": fake_server_mod,
            "fastmcp.server.providers.proxy": fake_proxy_mod,
        }

    def test_exits_when_kb_url_missing(self):
        with pytest.raises(SystemExit) as exc_info:
            proxy.run(None, "mykey", None, None)
        assert exc_info.value.code == 1

    def test_exits_when_no_creds(self):
        with pytest.raises(SystemExit) as exc_info:
            proxy.run("https://kb.example.com", None, None, None)
        assert exc_info.value.code == 1

    def test_proxy_built_with_api_key_auth(self):
        mock_create_proxy, mock_mcp = self._mock_fastmcp()
        mock_transport_cls = MagicMock()
        mock_proxy_client_cls = MagicMock()

        with patch.dict(
            sys.modules,
            self._fake_modules(mock_create_proxy, mock_transport_cls, mock_proxy_client_cls),
        ):
            proxy.run("https://kb.example.com", "mykey", None, None)

        # Transport must be built with the resolved endpoint and ApiKey header
        mock_transport_cls.assert_called_once_with(
            "https://kb.example.com/api/agent_builder/mcp",
            headers={"Authorization": "ApiKey mykey"},
        )
        # ProxyClient wraps the transport
        mock_proxy_client_cls.assert_called_once_with(mock_transport_cls.return_value)
        # create_proxy is called with the proxy client and a name
        mock_create_proxy.assert_called_once()
        call_args = mock_create_proxy.call_args
        assert call_args[0][0] is mock_proxy_client_cls.return_value
        assert call_args[1].get("name") == "Sourcerer"
        # mcp.run() is called with no arguments (defaults to stdio)
        mock_mcp.run.assert_called_once_with()

    def test_proxy_built_with_basic_auth(self):
        mock_create_proxy, mock_mcp = self._mock_fastmcp()
        mock_transport_cls = MagicMock()
        mock_proxy_client_cls = MagicMock()

        with patch.dict(
            sys.modules,
            self._fake_modules(mock_create_proxy, mock_transport_cls, mock_proxy_client_cls),
        ):
            proxy.run("https://kb.example.com", None, "alice", "secret")

        expected_b64 = base64.b64encode(b"alice:secret").decode()
        mock_transport_cls.assert_called_once_with(
            "https://kb.example.com/api/agent_builder/mcp",
            headers={"Authorization": f"Basic {expected_b64}"},
        )
        mock_mcp.run.assert_called_once_with()

    def test_no_literal_secret_on_stdout(self, capsys):
        """Validate that errors go to stderr, not stdout (stdout is the JSON-RPC stream)."""
        with pytest.raises(SystemExit):
            proxy.run(None, None, None, None)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "[ERROR]" in captured.err

    def _run_with_mocks(self, insecure: bool):
        """Helper: run() with all fastmcp internals mocked; return the transport call kwargs."""
        mock_create_proxy, mock_mcp = self._mock_fastmcp()
        mock_transport_cls = MagicMock()
        mock_proxy_client_cls = MagicMock()

        with patch.dict(
            sys.modules,
            self._fake_modules(mock_create_proxy, mock_transport_cls, mock_proxy_client_cls),
        ):
            proxy.run("https://kb.example.com", "mykey", None, None, insecure=insecure)

        _, call_kwargs = mock_transport_cls.call_args
        return call_kwargs

    def test_insecure_false_omits_httpx_client_factory(self):
        """Without --insecure, no httpx_client_factory is passed to StreamableHttpTransport."""
        call_kwargs = self._run_with_mocks(insecure=False)
        assert "httpx_client_factory" not in call_kwargs

    def test_insecure_true_passes_httpx_client_factory(self):
        """With insecure=True, an httpx_client_factory is passed to StreamableHttpTransport."""
        call_kwargs = self._run_with_mocks(insecure=True)
        assert "httpx_client_factory" in call_kwargs
        assert callable(call_kwargs["httpx_client_factory"])
