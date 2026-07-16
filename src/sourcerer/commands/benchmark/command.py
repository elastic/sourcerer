"""Generic `sourcerer benchmark` command implementations.

Thin dispatch layer over the benchmark registry: resolves `<benchmark_name>` to a
handler and delegates. `get`/`run` operate on `./benchmarks/<name>/` in the user's
current working directory; `index` reuses the packaged `repos.yml` and the existing
config-driven indexer.
"""
from __future__ import annotations

from importlib.resources import as_file, files
from pathlib import Path

from . import registry
from ..index import command as index_cmd


def _dest(name: str) -> Path:
    """Working location for a benchmark's dataset: ./benchmarks/<name>/."""
    return Path.cwd() / "benchmarks" / name


def available() -> list[str]:
    """Benchmark names available to `get` — the subdirectories of the
    `sourcerer.benchmarks` package."""
    root = files("sourcerer.benchmarks")
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    )


def _ensure_ready(name: str) -> Path:
    """Resolve a benchmark's dataset dir, lazily downloading + building it if
    it isn't present yet. Returns the dataset dir."""
    handler = registry.get_handler(name)
    dest = _dest(name)
    if not handler.is_ready(dest):
        handler.get(dest)
    return dest


def get(name: str) -> None:
    """Download + build a benchmark's dataset into ./benchmarks/<name>/."""
    handler = registry.get_handler(name)
    handler.get(_dest(name))


def index(
    name: str,
    url: str,
    api_key: str | None,
    username: str | None,
    password: str | None,
    force: bool,
    quiet: bool,
    cache_dir: str | None,
    ephemeral: bool,
    prune: bool,
    dry_run: bool,
) -> None:
    """Index a benchmark's commits via the packaged repos.yml (config-driven)."""
    _ensure_ready(name)
    package = registry.data_package(name)
    resource = files(package).joinpath("repos.yml")
    with as_file(resource) as repos_yml:
        index_cmd.run_config(
            str(repos_yml), url, api_key, username, password,
            force, quiet, cache_dir, ephemeral, prune, dry_run,
        )


def run(
    name: str,
    *,
    top_k: str,
    concurrency: int,
    connector_id: str | None,
    resume: bool,
) -> None:
    """Run a benchmark's eval, lazily building its dataset first if needed."""
    dest = _ensure_ready(name)
    handler = registry.get_handler(name)
    handler.run(
        dest,
        top_k=top_k,
        concurrency=concurrency,
        connector_id=connector_id,
        resume=resume,
    )
