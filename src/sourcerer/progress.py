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
    """One (org, repo, ref) indexing target plus its live/terminal state.

    `ref` may be None until a default branch is resolved. `kind` is one of
    branch|tag|commit|default. `status` is set once on completion to one of
    indexed|skipped|tagged|recorded|error.
    """

    org: str
    repo: str
    ref: str | None
    kind: str
    stage: str = "pending"  # pending|resolving|cloning|checkout|indexing|done
    total_files: int | None = None
    files: int = 0
    lines: int = 0
    status: str | None = None
    detail: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    @property
    def label(self) -> str:
        return f"{self.org}/{self.repo} @ {self.ref or '?'} ({self.kind})"

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
    return "[" + "█" * filled + "░" * (width - filled) + f"] {frac * 100:3.0f}%"


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

    def reorder_group(self, org: str, repo: str, dates: dict[tuple[str, str], int]) -> None:
        """Reorder this repo's units newest-first by creation date to match the
        actual indexing order (which also uses `dates`). Called once per repo after
        its clone, when creatordate information first becomes available. Thread-safe:
        the Rich Live render loop reads self.units ~4x/sec from a different thread."""
        with self._lock:
            idxs = [i for i, u in enumerate(self.units) if u.org == org and u.repo == repo]
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
            return f"✓ {unit.label} — indexed {counts} ({format_elapsed(unit.elapsed())})"
        if unit.status == "tagged":
            return f"✓ {unit.label} — tagged existing content ({counts})"
        if unit.status == "recorded":
            return f"✓ {unit.label} — content already indexed, recorded ref ({counts})"
        if unit.status == "skipped":
            return f"• {unit.label} — already indexed, skipped"
        if unit.status == "error":
            return f"✗ {unit.label} — error: {unit.detail}"
        return f"• {unit.label} — {unit.status}"

    def _plan_lines(self, units: list[Unit]) -> list[str]:
        n_repos = len({(u.org, u.repo) for u in units})
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
                 ("skipped", "skipped"), ("error", "failed")]
        parts = [f"{by[k]} {label}" for k, label in order if by.get(k)]
        body = ", ".join(parts) or "nothing to do"
        return f"Done in {format_elapsed(time.monotonic() - self.start_time)} — {body}; {files:,} files, {lines:,} lines"


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
            rows.append(Text(f"⏳ Resolving refs — {text} · {elapsed}", style="bold"))
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

        # Pick the most informative active unit: prefer one that is actually
        # checking out or indexing over one that is only cloning or resolving.
        # This matters because start() marks every ref in a repo as "resolving"
        # up front (before the clone), and set_stage marks just one unit as
        # "cloning" while the rest of the repo's refs are still "resolving" --
        # so without this preference the bar would pin on an older ref for the
        # whole duration of the clone and any newer-first indexing that follows.
        working = [u for u in units if u.status is None and u.started_at is not None]
        active = (
            next((u for u in working if u.stage in ("checkout", "indexing")), None)
            or next((u for u in working if u.stage == "cloning"), None)
            or (working[0] if working else None)
        )
        if active is not None:
            rows.append(self._unit_line(active))
        return Group(*rows)

    def _unit_line(self, u: Unit) -> "Text":
        t = Text("  → ")
        if u.stage == "cloning":
            # The clone is shared across all of a repo's refs; don't name a
            # specific (typically oldest) ref -- show the repo instead.
            t.append(f"{u.org}/{u.repo}", style="bold")
            t.append(" — cloning…")
            return t
        t.append(u.label, style="bold")
        if u.stage == "checkout":
            t.append(" — checking out…")
        elif u.stage == "resolving":
            t.append(" — resolving…")
        elif u.stage == "indexing":
            if u.total_files:
                t.append(f" — indexing {_bar(u.files / u.total_files, 16)} "
                         f"{u.files:,}/{u.total_files:,} files · {u.lines:,} lines · "
                         f"{format_elapsed(u.elapsed())}")
            else:
                t.append(f" — indexing {u.files:,} files · {u.lines:,} lines · "
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
