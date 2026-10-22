# Responsibility: Record what every test part measured, and say when it changes.
# Boundaries: comparison only. It meshes nothing; semi.regress drives it.
"""A BASELINE FOR THE WHOLE CORPUS, so a change in the mesher is visible.

WHY. Every number this package produces was measured once, by hand, and written into a docstring.
That is how it should be, and it is not a test: nothing notices when the mesher changes and a part
that used to come back with 100 passages comes back with 101. The unit tests cover the arithmetic
and the readers; nothing covers the meshes themselves, because meshing them takes minutes and a
unit test cannot.

So the corpus gets a recorded baseline and one command that re-measures it. What it compares is what
would actually matter if it moved:

    passages        a channel broken in half. The check `checkMesh` will not make.
    conformal       the interface face counts equal on both sides.
    port faces      a port staircased away, which makes the mesh unsolvable.
    cells           the mesh got bigger or smaller, which is usually a sizing change.
    volume error    the staircase got worse, which is usually a tagging change.

TOLERANCES ARE PER FIELD AND DELIBERATE. Passage counts, conformality and whether a port has any
faces at all are EXACT: those are topology, and a change in them is a defect every time. Cell
counts and volume errors are allowed to drift a little, because cfMesh is multithreaded and its
octree is not bit-reproducible across thread counts - a run on a busy machine and a run on an idle
one differ in the last few cells, and a baseline that fails on that is a baseline nobody keeps.

The drift allowance is not a judgement about whether the mesh is good. gap_sizing and the criteria
own that. This only answers "is it what it was".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: The file, beside the parts it describes.
BASELINE = "regression.json"

#: How far a cell count may drift before it is a change rather than cfMesh's thread scheduling.
#: Measured: repeat builds of the same part on this machine landed within a handful of cells, so
#: this is loose by a wide margin and still catches a sizing change, which moves counts by tens of
#: per cent.
CELLS_RTOL = 0.005

#: How far the fluid volume error may drift, in PERCENTAGE POINTS of the error itself. Same
#: reasoning: a tagging change moves this by whole points, thread scheduling does not.
VOLUME_ERROR_ATOL_PCT = 0.05


@dataclass(frozen=True)
class Record:
    """What one part measured at one resolution, as little of it as is worth comparing."""

    part: str
    across: int
    fluid_cells: int
    solid_cells: int
    passages: int
    cad_passages: int
    interface_faces: int
    conformal: bool
    inlet_faces: int
    outlet_faces: int
    ports_checked: bool
    volume_error_pct: float
    problems: int

    @classmethod
    def from_report(cls, mesh: dict) -> "Record":
        return cls(
            part=mesh["part"], across=int(mesh["cells_across_asked"]),
            fluid_cells=int(mesh.get("fluid_cells") or 0),
            solid_cells=int(mesh.get("solid_cells") or 0),
            passages=int(mesh.get("fluid_passages") or 0),
            cad_passages=int(mesh.get("cad_passages") or 0),
            interface_faces=int(mesh.get("interface_faces") or 0),
            conformal=bool(mesh.get("conformal")),
            inlet_faces=int(mesh.get("inlet_faces") or 0),
            outlet_faces=int(mesh.get("outlet_faces") or 0),
            ports_checked=bool(mesh.get("ports_checked")),
            volume_error_pct=float(mesh.get("fluid_volume_error_pct") or 0.0),
            problems=len(mesh.get("problems") or ()),
        )

    @property
    def key(self) -> str:
        return f"{self.part}@{self.across}"

    def as_dict(self) -> dict:
        return {
            "part": self.part, "across": self.across,
            "fluid_cells": self.fluid_cells, "solid_cells": self.solid_cells,
            "passages": self.passages, "cad_passages": self.cad_passages,
            "interface_faces": self.interface_faces, "conformal": self.conformal,
            "inlet_faces": self.inlet_faces, "outlet_faces": self.outlet_faces,
            "ports_checked": self.ports_checked,
            "volume_error_pct": round(self.volume_error_pct, 6),
            "problems": self.problems,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        return cls(**{k: d[k] for k in (
            "part", "across", "fluid_cells", "solid_cells", "passages", "cad_passages",
            "interface_faces", "conformal", "inlet_faces", "outlet_faces", "ports_checked",
            "volume_error_pct", "problems")})


@dataclass
class Comparison:
    """One part's result against what it used to be."""

    key: str
    #: Differences that are defects whatever their size: topology, conformality, a dead port.
    breaks: list[str] = field(default_factory=list)
    #: Differences inside the drift allowance, or outside it on a field that is allowed to drift.
    drifts: list[str] = field(default_factory=list)
    new: bool = False

    @property
    def ok(self) -> bool:
        return not self.breaks


