"""`sourcerer benchmark get swe_explore_bench` — download + build the dataset.

Reproduces the SWE-Explore-Bench setup into a benchmark directory (no data is
vendored into sourcerer):

  1. Clone https://github.com/Qiushao-E/SWE-Explore-Bench and pin the commit this
     integration was built against (provides eval.py / eval_runner.py / explorers/).
  2. `uv sync` in the checkout to install its deps (chiefly HuggingFace `datasets`).
  3. Run `_build_maps.py` in that checkout's `uv` env: download the bench file from
     HuggingFace and build the complete bench_commit_map.json / bench_issue_map.json.

Requires `git`, `uv`, and network access to GitHub + HuggingFace.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_URL = "https://github.com/Qiushao-E/SWE-Explore-Bench.git"
PINNED_COMMIT = "3c12dc5a551937038afcbdb6eb6bbf19f3ddd8c1"

BENCH_FILE = "bench.final.mixcap.jsonl"
COMMIT_MAP = "bench_commit_map.json"
ISSUE_MAP = "bench_issue_map.json"

# The eval imports these from the checkout at runtime; their presence marks a
# usable clone.
_CLONE_MARKER = "eval.py"


def is_ready(dest: Path) -> bool:
    """True when the checkout and all built artifacts are present in `dest`."""
    return all(
        (dest / name).exists()
        for name in (_CLONE_MARKER, BENCH_FILE, COMMIT_MAP, ISSUE_MAP)
    )


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def _clone_and_pin(dest: Path) -> None:
    if not (dest / ".git").is_dir():
        print(f"Cloning SWE-Explore-Bench into {dest} ...")
        _run(["git", "clone", REPO_URL, str(dest)])
    else:
        print(f"Checkout already exists at {dest}, skipping clone.")
    # Fetch is best-effort: the pinned commit is usually already present from the
    # clone; only a moved default branch would require it.
    subprocess.run(
        ["git", "fetch", "--quiet", "origin", PINNED_COMMIT],
        cwd=str(dest),
        check=False,
    )
    _run(["git", "checkout", "--quiet", PINNED_COMMIT], cwd=dest)
    print(f"Pinned to {PINNED_COMMIT}")


def _uv_sync(dest: Path) -> None:
    print("Installing SWE-Explore-Bench dependencies (uv sync) ...")
    _run(["uv", "sync"], cwd=dest)


def _build_maps(dest: Path) -> None:
    """Download the bench file and build the commit/issue maps in the checkout's
    uv env (needs HuggingFace `datasets`, which sourcerer itself doesn't depend on)."""
    builder = Path(__file__).with_name("_build_maps.py")
    print("Building bench commit/issue maps from HuggingFace ...")
    _run(["uv", "run", "--directory", str(dest), "python", str(builder), "--dest", str(dest)])


def get(dest: Path) -> None:
    """Populate `dest` with a pinned checkout, the bench file, and both maps."""
    dest.mkdir(parents=True, exist_ok=True)
    _clone_and_pin(dest)
    _uv_sync(dest)
    _build_maps(dest)
    print(f"\nReady: {dest}")
