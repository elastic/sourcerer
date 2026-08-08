"""Unit tests for scheduling: parse_schedule, Schedule.due, resolve_schedule, and
ScheduleRule matching. All pure-logic tests (no Elasticsearch needed)."""

# Standard packages
from datetime import datetime, timedelta, timezone

# Third-party packages
import pytest

# App packages
from sourcerer.config import (
    Schedule,
    ScheduleRule,
    Selector,
    parse_schedule,
    resolve_schedule,
)
from sourcerer.version import compile_pattern

_UTC = timezone.utc


def _utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=_UTC)


def _selector(schedule: Schedule | None = None) -> Selector:
    """Minimal Selector with a tag ref_type and a concrete match pattern."""
    return Selector(
        ref_type="tag",
        raw_patterns=["v{major}.{minor}.{patch}"],
        compiled=[compile_pattern("v{major}.{minor}.{patch}")],
        since=None,
        retain=None,
        levels=("major", "minor", "patch"),
        schedule=schedule,
    )


# ---------------------------------------------------------------------------
# parse_schedule
# ---------------------------------------------------------------------------


class TestParseSchedule:
    def test_duration_3h(self):
        s = parse_schedule("3h")
        assert s.kind == "duration"
        assert s.value == timedelta(hours=3)

    def test_duration_1d(self):
        s = parse_schedule("1d")
        assert s.kind == "duration"
        assert s.value == timedelta(days=1)

    def test_duration_30m(self):
        s = parse_schedule("30m")
        assert s.kind == "duration"
        assert s.value == timedelta(seconds=30 * 2592000)  # 30 * 30-day months

    def test_cron_every_hour(self):
        s = parse_schedule("0 * * * *")
        assert s.kind == "cron"
        assert s.value == "0 * * * *"

    def test_cron_every_3_hours(self):
        s = parse_schedule("0 */3 * * *")
        assert s.kind == "cron"
        assert s.value == "0 */3 * * *"

    def test_cron_nightly(self):
        s = parse_schedule("0 2 * * *")
        assert s.kind == "cron"
        assert s.value == "0 2 * * *"

    def test_always_constant(self):
        s = Schedule.always()
        assert s.kind == "cron"
        assert s.value == "* * * * *"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="invalid schedule"):
            parse_schedule("not a schedule")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="invalid schedule"):
            parse_schedule("")

    def test_partial_cron_raises(self):
        # Only 4 fields — not a valid 5-field cron
        with pytest.raises(ValueError, match="invalid schedule"):
            parse_schedule("0 * * *")


# ---------------------------------------------------------------------------
# Schedule.due — never indexed
# ---------------------------------------------------------------------------


class TestScheduleDueNeverIndexed:
    def test_duration_never_indexed_is_due(self):
        s = parse_schedule("1h")
        assert s.due(last=None, now=_utc(2026, 1, 1))

    def test_cron_never_indexed_is_due(self):
        s = parse_schedule("0 * * * *")
        assert s.due(last=None, now=_utc(2026, 1, 1))


# ---------------------------------------------------------------------------
# Schedule.due — duration
# ---------------------------------------------------------------------------


class TestScheduleDueDuration:
    def test_exactly_elapsed_is_due(self):
        s = parse_schedule("3h")
        last = _utc(2026, 1, 1, 9, 0)
        now = _utc(2026, 1, 1, 12, 0)  # exactly 3h later
        assert s.due(last=last, now=now)

    def test_more_than_elapsed_is_due(self):
        s = parse_schedule("3h")
        last = _utc(2026, 1, 1, 9, 0)
        now = _utc(2026, 1, 1, 13, 0)  # 4h later
        assert s.due(last=last, now=now)

    def test_less_than_elapsed_is_not_due(self):
        s = parse_schedule("3h")
        last = _utc(2026, 1, 1, 9, 0)
        now = _utc(2026, 1, 1, 11, 59)  # only 2h59m later
        assert not s.due(last=last, now=now)

    def test_just_under_elapsed_is_not_due(self):
        s = parse_schedule("1d")
        last = _utc(2026, 1, 1, 12, 0)
        now = _utc(2026, 1, 2, 11, 59)  # 23h59m later
        assert not s.due(last=last, now=now)