def _pct_change(now: float, was: float) -> str:
    if was == 0:
        return f"{was} -> {now}"
    return f"{was:,} -> {now:,} ({(now - was) / was:+.2%})"


def compare(now: Record, was: Record | None) -> Comparison:
    """What moved, split into what is always a defect and what is allowed to drift."""
    out = Comparison(key=now.key)
    if was is None:
        out.new = True
        return out

    # EXACT, every one of these. Topology does not drift.
    if now.passages != was.passages:
        out.breaks.append(
            f"the coolant came back in {now.passages} piece(s) and used to come back in "
            f"{was.passages}. The CAD says {now.cad_passages}. A passage broken in half or two "
            "merged into one is a mesh no solver will take, and checkMesh calls it OK.")
    if now.conformal != was.conformal:
        out.breaks.append(
            f"conformal was {was.conformal} and is now {now.conformal}. The two regions no longer "
            "see the same surface, so the heat coupling is unsound.")
    if now.interface_faces != was.interface_faces and now.passages == was.passages:
        out.drifts.append(f"interface faces {_pct_change(now.interface_faces, was.interface_faces)}")
    for role, a, b in (("inlet", now.inlet_faces, was.inlet_faces),
                       ("outlet", now.outlet_faces, was.outlet_faces)):
        if (a == 0) != (b == 0):
            out.breaks.append(
                f"the {role} had {b} face(s) and now has {a}. A port that loses every face is a "
                "coolant region with nothing fixing its pressure, which dies in the "
                "preconditioner.")
        elif a != b:
            out.drifts.append(f"{role} faces {_pct_change(a, b)}")
    if now.ports_checked != was.ports_checked:
        out.breaks.append(
            f"ports were {'checked' if was.ports_checked else 'not checked'} and are now "
            f"{'checked' if now.ports_checked else 'not checked'}, so this is not the same run")

    # ALLOWED TO DRIFT, because cfMesh's octree is not bit-reproducible across thread counts.
    for label, a, b in (("fluid cells", now.fluid_cells, was.fluid_cells),
                        ("solid cells", now.solid_cells, was.solid_cells)):
        if b and abs(a - b) > CELLS_RTOL * b:
            out.breaks.append(
                f"{label} {_pct_change(a, b)}, past the {CELLS_RTOL:.1%} the octree's own "
                "non-determinism accounts for. Something changed the sizing.")
        elif a != b:
            out.drifts.append(f"{label} {_pct_change(a, b)}")

    # Compared at the precision the baseline is STORED at, not at full float precision. The
    # baseline rounds to six places, so an unchanged part otherwise came back with a drift of
    # "-27.542% -> -27.542%": a difference in the seventh decimal, reported as news.
    delta = round(now.volume_error_pct, 6) - round(was.volume_error_pct, 6)
    if abs(delta) > VOLUME_ERROR_ATOL_PCT:
        out.breaks.append(
            f"the fluid volume error moved from {was.volume_error_pct:+.3f}% to "
            f"{now.volume_error_pct:+.3f}%, a shift of {delta:+.3f} points. Different cells are "
            "being tagged as the coolant.")
    elif delta:
        out.drifts.append(f"volume error {was.volume_error_pct:+.3f}% -> "
                          f"{now.volume_error_pct:+.3f}%")

    if now.problems != was.problems:
        out.drifts.append(f"problems reported {was.problems} -> {now.problems}")
    return out


def load(cad_dir) -> dict[str, Record]:
    path = Path(cad_dir) / BASELINE
    if not path.is_file():
        return {}
    return {r["part"] + "@" + str(r["across"]): Record.from_dict(r)
            for r in json.loads(path.read_text())}


def save(cad_dir, records: dict[str, Record]) -> Path:
    path = Path(cad_dir) / BASELINE
    path.write_text(json.dumps([records[k].as_dict() for k in sorted(records)], indent=2)
                    + chr(10))
    return path


__all__ = ["BASELINE", "CELLS_RTOL", "VOLUME_ERROR_ATOL_PCT", "Record", "Comparison",
           "compare", "load", "save"]
