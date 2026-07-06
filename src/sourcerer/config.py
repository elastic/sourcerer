"""repos.yml model + loader, shared by `index` and `prune`.

Per repo entry:

    - org: elastic
      repo: elasticsearch
      refs:
      - type: tag | branch
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

Duration units: s, h, d, w, m (=30d month), y (=365d year).
"""

from __future__ import annotations

# Standard packages
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Third-party packages
import yaml

# App packages
from .version import CompiledPattern, Version, compile_pattern, match_version, parse_bound

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|h|d|w|m|y)\s*$")
_DURATION_UNITS = {"s": 1, "h": 3600, "d": 86400, "w": 604800, "m": 2592000, "y": 31536000}
_NUMERIC_LEVELS = ("major", "minor", "patch", "build")
_PLURAL_TO_LEVEL = {"majors": "major", "minors": "minor", "patches": "patch", "builds": "build"}


def parse_duration(text) -> timedelta:
    m = _DURATION_RE.match(str(text))
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. 30s, 12h, 7d, 2w, 3m, 1y)")
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
    levels: tuple[str, ...] = ()   # numeric levels shared by the versioned match patterns

    def matches(self, ref_type: str, ref: str) -> Version | None:
        if self.ref_type != ref_type:
            return None
        for cp in self.compiled:
            v = match_version(cp, ref)
            if v is not None:
                return v
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


@dataclass
class RepoConfig:
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
        vunknown = set(vraw) - set(_PLURAL_TO_LEVEL)
        if vunknown:
            raise ValueError(f"{ctx} retain.version: unknown levels {sorted(vunknown)} "
                             f"(use {sorted(_PLURAL_TO_LEVEL)}; prerelease is a sibling of version)")
        counts = {_PLURAL_TO_LEVEL[p]: _parse_count_level(vraw.get(p), p, ctx) for p in _PLURAL_TO_LEVEL}
        counts = {lvl: n for lvl, n in counts.items() if n is not None}
        version = VersionPolicy(counts=counts)

    prerelease = raw.get("prerelease", "keep")
    if prerelease not in ("keep", "superseded"):
        raise ValueError(f"{ctx} retain.prerelease: must be 'keep' or 'superseded'")

    return Retain(age=age, count=count, version=version, prerelease=prerelease)


def _parse_selector(raw: dict, ctx: str) -> Selector:
    if raw.get("type") not in ("branch", "tag"):
        raise ValueError(f"{ctx}: 'type' must be 'branch' or 'tag'")
    unknown = set(raw) - {"type", "match", "since", "retain"}
    if unknown:
        raise ValueError(f"{ctx}: unknown keys {sorted(unknown)}")
    ref_type = raw["type"]

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

    since = _parse_since(raw["since"], ctx) if raw.get("since") is not None else None

    retain = None
    if raw.get("retain") is not None:
        retain = _parse_retain(raw["retain"], ctx, has_versioned, levels)
        if retain.is_empty():
            retain = None

    return Selector(ref_type=ref_type, raw_patterns=patterns, compiled=compiled,
                    since=since, retain=retain, levels=levels)


def load_config(config_path: str) -> list[RepoConfig]:
    with open(config_path) as f:
        return parse_config(yaml.safe_load(f))


def parse_config(data) -> list[RepoConfig]:
    if not isinstance(data, list):
        raise ValueError("config must be a YAML list of repo entries")
    out: list[RepoConfig] = []
    for i, entry in enumerate(data):
        ctx = f"config entry {i}"
        if not isinstance(entry, dict):
            raise ValueError(f"{ctx} must be a mapping")
        if not entry.get("org") or not entry.get("repo"):
            raise ValueError(f"{ctx} must set both 'org' and 'repo'")
        refs = entry.get("refs")
        if refs is not None and not isinstance(refs, list):
            raise ValueError(f"{ctx}: 'refs' must be a list")
        selectors = [_parse_selector(s, f"{ctx} refs[{j}]") for j, s in enumerate(refs or [])]
        out.append(RepoConfig(org=entry["org"], repo=entry["repo"], selectors=selectors))
    return out