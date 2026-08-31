"""sourcerer.yml model + loader, shared by `index`, `prune`, and `setup`.

Two optional top-level sections:

    hosts:                         # override/extend the built-in git-host defaults
    - id: github
      name: GitHub
      url: { clone: ..., directory: ..., file: ..., line: ..., line_range: ... }

    sources:                       # what to index
    - git:
        host: github               # single required host id (a git.host value)
        org: elastic               # single required org
        repo: elasticsearch        #
        ref_type: tag              # single required: branch | tag | commit
      match: <pattern> | [<pattern>, ...]     # version.py DSL; a ref matches if ANY hits
      since:                                   # inclusion floor (index-side); at most ONE of:
        age: 1y                                #   commit within this age of now
        date: 2025-01-01                       #   commit on/after this date
        commit: <sha>                          #   from this commit
        ref: v8.0.0                            #   from the commit this ref points to
      retain:                                  # omit -> keep forever. Prune if ANY fires:
        age: 2y                                #   keep commits within this age, prune older
        count: 10                              #   keep newest N by commit date, prune rest
        version:                               #   value-relative, needs versioned match:
          majors: 2                            #     keep newest 2 major values (latest + n-1)
          minors: null                         #     null/omit -> no constraint
          patches: 1                           #     newest patch per (major, minor)
          builds: null
        prerelease: superseded | keep          #   sibling of version; default keep

For `git.ref_type: commit`, `match` holds one or more commit SHA/prefixes (7-40 hex chars each);
`since` is not allowed and only `retain.age` (or omitting retain) is valid.

Dotted keys are accepted as flat shorthand for nesting, e.g. `git.host: github`, `since.ref:
v8.0.0`, or `retain.version.majors: 2`. Dotted and nested forms may be mixed; conflicting values
raise the same errors as duplicate nested keys.

Sources sharing the same (host, org, repo) are grouped into one RepoConfig (a list of selectors),
so cross-selector union retention semantics (see planner.plan_repo) hold across a repo's sources.

Duration units: s, h, d, w, m (=30d month), y (=365d year).
"""

from __future__ import annotations

# Standard packages
import fnmatch
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Union

# Third-party packages
import yaml
from croniter import croniter

# App packages
from .hosts import _FORBIDDEN_HOST_CHARS, Host, resolve_hosts, validate_host_id
from .version import (
    CompiledPattern,
    Version,
    compile_pattern,
    match_version,
    parse_bound,
    render_suffix,
    strip_suffix_tokens,
    suffix_template_tokens,
    version_range_keep,
)

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d|w|M|y)\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
_NUMERIC_LEVELS = ("major", "minor", "patch", "build")
_PLURAL_TO_LEVEL = {"majors": "major", "minors": "minor", "patches": "patch", "builds": "build"}
# A commit selector's `match` entries are SHA prefixes, not version-DSL patterns: 7 hex chars is
# git's own "short hash" convention (the shortest form `git rev-parse --short` will produce), and
# rejecting anything shorter bounds how many distinct commits a single prefix could collide with.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_GLOB_CHARS = frozenset("*?[")


def _has_glob(s: str) -> bool:
    """Return True if `s` contains any fnmatch wildcard character."""
    return any(c in _GLOB_CHARS for c in s)


def _scope_field_matches(pattern: str | None, value: str) -> bool:
    """Test a single schedule-rule scope field against a concrete source value.

    - ``None`` (field omitted): matches any value.
    - A pattern with no glob chars: exact equality (same as before).
    - A pattern with glob chars (``*``, ``?``, ``[…]``): fnmatch case-sensitive glob.

    This means bare ``*`` and existing exact strings keep backward-compatible behaviour
    (bare ``*`` is a glob that matches everything; an exact string matches only itself),
    while ``elastic-*`` / ``docs-?`` etc. enable partial-prefix matching.
    """
    if pattern is None:
        return True
    return fnmatch.fnmatchcase(value, pattern)


@dataclass(frozen=True)
class Schedule:
    """Parsed schedule value: either a duration interval or a cron expression.

    `kind` is "duration" or "cron". `value` is a timedelta (duration) or a cron string (cron).

    `due(last, now)` returns True if the schedule has fired since `last`:
      - never indexed (last is None) -> always due.
      - duration: due if now - last >= value.
      - cron: due if the first cron tick after `last` is at or before `now`.
    """
    kind: str          # "duration" | "cron"
    value: Union[timedelta, str]  # timedelta for duration, cron expr string for cron

    def due(self, last: datetime | None, now: datetime) -> bool:
        if last is None:
            return True
        if self.kind == "duration":
            return (now - last) >= self.value  # type: ignore[operator]
        # cron: has the next scheduled tick after `last` already passed?
        # croniter is exclusive on the start -- get_next returns the first tick strictly after last.
        try:
            nxt = croniter(self.value, last).get_next(datetime)
            # get_next returns a naive datetime; make it timezone-aware to match `now`.
            if nxt.tzinfo is None and now.tzinfo is not None:
                nxt = nxt.replace(tzinfo=now.tzinfo)
            return nxt <= now
        except Exception:
            return True  # parse failure at runtime -> treat as always due (fail open)

    @staticmethod
    def always() -> "Schedule":
        """The default 'always due' schedule: fires every minute (cron '* * * * *')."""
        return Schedule(kind="cron", value="* * * * *")


