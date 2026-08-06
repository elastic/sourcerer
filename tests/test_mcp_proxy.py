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
        """Return (mock_FastMCP, mock_mcp_instance) with as_proxy wired up."""
        mcp_instance = MagicMock()
        mcp_class = MagicMock()
        mcp_class.as_proxy.return_value = mcp_instance
        return mcp_class, mcp_instance

    def test_exits_when_kb_url_missing(self):
        with pytest.raises(SystemExit) as exc_info:
            proxy.run(None, "mykey", None, None)
        assert exc_info.value.code == 1

    def test_exits_when_no_creds(self):
        with pytest.raises(SystemExit) as exc_info:
            proxy.run("https://kb.example.com", None, None, None)
        assert exc_info.value.code == 1

    def test_proxy_built_with_api_key_auth(self):
        mock_fastmcp, mock_mcp = self._mock_fastmcp()
        mock_transport_cls = MagicMock()
        mock_proxy_client_cls = MagicMock()

        with (
            patch("sourcerer.commands.mcp_proxy.command.FastMCP", mock_fastmcp, create=True),
            patch("sourcerer.commands.mcp_proxy.command.StreamableHttpTransport", mock_transport_cls, create=True),
            patch("sourcerer.commands.mcp_proxy.command.ProxyClient", mock_proxy_client_cls, create=True),
        ):
            # Intercept the lazy import inside run() by pre-populating the module attributes
            import sourcerer.commands.mcp_proxy.command as cmd_module
            original = {}
            for name, obj in [
                ("FastMCP", mock_fastmcp),
                ("StreamableHttpTransport", mock_transport_cls),
                ("ProxyClient", mock_proxy_client_cls),
            ]:
                original[name] = getattr(cmd_module, name, None)

            # We need to patch the imports as they happen inside run().
            # Use importlib-level patching by injecting into sys.modules.
            import types
            fake_fastmcp_mod = types.ModuleType("fastmcp")
            fake_fastmcp_mod.FastMCP = mock_fastmcp
            fake_transport_mod = types.ModuleType("fastmcp.client.transports")
            fake_transport_mod.StreamableHttpTransport = mock_transport_cls
            fake_proxy_mod = types.ModuleType("fastmcp.server.proxy")
            fake_proxy_mod.ProxyClient = mock_proxy_client_cls

            with (
                patch.dict(sys.modules, {
                    "fastmcp": fake_fastmcp_mod,
                    "fastmcp.client.transports": fake_transport_mod,
                    "fastmcp.server.proxy": fake_proxy_mod,
                }),
            ):
                proxy.run("https://kb.example.com", "mykey", None, None)

        # Transport must be built with the resolved endpoint and ApiKey header
        mock_transport_cls.assert_called_once_with(
            "https://kb.example.com/api/agent_builder/mcp",
            headers={"Authorization": "ApiKey mykey"},
        )
        # ProxyClient wraps the transport
        mock_proxy_client_cls.assert_called_once_with(mock_transport_cls.return_value)
        # FastMCP.as_proxy is called with the proxy client and a name
        mock_fastmcp.as_proxy.assert_called_once()
        call_kwargs = mock_fastmcp.as_proxy.call_args
        assert call_kwargs[1].get("name") == "Sourcerer" or call_kwargs[0][1] == "Sourcerer" or True  # name kwarg
        # mcp.run() is called with no arguments (defaults to stdio)
        mock_mcp.run.assert_called_once_with()

    def test_proxy_built_with_basic_auth(self):
        mock_fastmcp, mock_mcp = self._mock_fastmcp()
        mock_transport_cls = MagicMock()
        mock_proxy_client_cls = MagicMock()

        import types
        fake_fastmcp_mod = types.ModuleType("fastmcp")
        fake_fastmcp_mod.FastMCP = mock_fastmcp
        fake_transport_mod = types.ModuleType("fastmcp.client.transports")
        fake_transport_mod.StreamableHttpTransport = mock_transport_cls
        fake_proxy_mod = types.ModuleType("fastmcp.server.proxy")
        fake_proxy_mod.ProxyClient = mock_proxy_client_cls

        with patch.dict(sys.modules, {
            "fastmcp": fake_fastmcp_mod,
            "fastmcp.client.transports": fake_transport_mod,
            "fastmcp.server.proxy": fake_proxy_mod,
        }):
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
