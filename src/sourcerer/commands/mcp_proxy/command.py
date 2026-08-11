"""`sourcerer mcp-proxy` - stdio↔streamable-HTTP MCP proxy for Claude Desktop.

Runs a local stdio MCP server that forwards every request to the Agent Builder MCP endpoint
at ``{KIBANA_URL}/api/agent_builder/mcp``, injecting the ``Authorization`` header from the
environment. Claude Desktop can launch this command directly via its ``mcpServers`` config block
with ``KIBANA_URL`` and credentials supplied in the ``env`` section.

Accepts two auth forms:
  - ``ELASTICSEARCH_API_KEY``     → ``Authorization: ApiKey <key>``
  - ``ELASTICSEARCH_USERNAME`` + ``ELASTICSEARCH_PASSWORD`` → ``Authorization: Basic <b64>``

All diagnostics are written to stderr; stdout is reserved for the JSON-RPC stream.

fastmcp imports are deferred to ``run()`` so ``--help`` and unit tests that patch the helpers
do not require the full server stack to be imported.
"""

# Standard packages
import base64
import sys

# Third-party packages
import click

MCP_ENDPOINT_PATH = "/api/agent_builder/mcp"


def _endpoint(kb_url: str) -> str:
    """``{KIBANA_URL}/api/agent_builder/mcp``, tolerant of a trailing slash."""
    return kb_url.rstrip("/") + MCP_ENDPOINT_PATH


def _auth_header(api_key: str | None, username: str | None, password: str | None) -> str | None:
    """Build the Authorization header value from the available credentials.

    Returns the header string, or ``None`` when no credentials are present (the caller should
    error out in that case rather than make an unauthenticated request).
    """
    if api_key:
        return f"ApiKey {api_key}"
    if username:
        encoded = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
        return f"Basic {encoded}"
    return None


def run(
    kb_url: str | None,
    api_key: str | None,
    username: str | None,
    password: str | None,
    insecure: bool = False,
) -> None:
    """Validate inputs, build the proxy, and run it over stdio."""
    if not kb_url:
        click.echo(
            "[ERROR] mcp-proxy requires a Kibana URL (--kb-url or KIBANA_URL).",
            err=True,
        )
        sys.exit(1)

    auth = _auth_header(api_key, username, password)
    if auth is None:
        click.echo(
            "[ERROR] mcp-proxy requires credentials. Set ELASTICSEARCH_API_KEY "
            "(ApiKey auth) or both ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD "
            "(Basic auth).",
            err=True,
        )
        sys.exit(1)

    # Defer fastmcp import so --help and unit tests don't require the full server stack.
    from fastmcp import FastMCP  # noqa: PLC0415
    from fastmcp.client.transports import StreamableHttpTransport  # noqa: PLC0415
    from fastmcp.server.proxy import ProxyClient  # noqa: PLC0415

    url = _endpoint(kb_url)

    httpx_client_factory = None
    if insecure:
        import warnings  # noqa: PLC0415
        import httpx  # noqa: PLC0415
        warnings.filterwarnings("ignore", message="Unverified HTTPS request", category=Warning)

        def httpx_client_factory(
            headers=None,
            timeout=None,
            auth=None,
        ):
            return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, verify=False)

    transport_kwargs = {"headers": {"Authorization": auth}}
    if httpx_client_factory is not None:
        transport_kwargs["httpx_client_factory"] = httpx_client_factory
    transport = StreamableHttpTransport(url, **transport_kwargs)
    mcp = FastMCP.as_proxy(ProxyClient(transport), name="Sourcerer")
    mcp.run()
