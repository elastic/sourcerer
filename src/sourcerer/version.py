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


# --- index.suffix templates (see specs/sourcerer-yml.md) --------------------------------
# A source's index.suffix may embed the same version tokens a match pattern uses, so one
# source can fan out to per-version sibling indices (index.suffix: "{major}.{minor}.x" ->
# ^9.5.x, ^9.6.x, ...). Rendering happens once per matched ref, at Unit construction, so
# every downstream consumer (refs markers, index names, migration checks) sees a concrete
# value indistinguishable from a hand-written literal suffix.
_ANY_TOKEN_RE = re.compile(r"\{[^{}]*\}")


def suffix_template_tokens(template: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a suffix template's `{...}` spans into (known version tokens, unknown spans).

    Known tokens are returned as bare names ("major", "prerelease") in first-appearance order
    with duplicates collapsed; unknown spans are returned verbatim (e.g. "{foo}") so a caller
    can name them in an error message. A template with no `{...}` spans yields two empty
    tuples, which is how callers detect a plain literal suffix."""
    known: list[str] = []
    unknown: list[str] = []
    for span in _ANY_TOKEN_RE.findall(template):
        m = _TOKEN_RE.fullmatch(span)
        if m is None:
            if span not in unknown:
                unknown.append(span)
        elif m.group(1) not in known:
            known.append(m.group(1))
    return tuple(known), tuple(unknown)


def strip_suffix_tokens(template: str) -> str:
    """The template's literal text with every `{...}` span removed, so a caller can apply
    index-name charset rules to the parts a user actually wrote. A leftover brace in the
    result means the template has an unmatched `{` or `}`."""
    return _ANY_TOKEN_RE.sub("", template)


def render_suffix(template: str, levels: tuple[str, ...], v: Version) -> str:
    """Substitute a suffix template's version tokens from `v` and lowercase the result.

    `levels` positions `v.components` (both are outermost-first), so a numeric token resolves
    via its index in `levels`. Lowercasing matters: {prerelease} can capture uppercase (see
    _PRERELEASE_RE) while index names cannot, and the rendered string is stored on the refs
    marker AND used to build the index name -- normalizing here keeps those two byte-identical
    so a re-run never reads back a mismatched routing and migrates needlessly.

    Assumes config parsing has already verified every token is captured by every match pattern
    (see config._validate_index_suffix_template); an uncaptured token raises ValueError."""
    def sub(m: re.Match) -> str:
        tok = m.group(1)
        if tok == "prerelease":
            if not v.prerelease:
                raise ValueError(f"suffix template {template!r}: ref {v.ref!r} has no prerelease")
            return v.prerelease
        if tok not in levels:
            raise ValueError(f"suffix template {template!r}: {{{tok}}} is not a captured level {levels}")
        return str(v.components[levels.index(tok)])

    return _TOKEN_RE.sub(sub, template).lower()


# --- retention primitives (pure; feed Version objects or (key, value) pairs) ------------
from datetime import datetime, timedelta, timezone  # noqa: E402
from collections import defaultdict  # noqa: E402


_OP_TOKEN_RE = re.compile(r"^(>=|<=|>|<|=)")


def _parse_range_str(range_str: str, levels: tuple[str, ...]) -> list[tuple[str, tuple[int, ...]]]:
    """Parse a range string (e.g. ">=6.0.0 <7.0.0") into a list of (op, components) pairs.
    Validates grammar and arity against `levels`; raises ValueError on any violation."""
    if "||" in range_str:
        raise ValueError(f"range {range_str!r}: '||' is not supported")
    # Reject hyphen ranges ("6.0.0 - 7.0.0") -- digit, space-dash-space, digit
    if re.search(r"\d\s+-\s+\d", range_str):
        raise ValueError(
            f"range {range_str!r}: hyphen ranges (e.g. '6.0.0 - 7.0.0') are not supported"
        )
    for bad in ("~", "^", "x", "X", "*"):
        if bad in range_str:
            raise ValueError(
                f"range {range_str!r}: {bad!r} is not supported; "
                "use >=, <=, >, <, = and bare numbers only"
            )

    n = len(levels)

    # Split by whitespace; each token must be "OP NUMBER" or bare "NUMBER" (exact match)
    tokens = range_str.split()
    if not tokens:
        raise ValueError(f"range {range_str!r}: empty range string")

    result: list[tuple[str, tuple[int, ...]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Check if token starts with a comparator or is a bare number
        m = _OP_TOKEN_RE.match(tok)
        if m:
            op = m.group(1)
            num_part = tok[m.end():]
            if not num_part:
                # Operator and number may be separate tokens (e.g. ">= 6.0.0")
                i += 1
                if i >= len(tokens):
                    raise ValueError(f"range {range_str!r}: operator {op!r} has no operand")
                num_part = tokens[i]
        elif re.match(r"^\d", tok):
            op = "="
            num_part = tok
        else:
            raise ValueError(
                f"range {range_str!r}: unexpected token {tok!r}; "
                f"expected a comparator (>=, <=, >, <, =) or a bare number"
            )

        if not re.match(r"^\d+(\.\d+)*$", num_part):
            raise ValueError(
                f"range {range_str!r}: {num_part!r} is not a valid version number "
                f"(no 'v' prefix; use bare numbers like 6.0.0)"
            )
        parts = [int(x) for x in num_part.split(".")]
        if len(parts) != n:
            raise ValueError(
                f"range {range_str!r}: literal {num_part!r} has {len(parts)} component(s) "
                f"but the match pattern captures {n} ({', '.join('{' + lvl + '}' for lvl in levels)}); "
                f"arities must match exactly"
            )
        result.append((op, tuple(parts)))
        i += 1

    return result


def _apply_range_op(components: tuple[int, ...], op: str, bound: tuple[int, ...]) -> bool:
    """True if `components op bound` holds, compared tuple-lexicographically."""
    if op == ">=":
        return components >= bound
    if op == "<=":
        return components <= bound
    if op == ">":
        return components > bound
    if op == "<":
        return components < bound
    if op == "=":
        return components == bound
    raise ValueError(f"unknown op {op!r}")


def version_range_keep(versions, range_str: str, levels: tuple[str, ...]) -> set:
    """Keep only versions whose numeric components satisfy all clauses in `range_str`.
    Prerelease field is ignored (retain.prerelease is the sole prerelease control).
    Versions without numeric components (empty levels) pass through unchanged."""
    clauses = _parse_range_str(range_str, levels)
    kept = set()
    for v in versions:
        if not v.components:
            kept.add(v)
            continue
        if all(_apply_range_op(v.components, op, bound) for op, bound in clauses):
            kept.add(v)
    return kept


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


def prerelease_count_keep(versions_with_dates, n: int) -> set:
    """Keep the newest N prereleases per final-version group (by commit date). Non-prerelease
    versions are never dropped by this criterion. Groups prereleases by their full numeric
    `components` tuple (the (major,minor,patch[,build]) tuple of the release they belong to);
    within each group keeps the n most-recently committed. `versions_with_dates` is an iterable
    of (Version, commit_date | None); None dates sort as oldest (same convention as _EPOCH)."""
    _epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    groups: dict = defaultdict(list)
    kept = set()
    for v, ts in versions_with_dates:
        if v.is_prerelease:
            groups[v.components].append((v, ts))
        else:
            kept.add(v)
    for _, pairs in groups.items():
        ordered = sorted(pairs, key=lambda p: p[1] if p[1] is not None else _epoch, reverse=True)
        kept.update(v for v, _ in ordered[:n])
    return kept


def recent_keep(refs_with_ts, n: int) -> set:
    """Keep the n most-recent refs. `refs_with_ts`: iterable of (key, timestamp)."""
    ordered = sorted(refs_with_ts, key=lambda p: p[1], reverse=True)
    return {key for key, _ in ordered[:n]}


def age_keep(refs_with_ts, max_age: timedelta, now: datetime | None = None) -> set:
    """Keep refs whose timestamp is within max_age of now."""
    now = now or datetime.now(timezone.utc)
    return {key for key, ts in refs_with_ts if now - ts <= max_age}