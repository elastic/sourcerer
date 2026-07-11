# sourcerer/commands/prune/report.py
# The prune report: what would be deleted and why. Row construction (from retention decisions
# and the orphan-sweep plan) plus the flat, greppable printer. Shared by --dry-run and a real
# run alike -- only whether execute.py is then called differs.

# Standard packages
from dataclasses import dataclass

# Third-party packages
from rich.console import Console
from rich.text import Text

# App packages
from ...planner import OrphanPlan

_console = Console()


@dataclass(frozen=True)
class _Row:
    """One line of the prune report: what would be deleted and why, so a line stands on its
    own when grepped, without needing a header line above it for context.

    WHY is one of a fixed set of tags:
      - "policy:<criteria>" -- a retain-policy deletion, where <criteria> is a comma-joined
        subset of "age", "count", "version", "prerelease" (that fixed order) naming exactly
        which criteria excluded this marker -- e.g. "policy:count" or "policy:age,count".
      - "orphan:index"      -- a whole physical index with no matching refs entry (Class A).
      - "orphan:content"    -- a commit's content docs with no surviving marker (Class B).
      - "orphan:marker"     -- a commit's marker doc with no content at all (Class C).
    (See planner.OrphanPlan for the three orphan classes.)

    WHAT names the actual object being deleted, which differs by tag rather than forcing one
    shape:
      - policy:*                       -> "org/repo@ref (commit)" -- a marker, addressed by
        its ref since two refs can point at the same pruned commit and be indistinguishable
        without it.
      - orphan:content, orphan:marker   -> "org/repo@commit" -- no ref exists for either case.
      - orphan:index                    -> the index's own name (e.g.
        "sourcerer-v1-files~org~repo") -- not commit- or even repo-addressable, since an
        org-level orphan index spans every repo under that org."""

    what: str
    why: str


def _retention_rows(cfg, decisions: list) -> list[_Row]:
    """Rows for markers a repo's retain policy would prune (see _Row for the WHAT/WHY
    shapes). Only 'delete' decisions are reported -- kept and unmanaged markers aren't being
    deleted, so there's nothing actionable to grep for in them."""
    return [_Row(f"{cfg.org}/{cfg.repo}@{d.marker.ref} ({d.marker.commit})",
                 "policy:" + ",".join(d.criteria))
            for d in decisions if d.action == "delete"]


def _orphan_rows(plan: OrphanPlan) -> list[_Row]:
    """Rows for the three orphan classes (see _Row for the WHAT/WHY shapes, and
    planner.OrphanPlan for the classes themselves)."""
    rows = []
    for name in plan.orphan_index_names:
        rows.append(_Row(name, "orphan:index"))
    for (org, repo), commits in sorted(plan.orphan_content.items()):
        for commit in sorted(commits):
            rows.append(_Row(f"{org}/{repo}@{commit}", "orphan:content"))
    for (org, repo), commits in sorted(plan.orphan_marker_commits.items()):
        for commit in sorted(commits):
            rows.append(_Row(f"{org}/{repo}@{commit}", "orphan:marker"))
    return rows


def _print(rows: list[_Row]) -> None:
    """Flat, greppable report: one line per deleted object, sorted lexicographically by WHY
    then WHAT (see _Row for what WHAT/WHY can contain) -- so every tag's rows sit together,
    grouped as if pre-filtered by grep. Nothing is printed for markers being kept; there's
    nothing to act on in something that isn't changing.

    No header row -- WHY leads since it's the shorter, more scannable field. Hand-padded and
    printed with soft_wrap (rather than a rich.Table, which wraps/crops cells to the terminal
    width) so every row stays exactly one physical line."""
    if not rows:
        return
    rows = sorted(rows, key=lambda r: (r.why, r.what))
    w_why = max(len(r.why) for r in rows)

    for r in rows:
        style = "yellow" if r.why.startswith("policy:") else "red"
        line = Text()
        line.append(f"{r.why:<{w_why}} ", style=style)
        line.append(r.what)
        _console.print(line, soft_wrap=True)
