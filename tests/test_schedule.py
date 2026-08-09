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
        assert s.value == timedelta(minutes=30)

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


def _matches(rule: ScheduleRule, host: str, org: str, repo: str,
             ref_type: str = "tag", patterns: list[str] | None = None) -> bool:
    """Convenience wrapper so most tests don't have to pass ref_type/patterns."""
    return rule.matches(host, org, repo, ref_type, patterns or ["v{major}.{minor}.{patch}"])


class TestScheduleRuleMatches:
    def test_empty_scope_matches_everything(self):
        rule = ScheduleRule(host=None, org=None, repo=None, schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "elasticsearch")
        assert _matches(rule, "gitlab", "someorg", "somerepo")

    def test_host_only_matches_same_host(self):
        rule = ScheduleRule(host="github", org=None, repo=None, schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "elasticsearch")
        assert not _matches(rule, "gitlab", "elastic", "elasticsearch")

    def test_host_org_matches_same_org(self):
        rule = ScheduleRule(host="github", org="elastic", repo=None, schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "elasticsearch")
        assert not _matches(rule, "github", "other", "elasticsearch")

    def test_host_org_repo_matches_exact(self):
        rule = ScheduleRule(host="github", org="elastic", repo="elasticsearch", schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "elasticsearch")
        assert not _matches(rule, "github", "elastic", "kibana")

    def test_specificity_ordering(self):
        # Exact fields each contribute 2; globs contribute 1; None contributes 0.
        r0 = ScheduleRule(host=None, org=None, repo=None, schedule=Schedule.always())
        r1 = ScheduleRule(host="github", org=None, repo=None, schedule=Schedule.always())
        r2 = ScheduleRule(host="github", org="elastic", repo=None, schedule=Schedule.always())
        r3 = ScheduleRule(host="github", org="elastic", repo="elasticsearch", schedule=Schedule.always())
        assert r0.specificity() == 0
        assert r1.specificity() == 2   # one exact field = 2
        assert r2.specificity() == 4   # two exact fields = 4
        assert r3.specificity() == 6   # three exact fields = 6

    # --- glob matching ---

    def test_glob_org_prefix(self):
        rule = ScheduleRule(host="github", org="elastic-*", repo=None, schedule=Schedule.always())
        assert _matches(rule, "github", "elastic-foo", "myrepo")
        assert _matches(rule, "github", "elastic-bar", "myrepo")
        assert not _matches(rule, "github", "other", "myrepo")

    def test_glob_repo_prefix(self):
        rule = ScheduleRule(host=None, org=None, repo="docs-*", schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "docs-content")
        assert not _matches(rule, "github", "elastic", "elasticsearch")

    def test_glob_bare_star_matches_any(self):
        rule = ScheduleRule(host="github", org="*", repo=None, schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "es")
        assert _matches(rule, "github", "anyorg", "anyrepo")

    def test_exact_value_still_exact(self):
        """A value with no glob chars still requires an exact match."""
        rule = ScheduleRule(host=None, org="elastic", repo=None, schedule=Schedule.always())
        assert _matches(rule, "github", "elastic", "repo")
        assert not _matches(rule, "github", "elasticfoo", "repo")

    # --- ref_type scoping ---

    def test_ref_type_exact_match(self):
        rule = ScheduleRule(host=None, org=None, repo=None, ref_type="branch",
                            schedule=Schedule.always())
        assert _matches(rule, "github", "org", "repo", ref_type="branch")
        assert not _matches(rule, "github", "org", "repo", ref_type="tag")
        assert not _matches(rule, "github", "org", "repo", ref_type="commit")

    def test_ref_type_star_matches_all(self):
        rule = ScheduleRule(host=None, org=None, repo=None, ref_type="*",
                            schedule=Schedule.always())
        assert _matches(rule, "github", "org", "repo", ref_type="branch")
        assert _matches(rule, "github", "org", "repo", ref_type="tag")
        assert _matches(rule, "github", "org", "repo", ref_type="commit")

    def test_ref_type_none_matches_all(self):
        rule = ScheduleRule(host=None, org=None, repo=None, ref_type=None,
                            schedule=Schedule.always())
        assert _matches(rule, "github", "org", "repo", ref_type="branch")
        assert _matches(rule, "github", "org", "repo", ref_type="tag")

    # --- ref scoping (matched against selector match patterns) ---

    def test_ref_glob_matches_pattern(self):
        rule = ScheduleRule(host=None, org=None, repo=None, ref="v*",
                            schedule=Schedule.always())
        assert _matches(rule, "gh", "org", "repo", patterns=["v{major}.{minor}.{patch}"])
        assert not _matches(rule, "gh", "org", "repo", patterns=["main"])

    def test_ref_none_matches_any_pattern(self):
        rule = ScheduleRule(host=None, org=None, repo=None, ref=None,
                            schedule=Schedule.always())
        assert _matches(rule, "gh", "org", "repo", patterns=["main"])
        assert _matches(rule, "gh", "org", "repo", patterns=["v{major}.{minor}.{patch}"])

    def test_ref_matches_any_of_multiple_patterns(self):
        """The rule matches if the ref glob hits at least one of the source's match patterns."""
        rule = ScheduleRule(host=None, org=None, repo=None, ref="v*",
                            schedule=Schedule.always())
        # One pattern matches v*, the other doesn't — should still match
        assert _matches(rule, "gh", "org", "repo",
                        patterns=["main", "v{major}.{minor}.{patch}"])
        # Neither matches
        assert not _matches(rule, "gh", "org", "repo", patterns=["main", "feature-*"])

    # --- specificity with globs ---

    def test_specificity_exact_beats_glob(self):
        exact = ScheduleRule(host="github", org="elastic", repo=None, schedule=Schedule.always())
        glob_ = ScheduleRule(host="github", org="elastic-*", repo=None, schedule=Schedule.always())
        catch = ScheduleRule(host=None, org=None, repo=None, schedule=Schedule.always())
        assert exact.specificity() > glob_.specificity() > catch.specificity()

    def test_specificity_ref_type_exact_vs_star(self):
        exact = ScheduleRule(host=None, org=None, repo=None, ref_type="branch",
                             schedule=Schedule.always())
        star = ScheduleRule(host=None, org=None, repo=None, ref_type="*",
                            schedule=Schedule.always())
        assert exact.specificity() > star.specificity() > 0

    def test_specificity_ref_exact_vs_glob(self):
        exact_ref = ScheduleRule(host=None, org=None, repo=None, ref="main",
                                 schedule=Schedule.always())
        glob_ref = ScheduleRule(host=None, org=None, repo=None, ref="v*",
                                schedule=Schedule.always())
        assert exact_ref.specificity() > glob_ref.specificity()


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

    # --- new fields: ref_type and ref ---

    def test_schedule_ref_type_parsed(self):
        from sourcerer.config import parse_config
        cfg = parse_config({
            "schedules": [
                {"git": {"ref_type": "tag"}, "schedule": "6h"},
                {"git": {"ref_type": "*"}, "schedule": "1d"},
            ],
            "sources": [self._src()],
        })
        assert cfg.schedules[0].ref_type == "tag"
        assert cfg.schedules[1].ref_type == "*"

    def test_schedule_ref_parsed(self):
        from sourcerer.config import parse_config
        cfg = parse_config({
            "schedules": [{"git": {"ref": "v*"}, "schedule": "3h"}],
            "sources": [self._src()],
        })
        assert cfg.schedules[0].ref == "v*"

    def test_schedule_glob_org_parsed(self):
        from sourcerer.config import parse_config
        cfg = parse_config({
            "schedules": [{"git": {"org": "elastic-*"}, "schedule": "3h"}],
            "sources": [self._src()],
        })
        assert cfg.schedules[0].org == "elastic-*"

    def test_schedule_ref_type_partial_glob_rejected(self):
        """ref_type only allows exact values or bare '*' — no partial globs."""
        from sourcerer.config import parse_config
        with pytest.raises(ValueError, match="ref_type"):
            parse_config({
                "schedules": [{"git": {"ref_type": "bra*"}, "schedule": "1d"}],
                "sources": [],
            })

    def test_schedule_ref_type_invalid_value_rejected(self):
        from sourcerer.config import parse_config
        with pytest.raises(ValueError, match="ref_type"):
            parse_config({
                "schedules": [{"git": {"ref_type": "sha"}, "schedule": "1d"}],
                "sources": [],
            })

    def test_schedule_unknown_git_key_rejected(self):
        from sourcerer.config import parse_config
        with pytest.raises(ValueError, match="unknown keys"):
            parse_config({
                "schedules": [{"git": {"bogus": "x"}, "schedule": "1d"}],
                "sources": [],
            })

    def test_dotted_git_ref_type_shorthand(self):
        """Dotted key git.ref_type in a schedule rule is expanded correctly."""
        from sourcerer.config import parse_config
        cfg = parse_config({
            "schedules": [{"git.ref_type": "tag", "schedule": "6h"}],
            "sources": [self._src()],
        })
        assert cfg.schedules[0].ref_type == "tag"