def parse_schedule(text) -> "Schedule":
    """Parse a schedule string (cron or duration) into a Schedule.

    Accepts a 5-field cron expression (e.g. '0 */3 * * *') or a duration string in the same
    syntax as `retain.age` / `since.age` (e.g. '15m', '3h', '1d'). Raises ValueError if the
    text is neither.
    """
    s = str(text).strip()
    if _DURATION_RE.match(s):
        return Schedule(kind="duration", value=parse_duration(s))
    if croniter.is_valid(s):
        return Schedule(kind="cron", value=s)
    raise ValueError(
        f"invalid schedule {s!r}: expected a cron expression (e.g. '0 */3 * * *') "
        f"or a duration (e.g. '15m', '3h', '1d')"
    )


@dataclass
class ScheduleRule:
    """A top-level `schedules[i]` entry: an optional git-scope filter plus a parsed schedule.

    A rule with all-None scope fields matches every source (the default-schedule fallback).

    Scope fields (all optional):
    - ``host`` / ``org`` / ``repo``: matched with fnmatch glob against the source's concrete
      values.  A plain string (no glob chars) behaves as before — exact equality.  A glob
      (e.g. ``elastic-*``) enables partial matching.  ``None`` matches any value.
    - ``ref_type``: matched against the source selector's ``ref_type``.  Only the exact values
      ``branch``/``tag``/``commit`` or the bare wildcard ``*`` are accepted (no partial globs
      on an enum field).  ``None`` matches any ref_type.
    - ``ref``: glob-matched against the source selector's raw ``match`` pattern string(s)
      (e.g. ``v{major}.{minor}.{patch}``).  The schedule gate runs before any ls-remote, so
      actual ref names are not yet available; ``ref`` therefore scopes by the configured match
      pattern.  ``None`` matches any source.

    Specificity (higher = more specific, used to pick the winning rule):
    Each of the five scope fields contributes:
    - 2 if set to an exact value (no glob metacharacters);
    - 1 if set to a glob/wildcard pattern (including bare ``*``);
    - 0 if omitted (``None``).
    The field weights are summed.  ``ref_type: "*"`` counts as glob-tier (1);
    ``ref_type: "branch"`` counts as exact (2).
    """
    host: str | None          # if set, glob-matched against sources with this git.host
    org: str | None           # if set, glob-matched against sources with this git.org
    repo: str | None          # if set, glob-matched against sources with this git.repo
    schedule: Schedule
    ref_type: str | None = None  # if set, matches source selector ref_type (exact or "*")
    ref: str | None = None       # if set, glob-matched against the selector's match pattern(s)

    def _field_weight(self, v: str | None) -> int:
        """Return the specificity weight for a single scope field."""
        if v is None:
            return 0
        return 1 if _has_glob(v) else 2

    def specificity(self) -> int:
        """Higher is more specific. Used to pick the winning rule when multiple rules match."""
        return sum(self._field_weight(v)
                   for v in (self.host, self.org, self.repo, self.ref_type, self.ref))

    def matches(self, host: str, org: str, repo: str, ref_type: str,
                match_patterns: list[str]) -> bool:
        """Return True if this rule's scope matches the given source.

        ``match_patterns`` is the selector's raw ``match`` pattern list
        (used to test the ``ref`` scope field before any ls-remote).
        """
        if not _scope_field_matches(self.host, host):
            return False
        if not _scope_field_matches(self.org, org):
            return False
        if not _scope_field_matches(self.repo, repo):
            return False
        # ref_type: None or "*" matches any; exact values matched directly (no partial glob).
        if self.ref_type is not None and self.ref_type != "*" and self.ref_type != ref_type:
            return False
        # ref: matched against the selector's configured match patterns.  The rule matches if
        # its ref glob matches at least one of the source's match pattern strings.
        if self.ref is not None:
            if not any(_scope_field_matches(self.ref, p) for p in match_patterns):
                return False
        return True


