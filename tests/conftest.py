"""Shared test builders. Kept minimal -- this suite tests pure logic, so no ES/git fixtures
are needed; these just remove the per-file duplication of small dataclass builders."""

# Standard packages
from datetime import datetime, timezone

# App packages
from sourcerer.planner import Marker
from sourcerer.version import Version


def make_marker(
    id_: str = "m1",
    ref: str = "main",
    ref_type: str = "branch",
    commit: str = "aaa",
    commit_date: datetime | None = None,
    indexed_at: datetime | None = None,
) -> Marker:
    return Marker(
        id=id_, ref=ref, ref_type=ref_type, commit=commit,
        commit_date=commit_date, indexed_at=indexed_at,
    )


def make_version(ref: str, components: tuple[int, ...] = (), prerelease: str = "") -> Version:
    return Version(ref=ref, components=components, prerelease=prerelease)


def dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)