# ---------------------------------------------------------------------------
# resolve_schedule with globs
# ---------------------------------------------------------------------------


def _branch_selector() -> Selector:
    """Minimal Selector with a branch ref_type and a 'main' match pattern."""
    return Selector(
        ref_type="branch",
        raw_patterns=["main"],
        compiled=[compile_pattern("main")],
        since=None,
        retain=None,
        levels=(),
        schedule=None,
    )


class TestResolveScheduleGlob:
    """Tests that verify glob rules and the exact > glob > any specificity hierarchy."""

    def test_exact_org_beats_glob_org(self):
        """An exact org rule wins over a glob org rule at the same level."""
        exact = ScheduleRule(host="github", org="elastic", repo=None,
                             schedule=parse_schedule("1h"))
        glob_ = ScheduleRule(host="github", org="elastic-*", repo=None,
                             schedule=parse_schedule("6h"))
        catch = ScheduleRule(host=None, org=None, repo=None,
                             schedule=parse_schedule("1d"))
        rules = [glob_, catch, exact]  # order shouldn't matter
        sel = _selector()
        result = resolve_schedule("github", "elastic", "elasticsearch", sel, rules)
        assert result.value == timedelta(hours=1)

    def test_glob_org_beats_catch_all(self):
        """A glob org rule is more specific than a catch-all rule."""
        glob_ = ScheduleRule(host=None, org="elastic-*", repo=None,
                             schedule=parse_schedule("6h"))
        catch = ScheduleRule(host=None, org=None, repo=None,
                             schedule=parse_schedule("1d"))
        rules = [catch, glob_]
        sel = _selector()
        result = resolve_schedule("github", "elastic-inner", "somerepo", sel, rules)
        assert result.value == timedelta(hours=6)

    def test_ref_type_scoping(self):
        """A ref_type=tag rule matches tag selectors but not branch selectors."""
        tag_rule = ScheduleRule(host=None, org=None, repo=None, ref_type="tag",
                                schedule=parse_schedule("1h"))
        catch = ScheduleRule(host=None, org=None, repo=None,
                             schedule=parse_schedule("1d"))
        rules = [tag_rule, catch]
        tag_sel = _selector()
        branch_sel = _branch_selector()
        # tag selector gets the tag-specific rule
        assert resolve_schedule("github", "org", "repo", tag_sel, rules).value == timedelta(hours=1)
        # branch selector falls back to the catch-all
        assert resolve_schedule("github", "org", "repo", branch_sel, rules).value == timedelta(days=1)

    def test_ref_scoping_by_match_pattern(self):
        """A ref=v* rule matches selectors whose match patterns start with 'v'."""
        ref_rule = ScheduleRule(host=None, org=None, repo=None, ref="v*",
                                schedule=parse_schedule("2h"))
        catch = ScheduleRule(host=None, org=None, repo=None,
                             schedule=parse_schedule("1d"))
        rules = [ref_rule, catch]
        tag_sel = _selector()          # match: v{major}.{minor}.{patch}
        branch_sel = _branch_selector()    # match: main
        assert resolve_schedule("github", "org", "repo", tag_sel, rules).value == timedelta(hours=2)
        assert resolve_schedule("github", "org", "repo", branch_sel, rules).value == timedelta(days=1)


