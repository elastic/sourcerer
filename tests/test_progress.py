"""Unit tests for sourcerer.progress: the Unit/ProgressReporter plumbing shared by every
reporter backend. Focuses on drop_units bookkeeping and the "skipped" completion text -- the
pieces the retain-doomed reporting fix (silently dropping doomed refs instead of mislabeling
them "already indexed, skipped") depends on -- plus the plain reporter's stage lines, which
are what CI logs actually show."""

# App packages
from sourcerer.progress import PlainProgressReporter, ProgressReporter, Unit


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
