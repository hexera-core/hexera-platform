# Responsibility: The VMTK staging's planned work (docs/RULES.md TIME-18): what the staged wall will cost to read and
#                 to remesh, counted before that work runs, and the stop when it cannot fit the run.
# Boundaries: pure arithmetic on counts (triangles, points, rays); it reads no clock and runs nothing. The staging
#             (engines/vmtk/lumen_staging) asks it, before its bound and before its radius field.
# Collaborates with: engines/vmtk/rims (the bound's own planned count, rims.TooManyTriangles), engines/radius_field
#             (its ray caps), agents/builder/attempt._stage_declared (records the stop as the staging's refusal,
#             and the run moves on to the next engine: pipeline/engine_fallback, NOT_BUILT).
from __future__ import annotations

from dataclasses import asdict, dataclass

#: THE STAGING'S WORK IS PLANNED, AND A PLAN THAT CANNOT FIT THE RUN MOVES IT ON (docs/RULES.md).
STAGING_WORK_RULE = "TIME-18"

#: What one staged wall triangle costs VMTK at its SLOW rate, in seconds: its share of the radius field the staging
#: reads on it, and of vmtk's own surface work (the remesh and the centerlines, which run on the staged wall before
#: any cell is filled). Read off the board (huntbig2..5, 2026-11-22, mesher seconds past the fill at VMTK's slow
#: 4,000 cells a second): 2.3 ms (abc 00046345, 1.0 M staged), 1.8 ms (00178394, 3.0 M), 6.4 ms (stabilizing rod,
#: 1.0 M) - the slow one is taken.
SECONDS_PER_STAGED_TRIANGLE = 0.0065
#: The most planned seconds the staging and vmtk's surface work on what it stages may take in a run: one hour, a
#: quarter of the mesh job's 4-hour wall, so a run that cannot fit still has the rest for the next engine to deliver
#: a valid mesh. Before (board, 22 Nov): abc 00168427, 00160152, 00184527 and 00046344 staged 3.0-3.6 M triangles,
#: spent 81-134 minutes staging and met the 250-minute wall with no mesh.
STAGING_BUDGET_S = 3600.0
#: ... so the staged wall is at most this many triangles (553,846).
MAX_STAGED_TRIANGLES = int(STAGING_BUDGET_S / SECONDS_PER_STAGED_TRIANGLE)


@dataclass(frozen=True)
class StagingWork:
    """The staging's planned work, as recorded beside the staged lumen (vmtk_staging.json `staging_work`)."""
    cad_triangles: int
    staged_triangles: int
    planned_s: float
    budget_s: float
    max_triangles: int
    rule: str = STAGING_WORK_RULE

    def as_dict(self) -> dict:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in asdict(self).items()}


class StagingOverWork(ValueError):
    """The staged wall would be past the planned work one run allows VMTK (TIME-18): said in plain words, as the
    staging's refusal, before the work is done."""


def planned(cad_triangles: int, staged_triangles: int) -> StagingWork:
    return StagingWork(cad_triangles=int(cad_triangles), staged_triangles=int(staged_triangles),
                       planned_s=float(staged_triangles) * SECONDS_PER_STAGED_TRIANGLE,
                       budget_s=STAGING_BUDGET_S, max_triangles=MAX_STAGED_TRIANGLES)


def refusal_words(cad_triangles: int, staged_at_least: int) -> str:
    """Why VMTK does not take this wall, in the words the run's record and the user read."""
    hours = staged_at_least * SECONDS_PER_STAGED_TRIANGLE / 3600.0
    return (f"VMTK would have to prepare at least {staged_at_least:,} wall triangles from the {cad_triangles:,} "
            f"of your surface, about {hours:.1f} hours of work, past the {STAGING_BUDGET_S / 3600.0:.0f} hour a run "
            "gives it, so it stopped before starting and another engine can take the part")


def check(cad_triangles: int, staged_triangles: int) -> StagingWork:
    """The planned work of a staged wall, or StagingOverWork when it is past MAX_STAGED_TRIANGLES."""
    work = planned(cad_triangles, staged_triangles)
    if staged_triangles > MAX_STAGED_TRIANGLES:
        raise StagingOverWork(refusal_words(cad_triangles, staged_triangles))
    return work


__all__ = ["MAX_STAGED_TRIANGLES", "SECONDS_PER_STAGED_TRIANGLE", "STAGING_BUDGET_S", "STAGING_WORK_RULE",
           "StagingOverWork", "StagingWork", "check", "planned", "refusal_words"]