# ---------------------------------------------------------------------------
# Schedule.due — cron
# ---------------------------------------------------------------------------


class TestScheduleDueCron:
    def test_cron_tick_has_passed(self):
        # Every hour on the hour; last indexed at 9:05; now 10:02 — the 10:00 tick passed.
        s = parse_schedule("0 * * * *")
        last = _utc(2026, 1, 1, 9, 5)
        now = _utc(2026, 1, 1, 10, 2)
        assert s.due(last=last, now=now)

    def test_cron_tick_has_not_passed(self):
        # Every hour on the hour; last indexed at 9:55; now 10:30 — the 10:00 tick passed...
        # wait, that WOULD fire. Let me set last at 9:01 and now at 9:55 — no 10:00 yet.
        s = parse_schedule("0 * * * *")
        last = _utc(2026, 1, 1, 9, 1)
        now = _utc(2026, 1, 1, 9, 55)
        # Next tick after 9:01 is 10:00, which is after now (9:55).
        assert not s.due(last=last, now=now)

    def test_cron_exactly_at_tick(self):
        # "0 * * * *" with last at 9:00 and now at 10:00 — the 10:00 tick is exactly now.
        s = parse_schedule("0 * * * *")
        last = _utc(2026, 1, 1, 9, 0)
        now = _utc(2026, 1, 1, 10, 0)
        # Next tick after 9:00 is 10:00 == now -> due.
        assert s.due(last=last, now=now)

    def test_cron_nightly_past_midnight(self):
        # Nightly at 2am; last indexed yesterday 2:00; now today 3:00 — the 2am tick passed.
        s = parse_schedule("0 2 * * *")
        last = _utc(2026, 1, 1, 2, 0)
        now = _utc(2026, 1, 2, 3, 0)
        assert s.due(last=last, now=now)

    def test_cron_nightly_before_next_tick(self):
        # Nightly at 2am; last indexed today 2:05; now today 3:00 — next tick is tomorrow 2am.
        s = parse_schedule("0 2 * * *")
        last = _utc(2026, 1, 1, 2, 5)
        now = _utc(2026, 1, 1, 3, 0)
        assert not s.due(last=last, now=now)


# ---------------------------------------------------------------------------
# ScheduleRule matching and resolve_schedule
# ---------------------------------------------------------------------------


class TestScheduleRuleMatches:
    def test_empty_scope_matches_everything(self):
        rule = ScheduleRule(host=None, org=None, repo=None, schedule=Schedule.always())
        assert rule.matches("github", "elastic", "elasticsearch")
        assert rule.matches("gitlab", "someorg", "somerepo")

    def test_host_only_matches_same_host(self):
        rule = ScheduleRule(host="github", org=None, repo=None, schedule=Schedule.always())
        assert rule.matches("github", "elastic", "elasticsearch")
        assert not rule.matches("gitlab", "elastic", "elasticsearch")

    def test_host_org_matches_same_org(self):
        rule = ScheduleRule(host="github", org="elastic", repo=None, schedule=Schedule.always())
        assert rule.matches("github", "elastic", "elasticsearch")
        assert not rule.matches("github", "other", "elasticsearch")

    def test_host_org_repo_matches_exact(self):
        rule = ScheduleRule(host="github", org="elastic", repo="elasticsearch", schedule=Schedule.always())
        assert rule.matches("github", "elastic", "elasticsearch")
        assert not rule.matches("github", "elastic", "kibana")

    def test_specificity_ordering(self):
        r0 = ScheduleRule(host=None, org=None, repo=None, schedule=Schedule.always())
        r1 = ScheduleRule(host="github", org=None, repo=None, schedule=Schedule.always())
        r2 = ScheduleRule(host="github", org="elastic", repo=None, schedule=Schedule.always())
        r3 = ScheduleRule(host="github", org="elastic", repo="elasticsearch", schedule=Schedule.always())
        assert r0.specificity() == 0
        assert r1.specificity() == 1
        assert r2.specificity() == 2
        assert r3.specificity() == 3


