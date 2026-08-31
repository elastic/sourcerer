"""Unit tests for sourcerer.progress: the Unit/ProgressReporter plumbing shared by every
reporter backend. Focuses on drop_units bookkeeping and the "skipped" completion text -- the
pieces the retain-doomed reporting fix (silently dropping doomed refs instead of mislabeling
them "already indexed, skipped") depends on -- plus the plain reporter's stage lines, which
are what CI logs actually show."""

# Third-party packages
import pytest

# App packages
from sourcerer.progress import (
    PlainProgressReporter,
    PlainPruneReporter,
    ProgressReporter,
    PruneReporter,
    RichPruneReporter,
    Unit,
    make_prune_reporter,
)


def _unit(ref: str = "v1.0.0-rc.1", kind: str = "tag") -> Unit:
    return Unit(host="github", org="acme", repo="widgets", ref=ref, kind=kind)


class TestSkippedCompletionText:
    def test_skipped_without_detail_reads_already_indexed(self):
        r = ProgressReporter()
        u = _unit()
        r.finish(u, "skipped")
        assert r._completion_text(u) == f"• {u.label} - already indexed, skipped"

    def test_skipped_with_detail_renders_the_real_reason(self):
        # A "skipped" unit with a detail must never render the "already indexed" claim --
        # that's the exact mislabeling this fix addresses for any future skip site that
        # forgets to route doomed/excluded refs through drop_units instead.
        r = ProgressReporter()
        u = _unit()
        r.finish(u, "skipped", detail="pruned by retain")
        text = r._completion_text(u)
        assert "already indexed" not in text
        assert "pruned by retain" in text


class TestDropUnits:
    def test_drop_units_removes_unit_and_decrements_total(self):
        r = ProgressReporter()
        keep = _unit("v1.0.0-rc.70")
        drop = _unit("v1.0.0-rc.7")
        r.set_plan([keep, drop])
        assert r.total == 2

        r.drop_units([drop])

        assert r.units == [keep]
        assert r.total == 1

    def test_dropped_unit_produces_no_completion_line_or_summary_entry(self):
        r = ProgressReporter()
        keep = _unit("v1.0.0-rc.70")
        drop = _unit("v1.0.0-rc.7")
        r.set_plan([keep, drop])
        r.drop_units([drop])
        r.finish(keep, "indexed", files=3, lines=10)

        # The dropped unit was never finish()ed and is no longer in self.units, so it
        # contributes nothing to the summary counts.
        assert "1 indexed" in r._summary_text()
        assert drop not in r.units

    def test_drop_units_is_a_noop_for_units_not_in_the_plan(self):
        r = ProgressReporter()
        keep = _unit("v1.0.0-rc.70")
        r.set_plan([keep])
        r.drop_units([_unit("not-in-plan")])
        assert r.units == [keep]
        assert r.total == 1


class TestPlainReporterStageLines:
    """The plain reporter is what non-TTY consumers (CI) get, so every stage a unit can sit
    in for a noticeable time needs a line here or the log goes silent during it."""

    def test_diffing_stage_announces_itself_and_does_not_claim_indexing(self, capsys):
        # Delta mode holds this stage across the name-status diff and the delete-by-query of
        # removed paths. Announcing it as "Indexing" is the mislabeling that made a long
        # pre-ingest stall read as stalled ingest.
        r = PlainProgressReporter()
        u = _unit()
        r.set_stage(u, "diffing")
        out = capsys.readouterr().out
        assert f"Diffing {u.label} ..." in out
        assert "Indexing" not in out

    def test_indexing_stage_still_announces_itself(self, capsys):
        r = PlainProgressReporter()
        u = _unit()
        r.set_stage(u, "indexing")
        assert f"Indexing {u.label} ..." in capsys.readouterr().out


class TestPruneReporterPhases:
    """PruneReporter.phase(): the context-manager seam plan_orphans_now uses to report
    step-by-step progress. The base reporter is a no-op, but phase bookkeeping (timing,
    advance, set_detail, error capture) must work regardless -- subclasses just render it."""

    def test_phase_records_elapsed_time(self):
        r = PruneReporter()
        with r.phase("listing indices") as p:
            pass
        assert p.phase.started_at is not None
        assert p.phase.finished_at is not None
        assert p.phase.elapsed() >= 0

    def test_advance_increments_current(self):
        r = PruneReporter()
        with r.phase("content by index", total=3) as p:
            p.advance()
            p.advance(2)
        assert p.phase.current == 3

    def test_set_detail_records_detail_text(self):
        r = PruneReporter()
        with r.phase("refs scan") as p:
            p.set_detail("42 repo(s)")
        assert p.phase.detail == "42 repo(s)"

    def test_exception_inside_phase_is_captured_and_reraised(self):
        r = PruneReporter()
        with pytest.raises(ValueError):
            with r.phase("empty-index check") as p:
                raise ValueError("boom")
        assert p.phase.error == "boom"
        assert p.phase.finished_at is not None


class TestPlainPruneReporter:
    def test_enter_announces_planning(self, capsys):
        r = PlainPruneReporter()
        with r:
            pass
        assert "Planning prune" in capsys.readouterr().out

    def test_completed_phase_prints_a_line_with_detail_and_timing(self, capsys):
        r = PlainPruneReporter()
        with r:
            with r.phase("listing indices") as p:
                p.set_detail("42 indices")
        out = capsys.readouterr().out
        assert "listing indices" in out
        assert "42 indices" in out
        assert "✓" in out

    def test_errored_phase_marks_failure_and_goes_to_stderr(self, capsys):
        r = PlainPruneReporter()
        with pytest.raises(RuntimeError):
            with r:
                with r.phase("refs scan"):
                    raise RuntimeError("cluster unreachable")
        captured = capsys.readouterr()
        assert "✗" in captured.err
        assert "cluster unreachable" in captured.err


class TestMakePruneReporter:
    def test_quiet_returns_base_null_reporter(self):
        r = make_prune_reporter(quiet=True)
        assert type(r) is PruneReporter

    def test_non_tty_returns_plain_reporter(self, monkeypatch):
        monkeypatch.setattr("sourcerer.progress.sys.stdout.isatty", lambda: False)
        assert isinstance(make_prune_reporter(quiet=False), PlainPruneReporter)

    def test_tty_returns_rich_reporter(self, monkeypatch):
        monkeypatch.setattr("sourcerer.progress.sys.stdout.isatty", lambda: True)
        assert isinstance(make_prune_reporter(quiet=False), RichPruneReporter)


class TestRichPruneReporterConcurrentPhases:
    """plan_orphans_now runs its gathers concurrently, so more than one phase can be active
    at once -- verify the dashboard state tracks all of them, not just the most recent."""

    def test_multiple_concurrently_active_phases_are_tracked(self):
        r = RichPruneReporter()
        r.live.start()
        try:
            h1 = r.phase("refs scan")
            h2 = r.phase("content by index", total=10)
            h1.__enter__()
            h2.__enter__()
            assert len(r._active) == 2
            h1.__exit__(None, None, None)
            assert len(r._active) == 1
            h2.__exit__(None, None, None)
            assert len(r._active) == 0
        finally:
            r.live.stop()