def resolve_schedule(
    host: str, org: str, repo: str, selector: "Selector", schedules: list[ScheduleRule]
) -> Schedule:
    """Pick the effective Schedule for a source selector:
      1. sources[i].schedule wins (per-source override).
      2. Most-specific matching schedules[i] rule wins (highest specificity).
         Specificity ranks: exact field (2) > glob field (1) > omitted (0), summed across
         all five scope fields (host, org, repo, ref_type, ref).
      3. Default: Schedule.always() (every minute, i.e. always due when invoked).
    """
    if selector.schedule is not None:
        return selector.schedule
    best: Schedule | None = None
    best_spec = -1
    for rule in schedules:
        if rule.matches(host, org, repo, selector.ref_type, selector.raw_patterns):
            s = rule.specificity()
            if s > best_spec:
                best_spec = s
                best = rule.schedule
    return best if best is not None else Schedule.always()


def parse_duration(text) -> timedelta:
    m = _DURATION_RE.match(str(text))
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. 30s, 15m, 12h, 7d, 2w, 3M, 1y)")
    return timedelta(seconds=int(m.group(1)) * _DURATION_UNITS[m.group(2)])


def parse_date(text) -> datetime:
    try:
        return datetime.fromisoformat(str(text)).replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(f"invalid date {text!r} (use YYYY-MM-DD)") from e


@dataclass
class Since:
    kind: str          # "age" | "date" | "commit" | "ref"
    value: object      # timedelta | datetime | str


@dataclass
class VersionPolicy:
    counts: dict[str, int]   # level -> count (>= 1), only for levels the user set
    prereleases: int | None = None   # keep newest N prereleases per final-version group (by commit date)
    range: str | None = None         # comparator expression, e.g. ">=6.0.0 <7.0.0"; None = no range filter


@dataclass
class Retain:
    age: timedelta | None = None
    count: int | None = None
    version: VersionPolicy | None = None
    prerelease: str = "keep"       # "keep" | "superseded"

    def is_empty(self) -> bool:
        return (self.age is None and self.count is None and self.version is None
                and self.prerelease == "keep")


@dataclass
class Selector:
    ref_type: str
    raw_patterns: list[str]
    compiled: list[CompiledPattern]
    since: Since | None
    retain: Retain | None
    levels: tuple[str, ...] = ()           # numeric levels shared by the versioned match patterns
    schedule: Schedule | None = None       # per-source schedule override (sources[i].schedule)
    # sources[i].mode: the indexing mode for this source -- "snapshot" (default, commit-addressed)
    # or "delta" (ref-addressed, branch or tag). Controls whether since/retain apply and routes
    # the unit to the incremental delta-index path instead of the snapshot flow.
    mode: str = "snapshot"                 # "snapshot" (default) or "delta" (branch or tag)
    # sources[i].index routing (see specs/sourcerer-yml.md): which physical files/lines index this
    # source's content docs land in. Per-source, so two sources sharing a (host, org, repo) may
    # route differently.
    index_level: str = "repo"              # "host" | "org" | "repo" | "commit"
    # May embed version variables ({major}, ... , {prerelease}) -- rendered per matched ref by
    # resolve_index_suffix(), never stored in template form.
    index_suffix: str | None = None        # appended as ^{suffix}; None == no suffix

    def matches(self, ref_type: str, ref: str) -> Version | None:
        if self.ref_type != ref_type:
            return None
        if self.ref_type == "commit":
            # raw_patterns holds normalized (lowercase) SHA prefixes; a ref (the full commit
            # SHA) matches if it starts with any of them. No version components -- a commit
            # selector never drives version/count/prerelease retention.
            ref_l = ref.lower()
            if any(ref_l.startswith(p) for p in self.raw_patterns):
                return Version(ref=ref, components=(), prerelease="")
            return None
        for cp in self.compiled:
            v = match_version(cp, ref)
            if v is not None:
                return v
        return None

    def match_pattern(self, ref_type: str, ref: str) -> tuple[str, Version] | None:
        """Like matches(), but also returns the raw match pattern that matched `ref`.
        Used to set Unit.ref_pattern to the raw sources[i].match string (not the concrete ref)."""
        if self.ref_type != ref_type:
            return None
        if self.ref_type == "commit":
            ref_l = ref.lower()
            for p in self.raw_patterns:
                if ref_l.startswith(p):
                    return p, Version(ref=ref, components=(), prerelease="")
            return None
        for pattern, cp in zip(self.raw_patterns, self.compiled):
            v = match_version(cp, ref)
            if v is not None:
                return pattern, v
        return None

    def since_version_floor(self) -> tuple[int, ...] | None:
        """If `since` is a ref anchor that denotes a version under this (versioned) selector,
        the inclusive version floor to index from -- name-only, so it's applied in Phase 1 and
        shows in the plan. Accepts either the full ref name ('releases/solr/9.10.1') or just
        the bare version ('9.10.1'). None for date-based since (age/date) or a non-version
        anchor, which resolve by commit date post-clone."""
        if self.since is None or self.since.kind != "ref" or not self.levels:
            return None
        value = str(self.since.value)
        for cp in self.compiled:
            if cp.levels:
                v = match_version(cp, value)
                if v is not None:
                    return v.components
        # Bare version (e.g. "9.10.1") given instead of the full ref name: parse its numeric
        # components against this selector's levels. Guard on a digit so a non-version anchor
        # (e.g. a branch name) stays date-based.
        if any(ch.isdigit() for ch in value):
            return parse_bound(value, self.levels)
        return None

    def range_admits(self, v: Version) -> bool:
        """True if this selector's retain.version.range admits version `v` (or there is no range).
        Used at selection time so a windowed selector only claims refs whose version falls within
        its range, letting a sibling selector with a different range (and index.suffix) claim the
        rest.  A selector with no range admits every version it matches.  Versions with empty
        components (non-versioned patterns) pass through: they can't carry a range (rejected at
        config parse time) so there is nothing to test."""
        if self.retain is None or self.retain.version is None or self.retain.version.range is None:
            return True
        return bool(version_range_keep([v], self.retain.version.range, self.levels))

    def resolve_index_suffix(self, v: Version) -> str | None:
        """This selector's index.suffix for one concrete matched ref: a literal suffix as-is, or
        a version template rendered from `v` (e.g. "{major}.{minor}.x" -> "9.5.x").

        Called once per emitted Unit, so every downstream consumer -- the refs marker's
        index_suffix, the physical index name, and the routing comparison that decides whether a
        source migrated -- only ever sees a concrete value. That is what makes a template and the
        equivalent set of hand-written literal suffixes fully interchangeable: swapping one for
        the other cannot change index_suffix, so it triggers no reindex and no prune."""
        if not self.index_suffix:
            return self.index_suffix
        tokens, _ = suffix_template_tokens(self.index_suffix)
        if not tokens:
            return self.index_suffix
        return render_suffix(self.index_suffix, self.levels, v)


