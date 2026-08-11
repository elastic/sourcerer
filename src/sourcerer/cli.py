# Standard packages
import os

# Third-party packages
import click
from dotenv import find_dotenv, load_dotenv

# App packages
from .commands.benchmark import command as benchmark_cmd
from .commands.index import command as index_cmd
from .commands.mcp_proxy import command as mcp_proxy_cmd
from .commands.prune import command as prune_cmd
from .commands.setup import command as setup_cmd


def _parse_retry_window(ctx, param, value):
    """Option callback: parse a duration string like '30m', '1h', '6h', '1d' into a timedelta."""
    from .config import parse_duration
    try:
        return parse_duration(value)
    except ValueError as e:
        raise click.BadParameter(str(e), param=param)


def _load_env(ctx, param, value):
    """Eager option callback: load the chosen `.env` before other options resolve envvars.

    All-or-nothing: with -e/--env, load *only* that file (`click.Path(resolve_path=True)`
    has already resolved a bare filename or relative path against the current directory);
    otherwise fall back to the default `.env` discovered from the cwd. `find_dotenv(usecwd=True)`
    walks up from the cwd rather than this package's install location, which matters when
    sourcerer runs as an installed uv tool in its own venv. Runs eagerly so the ELASTICSEARCH_*
    envvars are populated before the auth options below read them.
    """
    if value:
        path = os.path.expanduser(value)
        if not os.path.isfile(path):
            raise click.BadParameter(f"Path '{path}' does not exist or is not a file.", param=param)
        load_dotenv(path)
    else:
        load_dotenv(find_dotenv(usecwd=True))
    return value


def env_option(f):
    return click.option(
        "-e",
        "--env",
        "env_file",
        type=click.Path(exists=False, dir_okay=False, resolve_path=False),
        default=None,
        is_eager=True,
        expose_value=False,
        callback=_load_env,
        help="Path to a custom .env file to load instead of the default .env. "
        "Relative paths are resolved against the current directory.",
    )(f)


def auth_options(f):
    f = click.option("--url", required=True, envvar="ELASTICSEARCH_URL", help="Elasticsearch cluster URL.")(f)
    f = click.option("--api-key", envvar="ELASTICSEARCH_API_KEY", default=None, help="Elasticsearch API key.")(f)
    f = click.option("--username", envvar="ELASTICSEARCH_USERNAME", default=None, help="Elasticsearch username.")(f)
    f = click.option("--password", envvar="ELASTICSEARCH_PASSWORD", default=None, help="Elasticsearch password.")(f)
    return f


def insecure_option(f):
    return click.option(
        "--insecure",
        is_flag=True,
        default=False,
        envvar="ALLOW_INSECURE_TLS",
        help="Skip TLS certificate verification when connecting to Elasticsearch or Kibana "
        "(useful for locally-hosted clusters with self-signed certificates). "
        "Can also be set via the ALLOW_INSECURE_TLS environment variable. Off by default.",
    )(f)


@click.group()
def cli():
    """Sourcerer - index and search source code in Elasticsearch."""


@cli.command(name="help")
@click.pass_context
def help_cmd(ctx):
    """Show help for sourcerer commands."""
    click.echo(ctx.parent.get_help())


