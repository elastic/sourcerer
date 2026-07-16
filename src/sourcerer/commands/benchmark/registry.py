"""Registry of available benchmarks.

Each benchmark is a handler module exposing `get(dest)`, `is_ready(dest)`, and
`run(dest, **opts)`, plus a data package under `sourcerer.benchmarks.<name>`
holding its static `repos.yml`. To add a benchmark, drop the code under
`sourcerer.commands.benchmark.<name>` and the data under
`sourcerer.benchmarks.<name>`, then register it here.
"""
from __future__ import annotations

from importlib import import_module
from types import ModuleType

import click

# benchmark name -> handler module import path
_BENCHMARKS: dict[str, str] = {
    "swe_explore_bench": "sourcerer.commands.benchmark.swe_explore_bench",
}


def available() -> list[str]:
    return sorted(_BENCHMARKS)


def get_handler(name: str) -> ModuleType:
    """Return the handler module for `name`, or raise a UsageError listing the
    valid names."""
    try:
        module_path = _BENCHMARKS[name]
    except KeyError:
        raise click.UsageError(
            f"unknown benchmark '{name}'. Available: {', '.join(available())}"
        )
    return import_module(module_path)


def data_package(name: str) -> str:
    """Import package holding `name`'s static data (e.g. repos.yml)."""
    get_handler(name)  # validate the name
    return f"sourcerer.benchmarks.{name}"