@dataclass
class RepoConfig:
    host: str
    org: str
    repo: str
    selectors: list[Selector] = field(default_factory=list)


def _parse_since(raw: dict, ctx: str) -> Since:
    keys = [k for k in ("age", "date", "commit", "ref") if raw.get(k) is not None]
    unknown = set(raw) - {"age", "date", "commit", "ref"}
    if unknown:
        raise ValueError(f"{ctx} since: unknown keys {sorted(unknown)}")
    if len(keys) != 1:
        raise ValueError(f"{ctx} since: set exactly one of age/date/commit/ref, got {keys or 'none'}")
    k = keys[0]
    if k == "age":
        return Since("age", parse_duration(raw["age"]))
    if k == "date":
        return Since("date", parse_date(raw["date"]))
    return Since(k, str(raw[k]))


def _parse_count_level(val, plural: str, ctx: str) -> int | None:
    if val is None:
        return None
    if not isinstance(val, int) or val < 1:
        raise ValueError(f"{ctx} version.{plural}: must be an integer >= 1 or null (got {val!r})")
    return val


def _parse_retain(raw: dict, ctx: str, has_versioned: bool, levels: tuple[str, ...]) -> Retain:
    unknown = set(raw) - {"age", "count", "version", "prerelease"}
    if unknown:
        raise ValueError(f"{ctx} retain: unknown keys {sorted(unknown)}")

    age = parse_duration(raw["age"]) if raw.get("age") is not None else None

    count = None
    if raw.get("count") is not None:
        count = raw["count"]
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"{ctx} retain.count: must be an integer >= 1 (got {count!r})")

    version = None
    if raw.get("version") is not None:
        if not has_versioned:
            raise ValueError(f"{ctx} retain.version: match has no version tokens to compare")
        vraw = raw["version"]
        _VERSION_KEYS = set(_PLURAL_TO_LEVEL) | {"prereleases", "range"}
        vunknown = set(vraw) - _VERSION_KEYS
        if vunknown:
            raise ValueError(f"{ctx} retain.version: unknown levels {sorted(vunknown)} "
                             f"(use {sorted(_VERSION_KEYS)}; prerelease: keep|superseded is a sibling of version)")
        counts = {_PLURAL_TO_LEVEL[p]: _parse_count_level(vraw.get(p), p, ctx) for p in _PLURAL_TO_LEVEL}
        counts = {lvl: n for lvl, n in counts.items() if n is not None}
        prereleases = _parse_count_level(vraw.get("prereleases"), "prereleases", ctx)
        range_expr = None
        if vraw.get("range") is not None:
            raw_range = vraw["range"]
            if not isinstance(raw_range, str):
                raise ValueError(f"{ctx} retain.version.range: must be a string (got {raw_range!r})")
            try:
                version_range_keep([], raw_range, levels)  # validate grammar + arity eagerly
            except ValueError as e:
                raise ValueError(f"{ctx} retain.version.range: {e}") from e
            range_expr = raw_range
        version = VersionPolicy(counts=counts, prereleases=prereleases, range=range_expr)

    prerelease = raw.get("prerelease", "keep")
    if prerelease not in ("keep", "superseded"):
        raise ValueError(f"{ctx} retain.prerelease: must be 'keep' or 'superseded'")

    return Retain(age=age, count=count, version=version, prerelease=prerelease)


