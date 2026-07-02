# Third-party packages
import click
from dotenv import find_dotenv, load_dotenv

# App packages
from .commands import index as index_cmd
from .commands import setup as setup_cmd

# Resolve `.env` from the current working directory (and its parents) rather than
# from this package's install location. A bare load_dotenv() walks up from this
# file, which finds nothing when sourcerer is installed as a uv tool in its own venv.
load_dotenv(find_dotenv(usecwd=True))


def auth_options(f):
    f = click.option("--url", required=True, envvar="ELASTICSEARCH_URL", help="Elasticsearch cluster URL.")(f)
    f = click.option("--api-key", envvar="ELASTICSEARCH_API_KEY", default=None, help="Elasticsearch API key.")(f)
    f = click.option("--username", envvar="ELASTICSEARCH_USERNAME", default=None, help="Elasticsearch username.")(f)
    f = click.option("--password", envvar="ELASTICSEARCH_PASSWORD", default=None, help="Elasticsearch password.")(f)
    return f


@click.group()
def cli():
    """Sourcerer - index and search source code in Elasticsearch."""


@cli.command(name="help")
@click.pass_context
def help_cmd(ctx):
    """Show help for sourcerer commands."""
    click.echo(ctx.parent.get_help())


@cli.command()
@auth_options
@click.option("--kb-url", envvar="KIBANA_URL", default=None, help="Kibana URL for agent builder setup.")
def setup(url, api_key, username, password, kb_url):
    """Idempotently load index templates and Kibana agent builder objects."""
    setup_cmd.run(url, api_key, username, password, kb_url)


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
@auth_options
def index(repo_spec, branch, tag, commit, config_path, force, quiet, cache_dir, ephemeral, url, api_key, username, password):
    """Index a remote GitHub repo's git-tracked files into Elasticsearch.

    Provide a REPO_SPEC ('<org>/<repo>') for a single repo, or --config to index multiple
    repos/branches/tags selected by glob patterns from a YAML file.

    Clones are cached under --cache-dir (default ~/.cache/sourcerer) and refreshed with
    `git fetch` on later runs, so a scheduled run only transfers new commits; pass --ephemeral
    for a throwaway clone instead.
    """
    if config_path:
        if repo_spec or branch or tag or commit:
            raise click.UsageError("--config cannot be combined with REPO_SPEC or -b/-t/-c")
        index_cmd.run_config(config_path, url, api_key, username, password, force, quiet, cache_dir, ephemeral)
    else:
        if not repo_spec:
            raise click.UsageError("provide a REPO_SPEC ('<org>/<repo>') or --config")
        index_cmd.run(repo_spec, branch, tag, commit, url, api_key, username, password, force, quiet, cache_dir, ephemeral)


if __name__ == "__main__":
    cli()
