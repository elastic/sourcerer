# Standard packages
import dataclasses
import sys
import threading
import time
from collections import Counter

# Third-party packages
import click

# rich is an optional rendering backend: when it is missing (or stdout is not a
# TTY) we fall back to the Plain reporter, so the indexer never hard-depends on it.
try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.text import Text

    _HAS_RICH = True
except ImportError:  # pragma: no cover - rich is in requirements.txt
    _HAS_RICH = False


def format_elapsed(seconds: float) -> str:
    """Compact human duration: '45s', '1m21s', '1h02m03s'. Lower units are
    zero-padded once a larger unit is present so widths stay stable as time ticks."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s"


@dataclasses.dataclass
class Unit:
    """One (host, org, repo, ref) indexing target plus its live/terminal state.

    `ref` may be None until a default branch is resolved. `kind` is one of
    branch|tag|commit|default. `status` is set once on completion to one of
    indexed|skipped|no-changes|tagged|recorded|error.
    """

    host: str
    org: str
    repo: str
    ref: str | None
    kind: str
    stage: str = "pending"  # pending|resolving|cloning|checkout|diffing|indexing|done
    total_files: int | None = None
    files: int = 0
    lines: int = 0
    status: str | None = None
    detail: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    # SHA resolved by Phase 1 ls-remote (branch/tag only; None for commit selectors and the
    # single-repo CLI path). When set, process_group reuses it directly and skips a second
    # per-ref ls-remote in pre_clone_skip. May be slightly stale for fast-moving branches, but
    # the post-clone should_index guard is authoritative -- a stale SHA can only cause a
    # redundant clone, never an incorrect skip.
    remote_sha: str | None = None
    # sources[i].index routing carried from the selector that emitted this unit (see
    # config.Selector / specs/sourcerer-yml.md). Determines the physical files/lines index this
    # unit's content docs are written to; defaults reproduce the historical repo-level name.
    index_level: str = "repo"
    index_suffix: str | None = None
    # sources[i].mode carried from the selector that emitted this unit: "snapshot" (default,
    # commit-addressed) or "delta" (ref-addressed, branch or tag). Routes the unit to the
    # incremental delta-index path instead of the snapshot pre-clone/skip/retention flow.
    mode: str = "snapshot"
    # Stream identity for delta-tag moving streams. For a delta-mode tag selector whose `match`
    # pattern covers many concrete tags (e.g. "deploy@{major}"), `ref_pattern` holds the literal
    # pattern string and is stored as `git.ref_pattern` on all content and refs docs.  `ref`
    # advances to the resolved concrete tag post-clone.  For all other units (branches, snapshots,
    # concrete tags) `ref_pattern` == `ref` so the split is transparent to non-stream code paths.
    ref_pattern: str | None = None

    @property
    def label(self) -> str:
        return f"{self.host}/{self.org}/{self.repo} @ {self.ref or '?'} ({self.kind})"

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at


# rich text style per terminal status (the glyph + wording live in _completion_text).
_STATUS_STYLE = {
    "indexed": "green",
    "tagged": "cyan",
    "recorded": "cyan",
    "skipped": "dim",
    "error": "red",
}


def _bar(frac: float, width: int = 24) -> str:
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    filled = int(frac * width)
    pct = int(frac * 100)  # floor: only reads 100% at frac == 1.0, aligning with the glyph fill
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:3d}%"


class ProgressReporter:
    """No-op base reporter. Records plan + per-unit state via the methods the
    indexer calls but emits nothing; subclasses add a live region, plain line
    output, or (for the quiet reporter) stderr-only error reporting.
    """

    def __init__(self) -> None:
        self.units: list[Unit] = []
        self.total = 0
        self.start_time = time.monotonic()
        self._lock = threading.Lock()

    # -- context manager (live region lifecycle) --
    def __enter__(self) -> "ProgressReporter":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    # -- state transitions --
    def planning(self, text: str) -> None:
        pass

    def set_plan(self, units: list[Unit]) -> None:
        self.units = units
        self.total = len(units)

    def add_units(self, new_units: list["Unit"]) -> None:
        """Append units discovered after set_plan (e.g. per-commit expansions of a branch
        with `since`). Thread-safe: process_group workers call this during Phase 2 after the
        plan is already set. `total` is updated so progress fractions stay correct."""
        with self._lock:
            self.units.extend(new_units)
            self.total += len(new_units)

    def drop_units(self, units_to_drop: list["Unit"]) -> None:
        """Silently remove units from the plan so they produce no completion line and don't
        appear in the summary. Used for refs that are retain-doomed (they will be pruned) so
        they are simply not listed rather than shown as 'already indexed, skipped'. Thread-safe:
        called from process_group workers alongside add_units. `total` is decremented so progress
        fractions stay correct."""
        drop_set = set(id(u) for u in units_to_drop)
        with self._lock:
            kept = [u for u in self.units if id(u) not in drop_set]
            self.total -= len(self.units) - len(kept)
            self.units = kept

    def reorder_group(self, host: str, org: str, repo: str, dates: dict[tuple[str, str], int]) -> None:
        """Reorder this repo's units newest-first by creation date to match the
        actual indexing order (which also uses `dates`). Called once per repo after
        its clone, when creatordate information first becomes available. Thread-safe:
        the Rich Live render loop reads self.units ~4x/sec from a different thread."""
        with self._lock:
            idxs = [i for i, u in enumerate(self.units)
                    if u.host == host and u.org == org and u.repo == repo]
            group = sorted(
                (self.units[i] for i in idxs),
                key=lambda u: dates.get((u.kind, u.ref or ""), -1),
                reverse=True,
            )
            for i, u in zip(idxs, group):
                self.units[i] = u

    def start(self, unit: Unit) -> None:
        unit.started_at = time.monotonic()
        unit.stage = "resolving"

    def set_stage(self, unit: Unit, stage: str) -> None:
        unit.stage = stage
        # The per-ref timer starts when this ref's own work begins (checkout),
        # not at start(): start() fires for every ref of a repo up front during
        # the batched pre-clone skip pass, and the clone that follows is shared
        # across the repo's refs -- counting either would fold other refs' time
        # into this one's elapsed (the cumulative-looking duration bug).
        if stage == "checkout":
            unit.started_at = time.monotonic()

    def set_total_files(self, unit: Unit, n: int) -> None:
        unit.total_files = n

    def update_counts(self, unit: Unit, files: int, lines: int) -> None:
        unit.files = files
        unit.lines = lines

    def finish(self, unit: Unit, status: str, files: int = 0, lines: int = 0, detail: str | None = None) -> None:
        unit.status = status
        unit.stage = "done"
        unit.files = files
        unit.lines = lines
        unit.detail = detail
        unit.finished_at = time.monotonic()

    # -- shared formatting --
    def _completion_text(self, unit: Unit) -> str:
        counts = f"{unit.files:,} files, {unit.lines:,} lines"
        if unit.status == "indexed":
            return f"✓ {unit.label} - indexed {counts} ({format_elapsed(unit.elapsed())})"
        if unit.status == "tagged":
            return f"✓ {unit.label} - tagged existing content ({counts})"
        if unit.status == "recorded":
            return f"✓ {unit.label} - content already indexed, recorded ref ({counts})"
        if unit.status == "no-changes":
            return f"• {unit.label} - no changes, skipped"
        if unit.status == "skipped":
            if unit.detail:
                return f"• {unit.label} - skipped ({unit.detail})"
            return f"• {unit.label} - already indexed, skipped"
        if unit.status == "error":
            return f"✗ {unit.label} - error: {unit.detail}"
        return f"• {unit.label} - {unit.status}"

    def _plan_lines(self, units: list[Unit]) -> list[str]:
        n_repos = len({(u.host, u.org, u.repo) for u in units})
        lines = [f"Plan: {n_repos} repo(s), {len(units)} ref(s)"]
        # Use the same inline label as the progress lines (org/repo @ ref (kind))
        # so each entry is self-describing without relying on a repo header above it.
        lines.extend(f"  {u.label}" for u in units)
        return lines

    def _summary_text(self) -> str:
        by = Counter(u.status for u in self.units if u.status)
        files = sum(u.files for u in self.units)
        lines = sum(u.lines for u in self.units)
        order = [("indexed", "indexed"), ("tagged", "tagged"), ("recorded", "recorded"),
                 ("skipped", "skipped"), ("no-changes", "no changes"), ("error", "failed")]
        parts = [f"{by[k]} {label}" for k, label in order if by.get(k)]
        body = ", ".join(parts) or "nothing to do"
        return f"Done in {format_elapsed(time.monotonic() - self.start_time)} - {body}; {files:,} files, {lines:,} lines"


class NullProgressReporter(ProgressReporter):
    """Quiet/programmatic mode: nothing on stdout, but errors still go to stderr so
    a non-zero exit is accompanied by a diagnosable message."""

    def finish(self, unit: Unit, status: str, files: int = 0, lines: int = 0, detail: str | None = None) -> None:
        super().finish(unit, status, files, lines, detail)
        if status == "error":
            click.echo(f"Error indexing {unit.label}: {detail}", err=True)


class PlainProgressReporter(ProgressReporter):
    """Non-TTY / piped output: one line per stage transition and completion, no
    live region or ANSI redraws (so logs and CI capture cleanly)."""

    def planning(self, text: str) -> None:
        click.echo(f"Resolving refs: {text}")

    def set_plan(self, units: list[Unit]) -> None:
        super().set_plan(units)
        for line in self._plan_lines(units):
            click.echo(line)

    def set_stage(self, unit: Unit, stage: str) -> None:
        super().set_stage(unit, stage)
        if stage == "checkout":
            click.echo(f"Checking out {unit.label} ...")
        elif stage == "diffing":
            click.echo(f"Diffing {unit.label} ...")
        elif stage == "indexing":
            click.echo(f"Indexing {unit.label} ...")

    def finish(self, unit: Unit, status: str, files: int = 0, lines: int = 0, detail: str | None = None) -> None:
        super().finish(unit, status, files, lines, detail)
        click.echo(self._completion_text(unit), err=(status == "error"))

    def __exit__(self, *exc) -> bool:
        click.echo(self._summary_text())
        return False


class _Dashboard:
    """rich renderable rebuilt on every refresh tick (rich calls __rich__ each
    time), so the elapsed timers advance even while a step is blocked."""

    def __init__(self, reporter: "RichProgressReporter") -> None:
        self.r = reporter

    def __rich__(self):
        r = self.r
        elapsed = format_elapsed(time.monotonic() - r.start_time)
        rows = []

        if r.total == 0:
            text = r._planning_text or "preparing"
            rows.append(Text(f"⏳ Resolving refs - {text} · {elapsed}", style="bold"))
            return Group(*rows)

        with r._lock:
            units = list(r.units)
        completed = sum(1 for u in units if u.status)
        files = sum(u.files for u in units)
        lines = sum(u.lines for u in units)
        head = Text()
        head.append("Indexing ", style="bold")
        head.append(f"{completed}/{r.total} refs", style="bold cyan")
        head.append(f"  {_bar(completed / r.total)}  ")
        head.append(f"files {files:,} · lines {lines:,} · {elapsed}")
        rows.append(head)

        working = [u for u in units if u.status is None and u.started_at is not None]

        # One line per ref actually occupying a worktree slot (checkout/diffing/indexing).
        # Several refs of the SAME repo can be in these stages at once now (each in its own
        # `git worktree` slot -- see git.ensure_worktree / command.py's ref_executor), so
        # unlike the old one-line-per-repo rendering this can show them side by side; the
        # count is naturally bounded by INDEX_REF_CONCURRENCY, since that's how many refs can
        # reach these stages at the same time. Sorted by start time for a stable row order
        # across refresh ticks (rows would otherwise jump around as units list order shifts).
        active = sorted(
            (u for u in working if u.stage in ("checkout", "diffing", "indexing")),
            key=lambda u: u.started_at,
        )
        for u in active:
            rows.append(self._unit_line(u))

        # One line per repo that has nothing of its own actively checking out/indexing yet --
        # still cloning (the clone is shared across a repo's refs, so name the repo once
        # rather than an arbitrary ref of it), or every ref of it is still queued as
        # "resolving" ahead of that clone. Picks the first such unit per repo in plan order.
        active_repos = {(u.host, u.org, u.repo) for u in active}
        seen_repos: set[tuple[str, str, str]] = set()
        for u in working:
            key = (u.host, u.org, u.repo)
            if key in active_repos or key in seen_repos:
                continue
            seen_repos.add(key)
            rows.append(self._unit_line(u))
        return Group(*rows)

    def _unit_line(self, u: Unit) -> "Text":
        t = Text("  → ")
        if u.stage == "cloning":
            # The clone is shared across all of a repo's refs; don't name a
            # specific (typically oldest) ref -- show the repo instead.
            t.append(f"{u.host}/{u.org}/{u.repo}", style="bold")
            t.append(" - cloning…")
            return t
        t.append(u.label, style="bold")
        if u.stage == "checkout":
            t.append(" - checking out…")
        elif u.stage == "resolving":
            t.append(" - resolving…")
        elif u.stage == "diffing":
            # Delta mode only: the diff and the delete-by-query of removed paths both run
            # before the file total is known. Naming the stage keeps that work from rendering
            # as a motionless "indexing 0 files · 0 lines", which reads as stalled ingest.
            t.append(f" - diffing… · {format_elapsed(u.elapsed())}")
        elif u.stage == "indexing":
            if u.total_files:
                # Cap the displayed fraction at 0.99 while the unit is still
                # in-progress: the denominator (count_tracked_files) and
                # numerator (emitted file-docs) can diverge (binary/skipped
                # files), so the raw ratio can reach 1.0 before finish() fires.
                # The floored _bar already prevents early "100%" in the common
                # case; this cap makes it robust against denominator overshoot.
                frac = min(u.files / u.total_files, 0.99)
                t.append(f" - indexing {_bar(frac, 16)} "
                         f"{u.files:,}/{u.total_files:,} files · {u.lines:,} lines · "
                         f"{format_elapsed(u.elapsed())}")
            else:
                t.append(f" - indexing {u.files:,} files · {u.lines:,} lines · "
                         f"{format_elapsed(u.elapsed())}")
        return t


class RichProgressReporter(ProgressReporter):
    """TTY reporter: a rich.Live region (overall bar + current ref) auto-refreshed
    ~4x/sec, with the plan and per-ref completions printed as permanent lines above."""

    def __init__(self) -> None:
        super().__init__()
        self.console = Console()
        self._planning_text: str | None = None
        self.live = Live(_Dashboard(self), console=self.console, refresh_per_second=4, auto_refresh=True)

    def __enter__(self) -> "RichProgressReporter":
        self.live.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.live.stop()
        self.console.print(Text(self._summary_text(), style="bold"))
        return False

    def planning(self, text: str) -> None:
        self._planning_text = text

    def set_plan(self, units: list[Unit]) -> None:
        super().set_plan(units)
        self._planning_text = None
        lines = self._plan_lines(units)
        self.console.print(Text(lines[0], style="bold"))
        for line in lines[1:]:
            self.console.print(Text(line, style="dim"))

    def finish(self, unit: Unit, status: str, files: int = 0, lines: int = 0, detail: str | None = None) -> None:
        super().finish(unit, status, files, lines, detail)
        self.console.print(Text(self._completion_text(unit), style=_STATUS_STYLE.get(status, "dim")))


def make_reporter(quiet: bool) -> ProgressReporter:
    """Null when quiet, Rich on an interactive TTY, Plain otherwise."""
    if quiet:
        return NullProgressReporter()
    if _HAS_RICH and sys.stdout.isatty():
        return RichProgressReporter()
    return PlainProgressReporter()


# --- Prune progress reporting -----------------------------------------------------------
#
# `prune`'s orphan-sweep planning pass (commands/prune/execute.py's plan_orphans_now) is a
# handful of coarse, named phases (list indices, scan refs, gather per-index content, ...)
# rather than the indexer's hundreds of small per-ref units, so it gets its own reporter
# hierarchy instead of reusing Unit/ProgressReporter above. It mirrors that hierarchy's
# Rich/Plain/Null split (see make_prune_reporter) so quiet and non-TTY behaviour come free the
# same way. Phases can run concurrently -- plan_orphans_now parallelizes its mutually
# independent gathers -- so more than one can be "active" at once; state is guarded by a lock
# the same way ProgressReporter guards `units`.


@dataclasses.dataclass
class PrunePhase:
    """One named step of prune's read-only planning pass plus its live/terminal state. `total`
    is set when the step has a known unit count up front (e.g. one composite aggregation per
    already-listed index) so its progress is a real fraction rather than a spinner; left None
    for steps whose cost isn't unit-countable (e.g. a single refs scan)."""

    name: str
    total: int | None = None
    current: int = 0
    detail: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at