def _parse_commit_match(raw: dict, ctx: str) -> list[str]:
    m = raw.get("match")
    patterns = [m] if isinstance(m, str) else list(m or [])
    if not patterns or not all(isinstance(p, str) for p in patterns):
        raise ValueError(f"{ctx}: 'match' must be a commit SHA/prefix string or non-empty "
                         f"list of strings")
    normalized = []
    for p in patterns:
        if not _SHA_RE.match(p):
            raise ValueError(f"{ctx}: 'match' entries must be 7-40 hex chars "
                             f"(a commit SHA or prefix), got {p!r}")
        normalized.append(p.lower())
    return normalized


_GIT_KEYS = {"host", "org", "repo", "ref_type"}
_INDEX_LEVELS = ("host", "org", "repo", "commit")
_MODES = ("snapshot", "delta")
# A suffix goes into a physical index name after a `^`, so it must be safe as an index-name
# segment: the same characters forbidden in a host id, plus the `^` we use as the suffix delimiter.
_FORBIDDEN_SUFFIX_CHARS = _FORBIDDEN_HOST_CHARS | {"^"}


_SUFFIX_VARIABLES = ", ".join("{" + lvl + "}" for lvl in (*_NUMERIC_LEVELS, "prerelease"))


def _parse_index(raw: dict, ctx: str) -> tuple[str, str | None]:
    """Validate a source's `index:` block and return (level, suffix).

    `level` defaults to "repo"; `suffix` defaults to None. An empty-string suffix is treated as
    omitted (per the spec). The suffix charset mirrors the host-id rules (lowercase, no whitespace,
    no index-name-forbidden chars) plus a ban on the `^` delimiter itself.

    A suffix may embed version variables ({major} ... {prerelease}), which are rendered per
    matched ref later. The charset rules therefore apply only to the literal text between
    variables -- whether the variables are usable at all depends on the source's `match`, which
    isn't compiled yet, so that cross-check lives in _validate_index_suffix_template."""
    if not isinstance(raw, dict):
        raise ValueError(f"{ctx} index: must be a mapping with 'level' and/or 'suffix'")
    unknown = set(raw) - {"level", "suffix"}
    if unknown:
        raise ValueError(f"{ctx} index: unknown keys {sorted(unknown)} (use 'level', 'suffix')")

    level = "repo"
    if raw.get("level") is not None:
        level = raw["level"]
        if level not in _INDEX_LEVELS:
            raise ValueError(f"{ctx} index.level: must be one of {list(_INDEX_LEVELS)} (got {level!r})")

    suffix: str | None = None
    if raw.get("suffix") is not None:
        s = raw["suffix"]
        if not isinstance(s, str):
            raise ValueError(f"{ctx} index.suffix: must be a string")
        if s != "":  # empty string == omitted
            _, unknown_vars = suffix_template_tokens(s)
            if unknown_vars:
                raise ValueError(f"{ctx} index.suffix: {s!r} uses unknown variable(s) "
                                 f"{list(unknown_vars)} (available: {_SUFFIX_VARIABLES})")
            literal = strip_suffix_tokens(s)
            if "{" in literal or "}" in literal:
                raise ValueError(f"{ctx} index.suffix: {s!r} has an unmatched '{{' or '}}'")
            bad = sorted({c for c in literal if c in _FORBIDDEN_SUFFIX_CHARS})
            if bad:
                raise ValueError(f"{ctx} index.suffix: {s!r} contains forbidden character(s) {bad}")
            if any(c.isupper() for c in literal):
                raise ValueError(f"{ctx} index.suffix: {s!r} must not contain uppercase characters")
            if any(c.isspace() for c in literal):
                raise ValueError(f"{ctx} index.suffix: {s!r} must not contain whitespace")
            suffix = s

    return level, suffix