@cli.command()
@env_option
@insecure_option
@auth_options
@click.option("--kb-url", envvar="KIBANA_URL", default=None, help="Kibana URL for agent builder setup.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="sourcerer.yml whose 'hosts:' section customizes/extends the built-in git-host defaults "
    "used to generate per-host citation skills. Omit to use only the built-in host defaults.",
)
@click.option(
    "--include-experimental",
    is_flag=True,
    default=False,
    help="Also set up experimental resources (those under elastic/*/experimental/).",
)
@click.argument("categories", nargs=-1)
def setup(url, api_key, username, password, kb_url, config_path, include_experimental, categories, insecure):
    """Idempotently load index templates and Kibana agent builder objects.

    Generates one citation skill per known/configured git host (built-in defaults merged with
    the config's 'hosts:' overrides) and pushes them alongside the base tools/skills/agent.

    CATEGORIES controls which resource groups to set up (default: all stable resources):

    \b
      all        All categories (default when none specified)
      agents     Agent Builder agents
      skills     Agent Builder skills
      tools      Agent Builder tools
      templates  Elasticsearch index templates
      dashboards Kibana saved objects (dashboards, lenses, etc.)
      workflows  Kibana workflows

    Pass --include-experimental to also set up resources under elastic/*/experimental/.
    """
    from .commands.setup.command import VALID_CATEGORIES
    unknown = [c for c in categories if c not in VALID_CATEGORIES]
    if unknown:
        raise click.BadArgumentUsage(
            f"Unknown category {unknown[0]!r}. "
            f"Valid categories: {', '.join(sorted(VALID_CATEGORIES))}."
        )
    setup_cmd.run(url, api_key, username, password, kb_url, config_path,
                  categories=categories, include_experimental=include_experimental,
                  insecure=insecure)


@cli.command()
@click.argument("repo_spec", required=False)
@click.option("-b", "--branch", default=None, help="Branch to index.")
@click.option("-t", "--tag", default=None, help="Tag to index.")
@click.option("-c", "--commit", default=None, help="Commit hash to index.")
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML config selecting multiple repos/branches/tags to index.",
)
@click.option("-f", "--force", is_flag=True, default=False, help="Re-index even if already indexed.")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress progress output (for programmatic use).")
@click.option(
    "--cache-dir",
    envvar="SOURCERER_CACHE_DIR",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory for persistent repo clones (default: ~/.cache/sourcerer). "
    "Reused and `git fetch`ed on later runs instead of re-cloning.",
)
@click.option(
    "--ephemeral",
    is_flag=True,
    default=False,
    help="Clone into a throwaway temp dir and delete it afterwards, instead of using the cache.",
)
@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help="After all indexing completes, prune indexed refs that fall outside the config's "
    "retention policies (equivalent to running `sourcerer prune --config` afterwards). Requires --config.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would be indexed (and, with --prune, what would be pruned afterwards) "
    "without writing to Elasticsearch. Clones/fetches the cached repos to resolve real commits. Requires --config.",
)
@click.option(
    "--retry-window",
    default="1h",
    callback=_parse_retry_window,
    help="How long a ref's in-progress 'indexing' marker is trusted as an active concurrent "
    "run (skip re-indexing it); older markers are treated as stuck and re-indexed. Also "
    "drives the schedule gate's stuck-run detection. Duration like 30m, 1h, 6h, 1d. Default 1h.",
)
@env_option
@insecure_option
@auth_options
def index(repo_spec, branch, tag, commit, config_path, force, quiet, cache_dir, ephemeral, prune, dry_run, retry_window, url, api_key, username, password, insecure):
    """Index a remote GitHub repo's git-tracked files into Elasticsearch.

    Provide a REPO_SPEC ('<host>/<org>/<repo>') for a single repo, or --config to index multiple
    repos/branches/tags selected by glob patterns from a YAML file.

    Clones are cached under --cache-dir (default ~/.cache/sourcerer) and refreshed with
    `git fetch` on later runs, so a scheduled run only transfers new commits; pass --ephemeral
    for a throwaway clone instead.
    """
    if config_path:
        if repo_spec or branch or tag or commit:
            raise click.UsageError("--config cannot be combined with REPO_SPEC or -b/-t/-c")
        index_cmd.run_config(config_path, url, api_key, username, password, force, quiet, cache_dir, ephemeral, prune, dry_run, retry_window=retry_window, insecure=insecure)
    else:
        if prune:
            raise click.UsageError("--prune requires --config (there is no retention policy for a single ref)")
        if dry_run:
            raise click.UsageError("--dry-run requires --config")
        if not repo_spec:
            raise click.UsageError("provide a REPO_SPEC ('<host>/<org>/<repo>') or --config")
        index_cmd.run(repo_spec, branch, tag, commit, url, api_key, username, password, force, quiet, cache_dir, ephemeral, retry_window=retry_window, insecure=insecure)


