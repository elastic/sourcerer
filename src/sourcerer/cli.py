# Third-party packages
import click
from dotenv import find_dotenv, load_dotenv

# App packages
from .commands import index as index_cmd
from .commands import prune as prune_cmd
from .commands import setup as setup_cmd


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
        load_dotenv(value)
    else:
        load_dotenv(find_dotenv(usecwd=True))
    return value


def env_option(f):
    return click.option(
        "-e",
        "--env",
        "env_file",
        type=click.Path(exists=True, dir_okay=False, resolve_path=True),
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
@env_option
@auth_options
def index(repo_spec, branch, tag, commit, config_path, force, quiet, cache_dir, ephemeral, prune, dry_run, url, api_key, username, password):
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
        index_cmd.run_config(config_path, url, api_key, username, password, force, quiet, cache_dir, ephemeral, prune, dry_run)
    else:
        if prune:
            raise click.UsageError("--prune requires --config (there is no retention policy for a single ref)")
        if dry_run:
            raise click.UsageError("--dry-run requires --config")
        if not repo_spec:
            raise click.UsageError("provide a REPO_SPEC ('<org>/<repo>') or --config")
        index_cmd.run(repo_spec, branch, tag, commit, url, api_key, username, password, force, quiet, cache_dir, ephemeral)


@cli.command()
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
@auth_options
def prune(config_path, dry_run, quiet, url, api_key, username, password):
    """Delete indexed refs that fall outside their repos.yml retention policies, then sweep
    for orphans.

    With --config, applies the same retain policies the `index` command uses to skip doomed
    refs, but retroactively: refs already indexed that a policy would now delete are removed,
    along with any content (lines/files) no surviving ref still references.

    Afterwards -- or always, if --config is omitted -- also detects and removes orphans: whole
    files/lines indices with no matching entry in sourcerer-v1-refs (e.g. a repo removed
    from the config), commit content left behind with no marker referencing it, and refs
    markers whose content is entirely gone. Use --dry-run to preview both passes first.
    """
    prune_cmd.run(config_path, url, api_key, username, password, dry_run, quiet)


if __name__ == "__main__":
    cli()