def _validate_index_suffix_template(
    suffix: str | None, ctx: str, ref_type: str, mode: str,
    patterns: list[str], compiled: list[CompiledPattern],
) -> None:
    """Cross-check an index.suffix's version variables against the source's `match` patterns.

    A no-op for a plain literal suffix. Rendering a variable needs a version captured from the
    ref name, so a variable is only usable when the source captures it for *every* ref the
    selector can claim: `ref_type` other than commit, `mode: snapshot`, and every match pattern
    capturing every referenced level. Requiring every pattern (not just one) is what keeps
    Version.components aligned with Selector.levels at render time. Same shape of check
    retain.version.range's arity rule already applies to the same captured levels."""
    if not suffix:
        return
    tokens, _ = suffix_template_tokens(suffix)   # unknown variables already rejected upstream
    if not tokens:
        return
    listed = ", ".join("{" + t + "}" for t in tokens)
    if ref_type == "commit":
        raise ValueError(f"{ctx} index.suffix: {suffix!r} uses version variable(s) {listed}, but a "
                         f"commit source matches literal SHAs and captures no version")
    if mode != "snapshot":
        raise ValueError(f"{ctx} index.suffix: {suffix!r} uses version variable(s) {listed}, but "
                         f"'mode: {mode}' content is ref-addressed rather than per-version; "
                         f"version variables require 'mode: snapshot'")
    for pattern, cp in zip(patterns, compiled):
        for tok in tokens:
            if tok == "prerelease":
                if not cp.has_prerelease:
                    raise ValueError(f"{ctx} index.suffix: {{prerelease}} is not captured by match "
                                     f"pattern {pattern!r}; every match pattern must capture every "
                                     f"variable the suffix uses")
            elif tok not in cp.levels:
                captured = ", ".join("{" + lvl + "}" for lvl in cp.levels) or "no version levels"
                raise ValueError(f"{ctx} index.suffix: {{{tok}}} is not captured by match pattern "
                                 f"{pattern!r}, which captures {captured}; every match pattern must "
                                 f"capture every variable the suffix uses")


def _parse_git_scope(raw: dict, ctx: str) -> tuple[str, str, str, str]:
    """Validate a source's `git:` block and return (host, org, repo, ref_type). Every field is a
    single required concrete string; ref_type is one of branch/tag/commit; host is validated as a
    legal git.host value. No wildcards or lists (that keeps the config simple to parse and use)."""
    git = raw.get("git")
    if not isinstance(git, dict):
        raise ValueError(f"{ctx}: 'git' must be a mapping with host/org/repo/ref_type")
    unknown = set(git) - _GIT_KEYS
    if unknown:
        raise ValueError(f"{ctx} git: unknown keys {sorted(unknown)}")
    values: dict[str, str] = {}
    for key in ("host", "org", "repo", "ref_type"):
        val = git.get(key)
        if not isinstance(val, str) or not val:
            raise ValueError(f"{ctx} git: '{key}' must be a non-empty string")
        values[key] = val
    if values["ref_type"] not in ("branch", "tag", "commit"):
        raise ValueError(f"{ctx} git: 'ref_type' must be 'branch', 'tag', or 'commit'")
    try:
        validate_host_id(values["host"])
    except ValueError as e:
        raise ValueError(f"{ctx} git.host: {e}") from e
    return values["host"], values["org"], values["repo"], values["ref_type"]


def _parse_source(raw: dict, ctx: str) -> tuple[str, str, str, Selector]:
    """Parse one `sources[i]` entry into (host, org, repo, Selector). The ref_type comes from the
    `git` block; `match`/`since`/`retain`/`mode` are top-level siblings."""
    unknown = set(raw) - {"git", "match", "since", "retain", "schedule", "index", "mode"}
    if unknown:
        raise ValueError(f"{ctx}: unknown keys {sorted(unknown)}")
    host, org, repo, ref_type = _parse_git_scope(raw, ctx)

    # Parse mode early so it is available for the incremental constraints below.
    mode = "snapshot"
    if raw.get("mode") is not None:
        mode = raw["mode"]
        if mode not in _MODES:
            raise ValueError(f"{ctx} mode: must be one of {list(_MODES)} (got {mode!r})")

    # Parse the index: block for routing (level + suffix only; mode is now top-level).
    index_level, index_suffix = "repo", None
    if raw.get("index") is not None:
        index_level, index_suffix = _parse_index(raw["index"], ctx)

    if mode == "delta":
        if ref_type not in ("branch", "tag"):
            raise ValueError(f"{ctx} mode: 'delta' is only valid for "
                             f"git.ref_type: branch or tag (got ref_type {ref_type!r})")
        # A delta-mode ref maintains a single mutable ref-addressed view with no per-commit
        # history for retention to trim and no inclusion floor to apply -- both since and retain
        # are meaningless here (see specs/incremental-indexing.md).
        if raw.get("since") is not None:
            raise ValueError(f"{ctx}: 'mode: delta' cannot be combined with 'since'")
        if raw.get("retain") is not None:
            raise ValueError(f"{ctx}: 'mode: delta' cannot be combined with 'retain'")
        if index_level == "commit":
            # Delta-mode content is ref-addressed (no git.commit on content docs), so a
            # commit-level index name -- which requires a commit sha -- can never be built for it.
            raise ValueError(f"{ctx} mode: 'delta' cannot be combined with "
                             f"'index.level: commit'")

    if ref_type == "commit":
        # A pinned commit has no enumerable name to pattern-match against (see selection.py),
        # so `match` holds literal SHA/prefix strings instead of version.py DSL patterns, and
        # there are no version levels to drive version/count/prerelease retention.
        patterns = _parse_commit_match(raw, ctx)
        compiled: list[CompiledPattern] = []
        levels: tuple[str, ...] = ()
        has_versioned = False
    else:
        m = raw.get("match")
        patterns = [m] if isinstance(m, str) else list(m or [])
        if not patterns or not all(isinstance(p, str) for p in patterns):
            raise ValueError(f"{ctx}: 'match' must be a pattern string or non-empty list of strings")
        compiled = [compile_pattern(p) for p in patterns]

        # A version policy compares numeric levels, so every versioned pattern must agree on its
        # numeric level set (prerelease may be present on some and not others).
        level_sets = {cp.levels for cp in compiled if cp.levels}
        if len(level_sets) > 1:
            raise ValueError(f"{ctx}: match patterns disagree on version levels {sorted(level_sets)}")
        levels = next(iter(level_sets), ())
        has_versioned = bool(levels)

    # Now that `match` is compiled, the suffix's version variables (if any) can be checked
    # against the levels each pattern actually captures.
    _validate_index_suffix_template(index_suffix, ctx, ref_type, mode, patterns, compiled)

    if ref_type == "commit" and raw.get("since") is not None:
        # A commit selector already names the exact point to index -- there is nothing to
        # index "from".
        raise ValueError(f"{ctx}: commit sources do not support 'since'")
    since = _parse_since(raw["since"], ctx) if raw.get("since") is not None else None

    retain = None
    if raw.get("retain") is not None:
        retain = _parse_retain(raw["retain"], ctx, has_versioned, levels)
        if ref_type == "commit" and (retain.count is not None or retain.version is not None
                                      or retain.prerelease != "keep"):
            raise ValueError(f"{ctx}: commit sources support only 'age' retention")
        if retain.is_empty():
            retain = None

    schedule = None
    if raw.get("schedule") is not None:
        try:
            schedule = parse_schedule(raw["schedule"])
        except ValueError as e:
            raise ValueError(f"{ctx} schedule: {e}") from e

    selector = Selector(ref_type=ref_type, raw_patterns=patterns, compiled=compiled,
                        since=since, retain=retain, levels=levels, schedule=schedule,
                        mode=mode, index_level=index_level, index_suffix=index_suffix)
    return host, org, repo, selector


