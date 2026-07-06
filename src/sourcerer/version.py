"""Version-aware ref pattern matching + retention primitives.

A pattern is a glob with embedded tokens: numeric {major} {minor} {patch} {build} plus a
non-numeric {prerelease} (optional inner whitespace, e.g. { major }). Numeric tokens become
\\d+ capture groups; {prerelease} captures a semver-style identifier (rc.1, beta, lts);
everything else keeps glob semantics (* ? [seq] [!seq]). Patterns are structural and
future-proof: v{major}.{minor}.{patch} matches v1.2.3 today and v10.4.0 whenever it ships.
"""

from __future__ import annotations

# Standard packages
import re
from dataclasses import dataclass

LEVELS = ("major", "minor", "patch", "build")           # numeric, ordered outermost-first
_TOKEN_RE = re.compile(r"\{\s*(major|minor|patch|build|prerelease)\s*\}")
_PRERELEASE_RE = r"[0-9A-Za-z][0-9A-Za-z.\-]*"           # rc.1, beta, lts, 20250101


def _glob_fragment(seg: str) -> str:
    """Translate a token-free glob segment to a (non-anchored) regex fragment: * ? [seq] [!seq]."""
    out, i, n = [], 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            j = i + 1
            if j < n and seg[j] in "!^":
                j += 1
            if j < n and seg[j] == "]":
                j += 1
            while j < n and seg[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
                continue
            body = seg[i + 1 : j]
            if body[0] in "!^":
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = j + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class CompiledPattern:
    regex: re.Pattern
    levels: tuple[str, ...]      # numeric levels present, outermost-first
    has_prerelease: bool

    def is_versioned(self) -> bool:
        return bool(self.levels)


def compile_pattern(pattern: str) -> CompiledPattern:
    parts: list[str] = []
    levels: list[str] = []
    has_pre = False
    pos = 0
    for m in _TOKEN_RE.finditer(pattern):
        parts.append(_glob_fragment(pattern[pos : m.start()]))
        tok = m.group(1)
        if tok == "prerelease":
            if has_pre:
                raise ValueError(f"duplicate {{prerelease}} in pattern {pattern!r}")
            has_pre = True
            parts.append(f"(?P<prerelease>{_PRERELEASE_RE})")
        else:
            if tok in levels:
                raise ValueError(f"duplicate {{{tok}}} in pattern {pattern!r}")
            levels.append(tok)
            parts.append(f"(?P<{tok}>\\d+)")
        pos = m.end()
    parts.append(_glob_fragment(pattern[pos:]))

    if levels and tuple(levels) != LEVELS[: len(levels)]:
        raise ValueError(f"pattern {pattern!r} uses levels {levels}; must be a prefix of {LEVELS}")

    return CompiledPattern(re.compile("^" + "".join(parts) + "$"), tuple(levels), has_pre)


@dataclass(frozen=True)
class Version:
    ref: str
    components: tuple[int, ...]   # numeric values for `levels`, outermost-first
    prerelease: str              # "" for a final release; e.g. "rc.1" otherwise

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease != ""

    def prefix(self, k: int) -> tuple[int, ...]:
        return self.components[:k]


def match_version(cp: CompiledPattern, ref: str) -> Version | None:
    """Version if `ref` matches, else None. Non-versioned patterns still return a Version
    (empty components) so they act as pure glob filters."""
    m = cp.regex.match(ref)
    if m is None:
        return None
    comps = tuple(int(m.group(lvl)) for lvl in cp.levels)
    pre = (m.groupdict().get("prerelease") or "") if cp.has_prerelease else ""
    return Version(ref=ref, components=comps, prerelease=pre)


def parse_bound(text: str, levels: tuple[str, ...]) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", text)]
    nums = (nums + [0] * len(levels))[: len(levels)]
    return tuple(nums)


# --- retention primitives (pure; feed Version objects or (key, value) pairs) ------------
from datetime import datetime, timedelta, timezone  # noqa: E402
from collections import defaultdict  # noqa: E402


def version_keep(versions, counts: dict[str, int], levels: tuple[str, ...]) -> set:
    """Value-relative retention. `counts` maps a level -> N >= 1: keep the newest N *values*
    at that level *within its parent group* (threshold = latest - (N - 1)).
      majors:2  -> keep the latest major and one behind (n-1 EOL), by VALUE not by count
      patches:1 -> newest patch per (major, minor)
    A level not in `counts` imposes no constraint. Distinct from count-based keeps: with
    majors {2, 9} indexed, majors:2 keeps {9} (threshold 8); a count of 2 would keep {9, 2}."""
    kept = set(versions)
    for i, level in enumerate(levels):
        n = counts.get(level)
        if n is None:
            continue
        groups: dict = defaultdict(list)
        for v in kept:
            groups[v.prefix(i)].append(v)
        survivors = set()
        for _, vs in groups.items():
            threshold = max(v.components[i] for v in vs) - (n - 1)
            survivors |= {v for v in vs if v.components[i] >= threshold}
        kept = survivors
    return kept


def drop_superseded_prereleases(versions) -> set:
    """Remove any prerelease whose exact numeric tuple also exists as a final release. Needs
    only tuple equality + presence -- no prerelease ordering (rc.2 vs rc.1)."""
    finals = {v.components for v in versions if not v.is_prerelease}
    return {v for v in versions if not (v.is_prerelease and v.components in finals)}


def recent_keep(refs_with_ts, n: int) -> set:
    """Keep the n most-recent refs. `refs_with_ts`: iterable of (key, timestamp)."""
    ordered = sorted(refs_with_ts, key=lambda p: p[1], reverse=True)
    return {key for key, _ in ordered[:n]}


def age_keep(refs_with_ts, max_age: timedelta, now: datetime | None = None) -> set:
    """Keep refs whose timestamp is within max_age of now."""
    now = now or datetime.now(timezone.utc)
    return {key for key, ts in refs_with_ts if now - ts <= max_age}