@cli.command()
@click.argument("repo_spec", required=False)
@click.option("-b", "--branch", default=None, help="Branch to prune.")
@click.option("-t", "--tag", default=None, help="Tag to prune.")
@click.option(
    "-c", "--commit", default=None,
    help="Commit hash to prune (full SHA or unambiguous prefix ≥7 chars). Deletes every ref "
    "marker pinned to that commit (branch, tag, or commit-pin) and any content exclusively "
    "owned by it, regardless of how the commit was originally indexed. If no marker exists for "
    "the commit (e.g. an old commit a branch has moved past), the content docs are deleted "
    "directly.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML config whose retain policies decide which indexed refs to delete. Omit to "
    "run only the orphan sweep, which doesn't depend on a config.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be deleted without deleting anything.",
)
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress output for repos with nothing to prune.")
@env_option
@insecure_option
@auth_options
def prune(repo_spec, branch, tag, commit, config_path, dry_run, quiet, url, api_key, username, password, insecure):
    """Delete indexed refs that fall outside their sourcerer.yml retention policies, then sweep
    for orphans.

    Provide a REPO_SPEC ('<host>/<org>/<repo>') with exactly one of -b/-t/-c to prune only
    that single ref and any content exclusively owned by it. No orphan sweep is performed.

    With --config, applies the same retain policies the `index` command uses to skip doomed
    refs, but retroactively: refs already indexed that a policy would now delete are removed,
    along with any content (lines/files) no surviving ref still references.

    With --config, or when neither REPO_SPEC nor --config is given, also detects and removes
    orphans: whole files/lines indices with no matching entry in sourcerer-refs (e.g. a repo
    removed from the config), commit content left behind with no marker referencing it, and
    refs markers whose content is entirely gone. Use --dry-run to preview both passes first.
    """
    if config_path:
        if repo_spec or branch or tag or commit:
            raise click.UsageError("--config cannot be combined with REPO_SPEC or -b/-t/-c")
        prune_cmd.run(config_path, url, api_key, username, password, dry_run, quiet, insecure=insecure)
    elif repo_spec:
        refs = {k: v for k, v in [("branch", branch), ("tag", tag), ("commit", commit)] if v}
        if not refs:
            raise click.UsageError("provide exactly one of -b/--branch, -t/--tag, -c/--commit with REPO_SPEC")
        if len(refs) > 1:
            raise click.UsageError("specify at most one of -b/--branch, -t/--tag, -c/--commit")
        prune_cmd.run_ref(repo_spec, branch, tag, commit, url, api_key, username, password, dry_run, quiet, insecure=insecure)
    else:
        if branch or tag or commit:
            raise click.UsageError("-b/-t/-c require a REPO_SPEC ('<host>/<org>/<repo>')")
        prune_cmd.run(None, url, api_key, username, password, dry_run, quiet, insecure=insecure)



@cli.command(name="mcp-proxy")
@env_option
@insecure_option
@click.option("--kb-url", envvar="KIBANA_URL", default=None, help="Kibana URL. The proxy forwards to {kb-url}/api/agent_builder/mcp. Required.")
@click.option("--api-key", envvar="ELASTICSEARCH_API_KEY", default=None, help="Elasticsearch API key (ApiKey auth).")
@click.option("--username", envvar="ELASTICSEARCH_USERNAME", default=None, help="Elasticsearch username (Basic auth).")
@click.option("--password", envvar="ELASTICSEARCH_PASSWORD", default=None, help="Elasticsearch password (Basic auth).")
def mcp_proxy(kb_url, api_key, username, password, insecure):
    """Run a stdio MCP proxy that forwards to the Kibana Agent Builder MCP endpoint.

    Intended to be launched by Claude Desktop via the mcpServers section of
    claude_desktop_config.json, with KIBANA_URL and credentials supplied in that
    file's env block. Accepts ApiKey auth (ELASTICSEARCH_API_KEY) or Basic auth
    (ELASTICSEARCH_USERNAME + ELASTICSEARCH_PASSWORD). All diagnostic output goes
    to stderr; stdout carries the JSON-RPC stream.
    """
    mcp_proxy_cmd.run(kb_url, api_key, username, password, insecure=insecure)


