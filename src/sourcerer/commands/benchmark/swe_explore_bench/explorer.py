"""Sourcerer explorer for SWE-Explore-Bench.

Unlike the other agentic explorers (ClaudeCodeExplorer, CursorAgentExplorer, ...),
SourcererExplorer does NOT need a local clone of the repo under test: it queries
code already indexed into Elasticsearch via `sourcerer index <org>/<repo> -c <commit>`
(i.e. `sourcerer benchmark index swe_explore_bench`). Index every base_commit the
split references BEFORE running the eval.

This module is imported by `eval.py` only after the SWE-Explore-Bench checkout has
been inserted onto `sys.path`, so `from explorers.base import ...` resolves against
that checkout's dependency-free `explorers/base.py`.

Requires: requests (a sourcerer dependency).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from explorers.base import ContextRegion, Explorer, ExplorerResult

# Pro-format instance IDs embed the base commit directly:
#   {org}__{repo}-{40-char-base-commit}[-v{target}]
# e.g. ansible__ansible-3db08adbb1cc6aa9941be5e0fc810132c6e1fa4b-vba6da65...
#      flipt-io__flipt-5ffba3406a7993d97ced4cc13658bee66150fcca  (no -v suffix)
#      element-hq__element-web-18c03daa865d3c5b10e52b669cd50be34c67b2e5-vnan
# Greedy (.+) backtracks to the last `-{40hex}` so hyphenated repo names work.
_PRO_ID_RE = re.compile(r"^([^_]+)__(.+)-([0-9a-f]{40})(?:-v|$)")

_RELEVANT_FILE_RE = re.compile(
    r"^\s*-\s+(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$"
)


def _instance_to_org_repo(instance_id: str) -> tuple[str, str]:
    """`django__django-11099` -> ("django", "django").

    Also handles pro-format IDs: `ansible__ansible-{40hex}-v{...}` -> ("ansible", "ansible").
    """
    m = _PRO_ID_RE.match(instance_id)
    if m:
        return m.group(1), m.group(2)
    repo_part = instance_id.rsplit("-", 1)[0]
    org, _, repo = repo_part.partition("__")
    return org, repo


def parse_relevant_files(markdown: str) -> list[ContextRegion]:
    """Extract `RELEVANT_FILES` entries as (path, start, end) regions.

    The benchmark prompt asks for a single `RELEVANT_FILES:` block, but the parser
    is tolerant of extra prose before/after that block and de-duplicates repeated
    entries while preserving first-seen order.
    """
    lines = markdown.splitlines()
    start_idx = 0
    for idx, line in enumerate(lines):
        if line.strip() == "RELEVANT_FILES:":
            start_idx = idx + 1
            break
    regions: list[ContextRegion] = []
    seen: set[tuple[str, int, int]] = set()
    for line in lines[start_idx:]:
        m = _RELEVANT_FILE_RE.match(line)
        if not m:
            continue
        path = m.group("path").strip()
        start = int(m.group("start"))
        end = int(m.group("end")) if m.group("end") else start
        if end < start:
            start, end = end, start
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        regions.append(ContextRegion(path=path, start=start, end=end))
    return regions


@dataclass
class SourcererExplorer(Explorer):
    """Calls the Sourcerer agent over Elastic Agent Builder's converse API and
    turns its `RELEVANT_FILES` response block into ranked ContextRegions.

    `commit_map` should be the complete instance_id -> base_commit mapping produced
    by `sourcerer benchmark get` (bench_commit_map.json), so the commit you ask
    Sourcerer to scope to matches what you indexed. It must cover every instance —
    there is no fallback.

    Pass a Rich Console as `console` to print each prompt and response in dim text.
    Pass a `trace_log` path to append one JSONL record per call (prompt, response,
    extracted regions, timing) for later inspection and optimization.
    Pass `connector_id` to override the Kibana Agent Builder LLM connector (i.e. choose
    the model). When omitted the deployment's default connector is used.
    """

    kibana_url: str
    api_key: str
    commit_map: dict[str, str]
    agent_id: str = "sourcerer"
    connector_id: str | None = None  # Agent Builder connector that selects the LLM; None = deployment default
    timeout: int = 900
    max_retries: int = 0
    console: object = None
    trace_log: object = None  # str | Path | None
    _trace_lock: object = field(default_factory=threading.Lock, repr=False)

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "Content-Type": "application/json",
            "kbn-xsrf": "true",
            "Authorization": f"ApiKey {self.api_key}",
        })
        return s

    def _write_trace(self, record: dict) -> None:
        if not self.trace_log:
            return
        line = json.dumps(record, ensure_ascii=False)
        with self._trace_lock:
            with open(self.trace_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def explore(
        self, *, instance_id: str, query: str, top_k: int = 5
    ) -> list[ExplorerResult]:
        org, repo = _instance_to_org_repo(instance_id)
        # No fallback to the SHA embedded in pro-format IDs: that is the fix/test
        # commit (see SWE-bench_Pro's before_repo_set_cmd), not the base commit
        # the benchmark trajectories explored. The commit map must be complete —
        # `sourcerer benchmark get` builds it and fails loudly on any gap.
        commit = self.commit_map.get(instance_id)
        if not commit:
            self._write_trace({"instance_id": instance_id, "error": "no commit in commit_map"})
            return []

        t0 = time.time()
        
        # In the interest of comparing like-for-like in terms of reasoning,
        # token usage, and latency, this prompt closely matches the EXPLORE_PROMPT
        # of the claude_code explorer in terms of instructions, inputs, and outputs.
        # It omits the skills and tools for repo discovery and ref resolution and
        # instead receives the same explicit scoping that the other explorers receive.
        prompt = (
            "You are a code exploration specialist. Explore this repository to find the\n"
            "source files and line ranges most relevant to understanding and fixing the\n"
            "following issue.\n"
            "\n"
            "Use ONLY your Code Search and Code Citations skills and ONLY your\n"
            "sourcerer.code.* and sourcerer.files.* tools to explore the codebase.\n"
            "Focus on finding the ROOT CAUSE, not just symptom locations.\n"
            "\n"
            "Do NOT use your Repo Discovery skill, Ref Resolution skill, or\n"
            "sourcerer.refs.list tool. Scope EVERY sourcerer.code.* and\n"
            "sourcerer.files.* tool call to these filters:\n"
            f"- git_org={org}\n"
            f"- git_repo={repo}\n"
            f"- git_commit={commit}\n"
            "\n"
            "After exploration, output your findings in EXACTLY this format:\n"
            "\n"
            "RELEVANT_FILES:\n"
            "- path/to/file1.py:10-50\n"
            "- path/to/file2.py:1-100\n"
            "\n"
            f"Focus on the root cause. Limit to top {top_k} most relevant regions.\n"
            "\n"
            "ISSUE:\n"
            "\n"
            f"{query}"
        )

        base = self.kibana_url.rstrip("/")
        session = self._session()

        if self.console:
            self.console.print(f"[dim]PROMPT\n{prompt}[/dim]")

        resp = None
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if self.console:
                    self.console.print(f"[dim]→ POST /api/agent_builder/converse (attempt {attempt + 1})[/dim]")
                body: dict = {"input": prompt, "agent_id": self.agent_id}
                if self.connector_id:
                    body["connector_id"] = self.connector_id
                resp = session.post(
                    f"{base}/api/agent_builder/converse",
                    json=body,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 ** attempt)
        if resp is None:
            self._write_trace({
                "instance_id": instance_id,
                "org": org, "repo": repo, "commit": commit,
                "connector_id": self.connector_id,
                "top_k": top_k,
                "prompt": prompt,
                "error": str(last_err),
                "status_code": None,
                "elapsed_s": round(time.time() - t0, 2),
            })
            raise RuntimeError(f"Sourcerer converse call failed: {last_err}")

        data = resp.json()
        message = data.get("response", {}).get("message", "")

        regions = parse_relevant_files(message)[:top_k]

        if self.console:
            self.console.print(f"[dim]← {resp.status_code} ({len(message)} chars, {len(regions)} regions)\n{message}[/dim]")

        self._write_trace({
            "instance_id": instance_id,
            "org": org, "repo": repo, "commit": commit,
            "connector_id": self.connector_id,
            "top_k": top_k,
            "prompt": prompt,
            "response": message,
            "status_code": resp.status_code,
            "response_chars": len(message),
            "num_regions": len(regions),
            "regions": [{"path": r.path, "start": r.start, "end": r.end} for r in regions],
            "elapsed_s": round(time.time() - t0, 2),
        })

        if not regions:
            return []

        return [ExplorerResult(instance_id=instance_id, score=1.0, regions=regions)]