class TestSourceStateRetryWindow:
    """Tests that source_state honors a custom retry_window when detecting active indexing."""

    def _make_es(self, active_count):
        """Return a mock ES that reports `active_count` actively-indexing refs."""
        from unittest.mock import MagicMock
        es = MagicMock()
        es.search.return_value = {
            "aggregations": {
                "max_indexed_at": {"ts": {"value": None}},
                "active_indexing": {"doc_count": active_count},
            }
        }
        return es

    def test_default_retry_window_used_when_not_supplied(self):
        from sourcerer.commands.index.schedule import RETRY_INTERVAL, source_state
        es = self._make_es(1)
        now = datetime(2026, 8, 9, 18, 0, 0, tzinfo=_UTC)
        state = source_state(es, "github", "acme", "widgets", "branch", now)
        assert state.active_indexing is True
        # The range query uses now - RETRY_INTERVAL (1h by default).
        call = es.search.call_args.kwargs
        aggs = call["aggregations"]
        expected_gte = (now - RETRY_INTERVAL).isoformat()
        assert aggs["active_indexing"]["filter"]["bool"]["filter"][1] == {
            "range": {"indexing_started_at": {"gte": expected_gte}}
        }

    def test_custom_retry_window_overrides_default(self):
        from sourcerer.commands.index.schedule import source_state
        es = self._make_es(1)
        now = datetime(2026, 8, 9, 18, 0, 0, tzinfo=_UTC)
        window = timedelta(minutes=30)
        state = source_state(es, "github", "acme", "widgets", "branch", now, retry_window=window)
        assert state.active_indexing is True
        call = es.search.call_args.kwargs
        aggs = call["aggregations"]
        expected_gte = (now - window).isoformat()
        assert aggs["active_indexing"]["filter"]["bool"]["filter"][1] == {
            "range": {"indexing_started_at": {"gte": expected_gte}}
        }

    def test_short_window_treats_older_marker_as_not_active(self):
        # With a 30m window and 0 active indexing refs -> not active.
        es = self._make_es(0)
        from sourcerer.commands.index.schedule import source_state
        now = datetime(2026, 8, 9, 18, 0, 0, tzinfo=_UTC)
        state = source_state(es, "github", "acme", "widgets", "branch", now,
                             retry_window=timedelta(minutes=30))
        assert state.active_indexing is False