def _parse_schedule_rule(raw: dict, ctx: str) -> ScheduleRule:
    """Parse one `schedules[i]` entry into a ScheduleRule.

    A schedule rule has an optional `git` scope and a required `schedule` (cron or duration).

    Scope fields (all optional, inside a `git:` mapping):
    - `host` / `org` / `repo`: a string; glob wildcards (``*``, ``?``, ``[…]``) are allowed.
    - `ref_type`: one of ``branch``/``tag``/``commit`` or the bare wildcard ``*``.
      Partial globs (e.g. ``bra*``) are rejected — ref_type is an enum.
    - `ref`: a string; glob wildcards are allowed.  Matched against the selector's raw
      ``match`` pattern string(s) (pre-ls-remote semantics).
    """
    _SCHEDULE_GIT_KEYS = {"host", "org", "repo", "ref_type", "ref"}
    unknown = set(raw) - {"git", "schedule"}
    if unknown:
        raise ValueError(f"{ctx}: unknown keys {sorted(unknown)}")
    if "schedule" not in raw or raw["schedule"] is None:
        raise ValueError(f"{ctx}: 'schedule' is required")
    try:
        schedule = parse_schedule(raw["schedule"])
    except ValueError as e:
        raise ValueError(f"{ctx} schedule: {e}") from e

    host = org = repo = ref_type = ref = None
    git = raw.get("git")
    if git is not None:
        if not isinstance(git, dict):
            raise ValueError(f"{ctx} git: must be a mapping")
        unknown_git = set(git) - _SCHEDULE_GIT_KEYS
        if unknown_git:
            raise ValueError(f"{ctx} git: unknown keys {sorted(unknown_git)} "
                             f"(schedules git scope supports: "
                             f"{', '.join(sorted(_SCHEDULE_GIT_KEYS))})")
        if git.get("host") is not None:
            h = git["host"]
            if not isinstance(h, str) or not h:
                raise ValueError(f"{ctx} git.host: must be a non-empty string")
            host = h
        if git.get("org") is not None:
            o = git["org"]
            if not isinstance(o, str) or not o:
                raise ValueError(f"{ctx} git.org: must be a non-empty string")
            org = o
        if git.get("repo") is not None:
            r = git["repo"]
            if not isinstance(r, str) or not r:
                raise ValueError(f"{ctx} git.repo: must be a non-empty string")
            repo = r
        if git.get("ref_type") is not None:
            rt = git["ref_type"]
            if not isinstance(rt, str) or not rt:
                raise ValueError(f"{ctx} git.ref_type: must be a non-empty string")
            if rt not in ("branch", "tag", "commit", "*"):
                # Partial globs (e.g. "bra*") are not meaningful for an enum field.
                raise ValueError(
                    f"{ctx} git.ref_type: must be 'branch', 'tag', 'commit', or '*' "
                    f"(no partial globs on an enum field); got {rt!r}"
                )
            ref_type = rt
        if git.get("ref") is not None:
            rf = git["ref"]
            if not isinstance(rf, str) or not rf:
                raise ValueError(f"{ctx} git.ref: must be a non-empty string")
            ref = rf

    return ScheduleRule(host=host, org=org, repo=repo, schedule=schedule,
                        ref_type=ref_type, ref=ref)


