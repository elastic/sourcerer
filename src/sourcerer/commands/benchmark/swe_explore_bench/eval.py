"""`sourcerer benchmark run swe_explore_bench` — evaluate SourcererExplorer.

Imports SWE-Explore-Bench's own reusable, module-level pieces from the checkout
(`eval.ExploreEvaluator`; `eval_runner`'s loading/formatting helpers) — nothing is
copied — and drives them with the package's SourcererExplorer. The checkout dir is
put on `sys.path` first so those imports (and the explorer's `from explorers.base
import ...`) resolve against it.

Output rows match eval_runner.py's schema exactly (instance_id, explorer, regions,
metrics, num_regions), written to
`<dest>/results/{explorer}-{YYYYMMDDHHMMSS}/top{k}.jsonl` (+ traces.jsonl).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

BENCH_FILE = "bench.final.mixcap.jsonl"
COMMIT_MAP = "bench_commit_map.json"
ISSUE_MAP = "bench_issue_map.json"

EXPLORER_NAME = "sourcerer"


def _get_issue(rec: dict, issue_map: dict[str, str]) -> str:
    iid = rec.get("instance_id", "")
    return issue_map.get(iid) or rec.get("problem_statement", "")


def _parse_gt_json_fields(evaluator) -> None:
    # HuggingFace ds.to_json() double-serializes nested objects as JSON strings.
    # Walk every ground_truth dict and parse any string-valued fields back to
    # their native type so eval.py's dict/list accessors don't blow up.
    for item in evaluator.bench_data:
        gt = item.get("ground_truth")
        if not isinstance(gt, dict):
            continue
        for key, val in list(gt.items()):
            if isinstance(val, str):
                try:
                    gt[key] = json.loads(val)
                except (ValueError, TypeError):
                    pass


def run(
    dest: Path,
    *,
    top_k: str = "5",
    concurrency: int = 1,
    connector_id: str | None = None,
    resume: bool = False,
) -> None:
    console = Console()

    kibana_url = os.environ.get("KIBANA_URL")
    api_key = os.environ.get("ELASTICSEARCH_API_KEY")
    if not kibana_url or not api_key:
        raise SystemExit(
            "KIBANA_URL and ELASTICSEARCH_API_KEY must be set (e.g. via -e/--env) "
            "to run the eval against Elastic Agent Builder's converse API."
        )

    # Put the checkout on sys.path so `eval`, `eval_runner`, and the explorer's
    # `from explorers.base import ...` resolve against it. Import deferred to here.
    sys.path.insert(0, str(dest))
    from eval import ExploreEvaluator  # noqa: E402
    from eval_runner import (  # noqa: E402
        METRICS,
        _load_bench_records,
        _load_issue_map,
        _build_file_line_counts,
        _results_to_regions,
        _parse_top_k_list,
        _format_output_path,
        _load_existing_results,
    )

    from .explorer import SourcererExplorer

    bench_path = dest / BENCH_FILE
    commit_map_file = dest / COMMIT_MAP
    issue_map_file = dest / ISSUE_MAP

    records = _load_bench_records(bench_path)

    # Refuse to run with an incomplete commit map: an unresolvable instance is
    # silently scored as zero (the explorer returns [] with a "no commit in
    # commit_map" trace), which corrupts the aggregate metrics.
    commit_map: dict[str, str] = json.loads(commit_map_file.read_text())
    unresolved = [r["instance_id"] for r in records if r.get("instance_id") not in commit_map]
    if unresolved:
        raise SystemExit(
            f"{len(unresolved)}/{len(records)} bench instances have no base commit in "
            f"{commit_map_file} (e.g. {unresolved[:3]}). Re-run `sourcerer benchmark get`."
        )

    if issue_map_file.exists():
        issue_map = json.loads(issue_map_file.read_text())
    else:
        issue_map = {}

    # Same guarantee for the issue text: an instance with an empty issue sends
    # Sourcerer a prompt ending in a bare "ISSUE:", which yields no regions and
    # scores zero without any visible failure.
    no_issue = [
        r["instance_id"]
        for r in records
        if not _get_issue(r, issue_map).strip()
    ]
    if no_issue:
        raise SystemExit(
            f"{len(no_issue)}/{len(records)} bench instances have no issue text "
            f"(e.g. {no_issue[:3]}). Re-run `sourcerer benchmark get`."
        )

    # Local repo snapshots aren't fetched, so file-line-count ground truth is empty;
    # scoring proceeds without it (unchanged from the standalone driver).
    file_line_counts = _build_file_line_counts(records, None)
    evaluator = ExploreEvaluator(bench_path, file_line_counts=file_line_counts)
    _parse_gt_json_fields(evaluator)

    top_k_list = _parse_top_k_list(top_k)
    max_top_k = max(top_k_list)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    results_dir = dest / "results" / f"{EXPLORER_NAME}-{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)
    output_jsonl = str(results_dir / "top{k}.jsonl")
    trace_log = results_dir / "traces.jsonl"

    # Pass console only when running sequentially; parallel workers would interleave
    # the verbose PROMPT/response dumps into unreadable output.
    explorer = SourcererExplorer(
        kibana_url=kibana_url,
        api_key=api_key,
        commit_map=commit_map,
        connector_id=connector_id,
        console=(console if concurrency == 1 else None),
        trace_log=trace_log,
    )

    per_k_totals: dict[int, dict[str, float]] = {k: {m: 0.0 for m in METRICS} for k in top_k_list}
    per_k_evaluated: dict[int, int] = {k: 0 for k in top_k_list}
    resumed_ids: set[str] = set()

    if resume:
        per_k_ids = []
        for k in top_k_list:
            existing = _load_existing_results(_format_output_path(output_jsonl, EXPLORER_NAME, k))
            ids = {r["instance_id"] for r in existing}
            per_k_ids.append(ids)
            for r in existing:
                per_k_evaluated[k] += 1
                for m in METRICS:
                    per_k_totals[k][m] += r["metrics"].get(m, 0.0)
        if per_k_ids:
            resumed_ids = set.intersection(*per_k_ids)

    remaining = [
        r for r in records
        if r.get("instance_id", "") not in resumed_ids
    ]
    console.print(
        f"\n[bold cyan]▶ sourcerer[/bold cyan]  "
        f"({len(remaining)} to run, {len(resumed_ids)} resumed, "
        f"top_k={top_k_list}, concurrency={concurrency}, "
        f"connector={connector_id or 'default'})"
    )
    console.print(f"[dim]traces → {trace_log}[/dim]")

    out_files: dict[int, object] = {}
    for k in top_k_list:
        out_path = _format_output_path(output_jsonl, EXPLORER_NAME, k)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_files[k] = out_path.open("a")

    t0 = time.time()
    total = len(remaining)
    done = 0

    def _explore_one(rec):
        """Network call only — runs in worker thread. Thread-safe: builds its own Session."""
        iid = rec.get("instance_id", "")
        try:
            results = explorer.explore(
                instance_id=iid, query=_get_issue(rec, issue_map), top_k=max_top_k,
            )
            return rec, _results_to_regions(results)
        except Exception as e:
            sys.stderr.write(f"\n  [ERROR] sourcerer {iid}: {e}\n")
            return rec, []

    def _score_and_write(rec, preds):
        """Scoring + file writes — called on the main thread only (evaluator is not thread-safe)."""
        nonlocal done
        iid = rec.get("instance_id", "")
        done += 1
        bench_gt = evaluator.bench_data_dict[iid]["ground_truth"]
        per_file_lines = file_line_counts.get(iid, {})
        for k in top_k_list:
            sliced = preds[:k]
            evaluator._current_instance_id = iid
            evaluator._current_file_line_counts = per_file_lines
            scores = {m: getattr(evaluator, f"evaluate_{m}")(sliced, bench_gt) for m in METRICS}
            row = {
                "instance_id": iid,
                "explorer": EXPLORER_NAME,
                "regions": [{"path": p, "start": s, "end": e} for p, s, e in sliced],
                "metrics": scores,
                "num_regions": min(len(preds), k),
            }
            for m in METRICS:
                per_k_totals[k][m] += scores[m]
            per_k_evaluated[k] += 1
            out_files[k].write(json.dumps(row, ensure_ascii=False) + "\n")
            out_files[k].flush()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        if concurrency == 1:
            # Sequential path: preserve the per-instance rule separator and the
            # explorer's verbose console trace (PROMPT / response dumps).
            for i, rec in enumerate(remaining, 1):
                iid = rec.get("instance_id", "")
                console.rule(f"[dim]{i}/{total}  {iid}[/dim]", style="dim")
                _, preds = pool.submit(_explore_one, rec).result()
                _score_and_write(rec, preds)
        else:
            # Parallel path: submit all, process each future as it completes.
            futures = {pool.submit(_explore_one, rec): rec for rec in remaining}
            for future in concurrent.futures.as_completed(futures):
                rec, preds = future.result()
                iid = rec.get("instance_id", "")
                _score_and_write(rec, preds)
                console.print(f"[dim]{done}/{total}[/dim]  {iid}  ({len(preds)} regions)")

    for f in out_files.values():
        f.close()

    table = Table(title="sourcerer Results", show_lines=False)
    table.add_column("top_k", justify="right")
    table.add_column("Eval", justify="right")
    for metric in METRICS:
        table.add_column(metric, justify="right")
    for k in top_k_list:
        n = per_k_evaluated[k]
        avg = {m: (per_k_totals[k][m] / n if n else 0.0) for m in METRICS}
        table.add_row(str(k), str(n), *[f"{avg[m]:.4f}" for m in METRICS])
    console.print(table)
    console.print(f"[dim]Results → {results_dir}[/dim]")
    console.print(f"[dim]Done in {time.time() - t0:.1f}s[/dim]")
