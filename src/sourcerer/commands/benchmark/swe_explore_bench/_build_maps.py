"""Standalone SWE-Explore-Bench dataset builder.

Run INSIDE the SWE-Explore-Bench checkout's `uv` environment (it needs
HuggingFace `datasets`, which the checkout depends on, not sourcerer):

    uv run --directory <checkout> python _build_maps.py --dest <checkout>

It is intentionally self-contained — it imports only `datasets` and the stdlib,
never `sourcerer` — so it runs cleanly in that separate venv.

Two outputs, both keyed by the 848 instance_ids in the bench file:
  - bench_commit_map.json  {instance_id: base_commit}
  - bench_issue_map.json   {instance_id: issue_text}

Every instance's base_commit AND problem_statement are resolved directly from the
four source datasets, so no upstream commit_map.json / issue_map.json is needed:

  - verified (451):     princeton-nlp/SWE-bench_Verified + nebius/SWE-rebench
  - pro (215):          ScaleAI/SWE-bench_Pro (dataset id = "instance_" + bench id;
                        use the dataset's `base_commit`, NOT the fix/test SHA
                        embedded in the instance id).
  - multilingual (182): SWE-bench/SWE-bench_Multilingual

Fails loudly (exit 1) if any instance is left without a commit or issue text — a
partial map would otherwise silently zero-score those instances.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCH_DATASET = "SWE-Explore-Bench/SWE-Explore-Bench"
BENCH_FILE = "bench.final.mixcap.jsonl"
COMMIT_MAP = "bench_commit_map.json"
ISSUE_MAP = "bench_issue_map.json"

# Source datasets. Each carries both `base_commit` and `problem_statement`.
_SOURCE_DATASETS = (
    "princeton-nlp/SWE-bench_Verified",
    "nebius/SWE-rebench",
    "SWE-bench/SWE-bench_Multilingual",
)
_PRO_DATASET = "ScaleAI/SWE-bench_Pro"


def _download_bench(bench_path: Path) -> None:
    from datasets import load_dataset

    print(f"Downloading {BENCH_DATASET} -> {bench_path.name} ...", file=sys.stderr)
    ds = load_dataset(BENCH_DATASET, split="train")
    ds.to_json(str(bench_path))


def _load_sources() -> tuple[dict[str, str], dict[str, str]]:
    """Return ({instance_id: base_commit}, {instance_id: problem_statement}) merged
    across all four source datasets, keyed by bench instance ids."""
    from datasets import load_dataset

    commits: dict[str, str] = {}
    issues: dict[str, str] = {}

    for ds_name in _SOURCE_DATASETS:
        print(f"Loading {ds_name} ...", file=sys.stderr)
        for item in load_dataset(ds_name, split="test"):
            iid = item["instance_id"]
            commits[iid] = item["base_commit"]
            issues[iid] = item.get("problem_statement") or ""

    # SWE-bench_Pro instance ids are the bench ids prefixed with "instance_".
    print(f"Loading {_PRO_DATASET} ...", file=sys.stderr)
    for item in load_dataset(_PRO_DATASET, split="test"):
        iid = item["instance_id"].removeprefix("instance_")
        commits[iid] = item["base_commit"]
        issues[iid] = item.get("problem_statement") or ""

    return commits, issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Benchmark directory (the SWE-Explore-Bench checkout) to read/write files in.",
    )
    args = parser.parse_args()

    dest: Path = args.dest
    bench_path = dest / BENCH_FILE
    if not bench_path.exists():
        _download_bench(bench_path)

    instance_ids: list[str] = []
    with bench_path.open() as f:
        for line in f:
            if line.strip():
                instance_ids.append(json.loads(line)["instance_id"])

    src_commits, src_issues = _load_sources()

    commits: dict[str, str] = {}
    issues: dict[str, str] = {}
    missing_commit: list[str] = []
    missing_issue: list[str] = []
    for iid in instance_ids:
        commit = src_commits.get(iid)
        if commit:
            commits[iid] = commit
        else:
            missing_commit.append(iid)

        issue = (src_issues.get(iid) or "").strip()
        if issue:
            issues[iid] = src_issues[iid]
        else:
            missing_issue.append(iid)

    failed = False
    for what, missing in (("base commit", missing_commit), ("issue text", missing_issue)):
        if missing:
            failed = True
            print(f"ERROR: {len(missing)}/{len(instance_ids)} instances have no {what}:", file=sys.stderr)
            for iid in missing[:20]:
                print(f"  {iid}", file=sys.stderr)
            if len(missing) > 20:
                print(f"  ... and {len(missing) - 20} more", file=sys.stderr)
    if failed:
        sys.exit(1)

    commit_out = dest / COMMIT_MAP
    issue_out = dest / ISSUE_MAP
    commit_out.write_text(json.dumps(dict(sorted(commits.items())), indent=1) + "\n")
    issue_out.write_text(json.dumps(dict(sorted(issues.items())), indent=1) + "\n")
    print(
        f"Resolved {len(commits)}/{len(instance_ids)} commits and "
        f"{len(issues)}/{len(instance_ids)} issue texts.",
        file=sys.stderr,
    )
    print(f"Wrote {commit_out}", file=sys.stderr)
    print(f"Wrote {issue_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