class _PrunePhaseHandle:
    """Context manager returned by PruneReporter.phase(); lets the caller report a step's
    progress (advance/set_detail) while it runs, then records completion (or the exception
    that aborted it) on exit. Never suppresses an exception."""

    def __init__(self, reporter: "PruneReporter", phase: PrunePhase) -> None:
        self._reporter = reporter
        self.phase = phase

    def advance(self, n: int = 1) -> None:
        self._reporter._update(self.phase, current_delta=n)

    def set_detail(self, detail: str) -> None:
        self._reporter._update(self.phase, detail=detail)

    def __enter__(self) -> "_PrunePhaseHandle":
        self.phase.started_at = time.monotonic()
        self._reporter._start(self.phase)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.phase.finished_at = time.monotonic()
        if exc_type is not None:
            self.phase.error = str(exc)
        self._reporter._finish(self.phase)
        return False


class PruneReporter:
    """No-op base reporter for prune's read-only planning pass (also used directly as the
    quiet/programmatic reporter -- there's nothing to suppress since the base emits nothing).
    Subclasses render a phase-timing breakdown as plan_orphans_now works through listing
    indices, scanning refs, and gathering per-index content, so a slow prune stays
    self-diagnosing instead of going silent for minutes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self) -> "PruneReporter":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def phase(self, name: str, total: int | None = None) -> _PrunePhaseHandle:
        return _PrunePhaseHandle(self, PrunePhase(name=name, total=total))

    # -- hooks called by _PrunePhaseHandle; no-op by default --
    def _start(self, phase: PrunePhase) -> None:
        pass

    def _update(self, phase: PrunePhase, current_delta: int = 0, detail: str | None = None) -> None:
        with self._lock:
            phase.current += current_delta
            if detail is not None:
                phase.detail = detail

    def _finish(self, phase: PrunePhase) -> None:
        pass


class PlainPruneReporter(PruneReporter):
    """Non-TTY / piped output: one line per phase, printed once it completes (no live
    redraw), so logs and CI capture the breakdown cleanly."""

    def __enter__(self) -> "PlainPruneReporter":
        click.echo("Planning prune...")
        return self

    def _finish(self, phase: PrunePhase) -> None:
        glyph = "✗" if phase.error else "✓"
        detail = f" {phase.detail}" if phase.detail else ""
        suffix = f": {phase.error}" if phase.error else ""
        click.echo(f"  {glyph} {phase.name}{detail} ({format_elapsed(phase.elapsed())}){suffix}",
                  err=bool(phase.error))


class _PruneDashboard:
    """rich renderable rebuilt on every refresh tick; shows one line per currently-active phase
    (there may be several, since plan_orphans_now runs its gathers concurrently)."""

    def __init__(self, reporter: "RichPruneReporter") -> None:
        self.r = reporter

    def __rich__(self):
        with self.r._lock:
            active = list(self.r._active.values())
        if not active:
            return Text("")
        rows = []
        for phase in active:
            elapsed = format_elapsed(phase.elapsed())
            if phase.total:
                frac = min(phase.current / phase.total, 1.0)
                rows.append(Text(f"  ⠹ {phase.name} {_bar(frac, 16)} "
                                 f"{phase.current}/{phase.total} · {elapsed}"))
            else:
                detail = f" {phase.detail}" if phase.detail else ""
                rows.append(Text(f"  ⠹ {phase.name}{detail} · {elapsed}"))
        return Group(*rows)


class RichPruneReporter(PruneReporter):
    """TTY reporter: a rich.Live region showing the currently-active phase(s) as an
    indeterminate/determinate bar, with completed phases printed as permanent lines above
    (same pattern as RichProgressReporter)."""

    def __init__(self) -> None:
        super().__init__()
        self.console = Console()
        self._active: dict[int, PrunePhase] = {}
        self.live = Live(_PruneDashboard(self), console=self.console, refresh_per_second=4, auto_refresh=True)

    def __enter__(self) -> "RichPruneReporter":
        self.console.print(Text("Planning prune...", style="bold"))
        self.live.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.live.stop()
        return False

    def _start(self, phase: PrunePhase) -> None:
        with self._lock:
            self._active[id(phase)] = phase

    def _finish(self, phase: PrunePhase) -> None:
        with self._lock:
            self._active.pop(id(phase), None)
        detail = f"{phase.detail} " if phase.detail else ""
        style = "red" if phase.error else "green"
        glyph = "✗" if phase.error else "✓"
        suffix = f": {phase.error}" if phase.error else ""
        self.console.print(
            Text(f"{glyph} {phase.name:<20} {detail}({format_elapsed(phase.elapsed())}){suffix}", style=style)
        )


def make_prune_reporter(quiet: bool) -> PruneReporter:
    """Null (the PruneReporter base, which is already a no-op) when quiet, Rich on an
    interactive TTY, Plain otherwise -- mirrors make_reporter's selection above."""
    if quiet:
        return PruneReporter()
    if _HAS_RICH and sys.stdout.isatty():
        return RichPruneReporter()
    return PlainPruneReporter()