class TestResolveSchedule:
    def _rules(self):
        return [
            ScheduleRule(
                host="github", org="elastic", repo=None,
                schedule=parse_schedule("0 */3 * * *"),
            ),
            ScheduleRule(
                host=None, org=None, repo=None,
                schedule=parse_schedule("1d"),
            ),
        ]

    def test_selector_schedule_wins(self):
        sel_sched = parse_schedule("0 0 * * *")
        sel = _selector(schedule=sel_sched)
        result = resolve_schedule("github", "elastic", "elasticsearch", sel, self._rules())
        assert result is sel_sched

    def test_most_specific_rule_wins(self):
        sel = _selector(schedule=None)
        result = resolve_schedule("github", "elastic", "elasticsearch", sel, self._rules())
        # github+elastic is more specific than the catch-all 1d rule
        assert result.kind == "cron"
        assert result.value == "0 */3 * * *"

    def test_catch_all_rule_fallback(self):
        sel = _selector(schedule=None)
        result = resolve_schedule("gitlab", "someorg", "somerepo", sel, self._rules())
        # No host-specific rule matches; catch-all 1d wins
        assert result.kind == "duration"
        assert result.value == timedelta(days=1)

    def test_no_rules_returns_always(self):
        sel = _selector(schedule=None)
        result = resolve_schedule("github", "elastic", "elasticsearch", sel, [])
        assert result.kind == "cron"
        assert result.value == "* * * * *"

    def test_empty_rule_matches_all_sources(self):
        """A single empty-scope rule (no git block) matches every source."""
        rules = [ScheduleRule(host=None, org=None, repo=None, schedule=parse_schedule("6h"))]
        sel = _selector(schedule=None)
        result = resolve_schedule("anhost", "anorg", "anrepo", sel, rules)
        assert result.kind == "duration"
        assert result.value == timedelta(hours=6)


# ---------------------------------------------------------------------------
# Config parsing — schedules section
# ---------------------------------------------------------------------------


class TestParseConfigSchedules:
    def _src(self, **kwargs):
        base = {
            "git": {"host": "github", "org": "elastic", "repo": "elasticsearch", "ref_type": "tag"},
            "match": "v{major}.{minor}.{patch}",
        }
        base.update(kwargs)
        return base

    def test_schedules_section_parsed(self):
        from sourcerer.config import parse_config
        cfg = parse_config({
            "schedules": [
                {"git": {"host": "github"}, "schedule": "0 */3 * * *"},
                {"schedule": "1d"},
            ],
            "sources": [self._src()],
        })
        assert len(cfg.schedules) == 2
        assert cfg.schedules[0].host == "github"
        assert cfg.schedules[0].schedule.kind == "cron"
        assert cfg.schedules[1].host is None
        assert cfg.schedules[1].schedule.kind == "duration"

    def test_sources_schedule_field_parsed(self):
        from sourcerer.config import parse_config
        cfg = parse_config({"sources": [self._src(schedule="0 0 * * *")]})
        assert cfg.repos[0].selectors[0].schedule is not None
        assert cfg.repos[0].selectors[0].schedule.kind == "cron"

    def test_invalid_source_schedule_raises(self):
        from sourcerer.config import parse_config
        with pytest.raises(ValueError, match="invalid schedule"):
            parse_config({"sources": [self._src(schedule="not-a-schedule")]})

    def test_invalid_schedules_entry_raises(self):
        from sourcerer.config import parse_config
        with pytest.raises(ValueError, match="invalid schedule"):
            parse_config({"schedules": [{"schedule": "bad"}], "sources": []})

    def test_schedules_entry_missing_schedule_raises(self):
        from sourcerer.config import parse_config
        with pytest.raises(ValueError, match="'schedule' is required"):
            parse_config({"schedules": [{"git": {"host": "github"}}], "sources": []})

    def test_no_schedules_empty_list(self):
        from sourcerer.config import parse_config
        cfg = parse_config({"sources": [self._src()]})
        assert cfg.schedules == []