@cli.group()
def benchmark():
    """Fetch, index, and run code-exploration benchmarks (e.g. swe_explore_bench)."""


@benchmark.command(name="list")
def benchmark_list():
    """List the benchmarks available to get."""
    for name in benchmark_cmd.available():
        click.echo(name)


@benchmark.command(name="get")
@click.argument("benchmark_name")
@env_option
def benchmark_get(benchmark_name):
    """Download and build BENCHMARK_NAME's dataset into ./benchmarks/<name>/."""
    benchmark_cmd.get(benchmark_name)


@benchmark.command(name="index")
@click.argument("benchmark_name")
@click.option("-f", "--force", is_flag=True, default=False, help="Re-index even if already indexed.")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress progress output (for programmatic use).")
@click.option(
    "--cache-dir",
    envvar="SOURCERER_CACHE_DIR",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory for persistent repo clones (default: ~/.cache/sourcerer). "
    "Reused and `git fetch`ed on later runs instead of re-cloning.",
)
@click.option(
    "--ephemeral",
    is_flag=True,
    default=False,
    help="Clone into a throwaway temp dir and delete it afterwards, instead of using the cache.",
)
@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help="After all indexing completes, prune indexed refs that fall outside the benchmark "
    "config's retention policies (equivalent to `sourcerer prune --config` afterwards).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview what would be indexed (and, with --prune, what would be pruned afterwards) "
    "without writing to Elasticsearch.",
)
@env_option
@insecure_option
@auth_options
def benchmark_index(benchmark_name, force, quiet, cache_dir, ephemeral, prune, dry_run, url, api_key, username, password, insecure):
    """Index BENCHMARK_NAME's commits into Elasticsearch using its packaged repos.yml.

    Runs the equivalent of `sourcerer index --config <benchmark>/repos.yml`; the config
    path is fixed per benchmark, so REPO_SPEC / -b / -t / -c / --config are not accepted.
    """
    benchmark_cmd.index(
        benchmark_name, url, api_key, username, password,
        force, quiet, cache_dir, ephemeral, prune, dry_run,
        insecure=insecure,
    )


@benchmark.command(name="run")
@click.argument("benchmark_name")
@click.option("-k", "--top-k", "top_k", default="5", help="Comma-separated top_k values, e.g. 5,10,20.")
@click.option("-j", "--concurrency", default=1, type=int, help="Instances to explore in parallel (default 1 = sequential).")
@click.option("--connector-id", default=None, help="Agent Builder connector_id selecting the LLM (default: deployment default).")
@click.option("--resume", is_flag=True, default=False, help="Skip instances already completed in the output files.")
@env_option
@insecure_option
def benchmark_run(benchmark_name, top_k, concurrency, connector_id, resume, insecure):
    """Run BENCHMARK_NAME's eval, writing results under ./benchmarks/<name>/results/.

    Lazily downloads and builds the dataset first if it isn't present. Reads
    KIBANA_URL and ELASTICSEARCH_API_KEY from the environment (load them with -e/--env).
    """
    benchmark_cmd.run(
        benchmark_name,
        top_k=top_k,
        concurrency=concurrency,
        connector_id=connector_id,
        resume=resume,
        insecure=insecure,
    )


if __name__ == "__main__":
    cli()