def _deep_merge(dst: dict, src: dict) -> None:
    """Merge src into dst in place; raise on any key collision that isn't itself a pair of
    mappings to recurse into (used to reconcile dotted and nested forms of the same key)."""
    for k, v in src.items():
        if isinstance(dst.get(k), dict) and isinstance(v, dict):
            _deep_merge(dst[k], v)
        elif k in dst:
            raise ValueError(f"conflicting key {k!r} in config")
        else:
            dst[k] = v


def _expand_dotted_keys(obj):
    """Expand dict keys containing '.' into nested mappings, so `since.ref: x` becomes
    `since: {ref: x}`. Recurses into lists and nested dicts; deep-merges siblings that share
    a dotted prefix (e.g. retain.version.majors + retain.version.patches) or mix dotted and
    nested forms of the same subtree."""
    if isinstance(obj, list):
        return [_expand_dotted_keys(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out: dict = {}
    for key, val in obj.items():
        val = _expand_dotted_keys(val)
        parts = key.split(".") if isinstance(key, str) else [key]
        cursor = out
        for p in parts[:-1]:
            nxt = cursor.get(p)
            if nxt is None:
                nxt = cursor[p] = {}
            elif not isinstance(nxt, dict):
                raise ValueError(f"dotted key {key!r} conflicts with a non-mapping value for {p!r}")
            cursor = nxt
        leaf = parts[-1]
        if isinstance(cursor.get(leaf), dict) and isinstance(val, dict):
            _deep_merge(cursor[leaf], val)
        elif leaf in cursor:
            raise ValueError(f"duplicate key {key!r} in config")
        else:
            cursor[leaf] = val
    return out


@dataclass
class Config:
    """A parsed sourcerer.yml: the resolved git-host registry (built-in defaults merged with the
    file's `hosts:` overrides), the repos/selectors the `sources:` section selects, and the
    top-level schedule rules from `schedules:`."""
    hosts: dict[str, Host]
    repos: list[RepoConfig]
    schedules: list[ScheduleRule] = field(default_factory=list)


def load_config(config_path: str) -> Config:
    with open(config_path) as f:
        return parse_config(yaml.safe_load(f))


def parse_config(data) -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("config must be a YAML mapping with optional 'hosts:' and 'sources:'")
    data = _expand_dotted_keys(data)

    unknown = set(data) - {"hosts", "sources", "schedules"}
    if unknown:
        raise ValueError(f"config: unknown top-level keys {sorted(unknown)}")

    raw_hosts = data.get("hosts")
    if raw_hosts is not None and not isinstance(raw_hosts, list):
        raise ValueError("config: 'hosts' must be a list")
    hosts = resolve_hosts(raw_hosts)

    sources = data.get("sources")
    if sources is not None and not isinstance(sources, list):
        raise ValueError("config: 'sources' must be a list")

    # Group sources sharing (host, org, repo) into one RepoConfig (a list of selectors), so the
    # planner's cross-selector union retention holds across a repo's sources. Insertion order is
    # preserved so plan/report order is stable.
    grouped: dict[tuple[str, str, str], RepoConfig] = {}
    for i, entry in enumerate(sources or []):
        ctx = f"sources[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{ctx} must be a mapping")
        host, org, repo, selector = _parse_source(entry, ctx)
        # A source may name a host id not present in the (possibly overridden) registry only if it
        # is a built-in; resolve_hosts always includes every built-in, so an unknown id here means
        # a custom host was referenced without being defined in `hosts:`.
        if host not in hosts:
            raise ValueError(
                f"{ctx} git.host: unknown host {host!r}; define it under 'hosts:' in the config"
            )
        key = (host, org, repo)
        if key not in grouped:
            grouped[key] = RepoConfig(host=host, org=org, repo=repo, selectors=[])
        grouped[key].selectors.append(selector)

    raw_schedules = data.get("schedules")
    if raw_schedules is not None and not isinstance(raw_schedules, list):
        raise ValueError("config: 'schedules' must be a list")
    schedule_rules: list[ScheduleRule] = []
    for i, entry in enumerate(raw_schedules or []):
        ctx = f"schedules[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{ctx} must be a mapping")
        schedule_rules.append(_parse_schedule_rule(entry, ctx))

    return Config(hosts=hosts, repos=list(grouped.values()), schedules=schedule_rules)
