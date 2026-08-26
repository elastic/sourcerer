# sourcerer/commands/index/schedule.py
# Schedule-gating logic for `sourcerer index --config`: determines which sources are due for
# indexing based on their configured schedule and the state of refs already indexed in
# sourcerer-v3-refs (last completed-at and any active in-progress indexing runs).
#
# The gate runs BEFORE the expensive ls-remote / clone / ingest pipeline, so a source whose
# schedule hasn't fired since its last indexed run is dropped before any network I/O.

# Standard packages
import datetime
from dataclasses import dataclass
from typing import Literal

# Third-party packages
from elasticsearch import Elasticsearch, NotFoundError

# App packages
from ...config import Config, RepoConfig, Selector, Schedule, ScheduleRule, resolve_schedule
from ...indices import REFS_INDEX

# Default retry window: how long an "indexing" marker is trusted as an active run before
# being treated as stuck/abandoned and retried. Configurable via --retry-window; default 1h.
RETRY_INTERVAL = datetime.timedelta(hours=1)


@dataclass
class SourceState:
    """The indexing state of one source scope (host/org/repo/ref_type) in the refs index."""
    last_indexed_at: datetime.datetime | None   # max indexed_at where status:complete; None = never
    active_indexing: bool                        # True if any ref has status:indexing within retry window


@dataclass
class ScheduleDecision:
    """Scheduling decision for one (RepoConfig, Selector) pair."""
    repo: RepoConfig
    selector: Selector
    schedule: Schedule
    state: SourceState
    due: bool
    reason: str   # human-readable explanation for --dry-run / verbose output


def source_state(
    es: Elasticsearch,
    host: str,
    org: str,
    repo: str,
    ref_type: str,
    now: datetime.datetime,
    retry_window: datetime.timedelta = RETRY_INTERVAL,
) -> SourceState:
    """Query the refs index for the current state of one source scope.

    Returns:
      - last_indexed_at: the most recent `indexed_at` for any status:complete ref in scope.
      - active_indexing: True if any ref in scope has status:indexing AND its
        indexing_started_at is within the retry window (i.e. the run is not stuck).
    """
    query: dict = {
        "bool": {
            "filter": [
                {"term": {"git.host": host}},
                {"term": {"git.org": org}},
                {"term": {"git.repo": repo}},
                {"term": {"git.ref_type": ref_type}},
            ]
        }
    }
    aggs = {
        # max indexed_at across completed refs
        "max_indexed_at": {
            "filter": {"term": {"status": "complete"}},
            "aggs": {"ts": {"max": {"field": "indexed_at"}}},
        },
        # count of refs that are actively indexing (started within the retry window)
        "active_indexing": {
            "filter": {
                "bool": {
                    "filter": [
                        {"term": {"status": "indexing"}},
                        {"range": {"indexing_started_at": {
                            "gte": (now - retry_window).isoformat(),
                        }}},
                    ]
                }
            }
        },
    }
    try:
        resp = es.search(index=REFS_INDEX, size=0, query=query, aggregations=aggs)
    except NotFoundError:
        # Refs index doesn't exist yet (first-ever run).
        return SourceState(last_indexed_at=None, active_indexing=False)

    agg = resp.get("aggregations", {})

    ts_value = agg.get("max_indexed_at", {}).get("ts", {}).get("value")
    last_indexed_at: datetime.datetime | None = None
    if ts_value is not None:
        # ES returns epoch_millis for date fields in aggregations.
        dt = datetime.datetime.fromtimestamp(ts_value / 1000.0, tz=datetime.timezone.utc)
        last_indexed_at = dt

    active_count = agg.get("active_indexing", {}).get("doc_count", 0)
    active_indexing = active_count > 0

    return SourceState(last_indexed_at=last_indexed_at, active_indexing=active_indexing)


def compute_decisions(
    es: Elasticsearch,
    config: Config,
    now: datetime.datetime,
    retry_window: datetime.timedelta = RETRY_INTERVAL,
) -> list[ScheduleDecision]:
    """Determine which (repo, selector) pairs are due for indexing.

    For each selector in the config, resolves its effective schedule, queries the refs index
    for the source's current state, and decides whether the source is due.

    Returns a list of ScheduleDecision objects (one per selector, including not-due ones so
    the caller can print a full report in --dry-run mode).
    """
    decisions: list[ScheduleDecision] = []

    # Cache source_state lookups: one query per (host, org, repo, ref_type) scope.
    state_cache: dict[tuple[str, str, str, str], SourceState] = {}

    for repo_cfg in config.repos:
        for sel in repo_cfg.selectors:
            scope = (repo_cfg.host, repo_cfg.org, repo_cfg.repo, sel.ref_type)
            if scope not in state_cache:
                state_cache[scope] = source_state(
                    es, repo_cfg.host, repo_cfg.org, repo_cfg.repo, sel.ref_type, now,
                    retry_window=retry_window,
                )
            state = state_cache[scope]
            schedule = resolve_schedule(
                repo_cfg.host, repo_cfg.org, repo_cfg.repo, sel, config.schedules
            )

            if state.active_indexing:
                due = False
                reason = "skipped: another run is actively indexing this source"
            elif not schedule.due(state.last_indexed_at, now):
                last_str = (state.last_indexed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                            if state.last_indexed_at else "never")
                reason = f"not due (last indexed: {last_str})"
                due = False
            else:
                due = True
                last_str = (state.last_indexed_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                            if state.last_indexed_at else "never")
                reason = f"due (last indexed: {last_str})"

            decisions.append(ScheduleDecision(
                repo=repo_cfg, selector=sel, schedule=schedule,
                state=state, due=due, reason=reason,
            ))

    return decisions


def filter_config_by_schedule(
    es: Elasticsearch,
    config: Config,
    now: datetime.datetime,
    retry_window: datetime.timedelta = RETRY_INTERVAL,
) -> tuple[Config, list[ScheduleDecision]]:
    """Return a filtered Config containing only sources that are due for indexing, plus the full
    list of decisions (for dry-run reporting).

    A RepoConfig is included if at least one of its selectors is due. Only due selectors are
    kept (so a partially-due repo's not-due selectors are excluded from ls-remote / indexing,
    meaning they don't contribute units to Phase 1).
    """
    decisions = compute_decisions(es, config, now, retry_window=retry_window)

    # Build the filtered repos list: keep repos with at least one due selector, carrying only
    # those due selectors. This preserves the existing grouping shape so downstream code is
    # unchanged -- process_group, plan_repo, etc. all operate on RepoConfig objects.
    filtered_repos: list[RepoConfig] = []
    for repo_cfg in config.repos:
        due_selectors = [
            d.selector for d in decisions
            if d.repo is repo_cfg and d.due
        ]
        if due_selectors:
            filtered_repos.append(RepoConfig(
                host=repo_cfg.host,
                org=repo_cfg.org,
                repo=repo_cfg.repo,
                selectors=due_selectors,
            ))

    filtered_config = Config(
        hosts=config.hosts,
        repos=filtered_repos,
        schedules=config.schedules,
    )
    return filtered_config, decisions
