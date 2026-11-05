# Responsibility: Judge one FEA mesh against each version-1 solver's own published element checks - CalculiX, Abaqus, Nastran, Ansys MAPDL - so the result screen and the download say pass, warnings or fail per solver, with the file each one reads and whether that solver has really run it.
# Boundaries: pure over the figures engines/fea_quality.py measured (quality.json). It decides no verdict (the run's verdict is VER-1..4 over the strictest limits); it reports each solver's own view. Our readings of each check are stated beside them: no solver was run here except CalculiX.
# Collaborates with: engines/fea_quality.py (the figures), engines/fea_writers.SOLVERS (files, element types), engines/fea_results.py (shows the lines), Documents/HEXERA_FEA_RESEARCH.md section 3 (the limits and their sources).
"""PASS / WARNINGS / FAIL, PER SOLVER.

Each row: the figure it reads, the solver's warning limit and its error limit (None when the solver
has only one), and the source. A solver passes when every row is within its warning limit, warns
when a row is past a warning but within every error limit, and fails past an error limit. A row
whose figure was not measured is listed as such and decides nothing.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meshpipeline.engines.fea_writers import NOT_TESTED, SOLVERS

_ANSYS = "Ansys MAPDL element shape checks (Theory Reference 12.1)"
_ABAQUS = "Abaqus/CAE Verify Mesh defaults for tetrahedra"
_NASTRAN = "MSC Nastran GEOMCHECK defaults for CTETRA"

#: solver -> rows (figure key, what it checks, op, warning limit, error limit, source / our reading)
CHECKS: dict[str, tuple[tuple[str, str, str, Any, Any, str], ...]] = {
    "calculix": (
        ("fea_inverted", "Elements with a Jacobian not above zero", "<=", None, 0,
         "CalculiX stops on a nonpositive Jacobian; it has no graded shape checks"),
    ),
    "abaqus": (
        ("fea_inverted", "Elements with a Jacobian not above zero", "<=", None, 0, "an inverted element never runs"),
        ("fea_max_edge_ratio", "Longest edge over shortest edge", "<=", 10.0, None,
         f"{_ABAQUS}: aspect ratio above 10 (read here as longest over shortest edge)"),
        ("fea_min_face_angle_deg", "Smallest angle in a face (degrees)", ">=", 5.0, None, f"{_ABAQUS}: under 5 degrees"),
        ("fea_max_face_angle_deg", "Largest angle in a face (degrees)", "<=", 170.0, None, f"{_ABAQUS}: over 170 degrees"),
    ),
    "nastran": (
        ("fea_inverted", "Elements with a Jacobian not above zero", "<=", None, 0, f"{_NASTRAN}: TET_DETJ 0"),
        ("fea_max_aspect_raw", "Longest edge over shortest height", "<=", 100.0, None,
         f"{_NASTRAN}: TET_AR 100 (read here as longest edge over shortest height)"),
        ("fea_max_midside_offset", "Midside node off its edge's middle (share of the edge)", "<=", 0.25, None,
         f"{_NASTRAN}: TET_EPLR 0.5 (the node between the edge's quarter points)"),
        ("fea_max_midside_bend_deg", "Edge bend at a midside node (degrees)", "<=", 30.0, None,
         f"{_NASTRAN}: TET_EPIA 150 (an included angle of at least 150 degrees)"),
    ),
    "ansys": (
        ("fea_inverted", "Elements with a Jacobian not above zero", "<=", None, 0, f"{_ANSYS}: a sign flip always fails"),
        ("fea_max_jacobian_ratio", "Jacobian ratio across the nodes", "<=", 30.0, 1000.0, f"{_ANSYS}: corner-node Jacobian ratio"),
        ("fea_max_aspect_raw", "Longest edge over shortest height", "<=", 20.0, 1.0e6,
         f"{_ANSYS}: aspect ratio (read here as longest edge over shortest height)"),
        ("fea_max_face_angle_deg", "Largest angle in a face (degrees)", "<=", 165.0, 179.9, f"{_ANSYS}: maximum corner angle"),
    ),
}

PASS, WARNINGS, FAIL, UNMEASURED = "pass", "warnings", "fail", "unmeasured"


def _within(v: float, op: str, limit) -> bool:
    return v <= float(limit) if op == "<=" else v >= float(limit)


def judge(figures: Mapping[str, Any]) -> list[dict]:
    """One line per solver, in the download's order: {solver, label, file, element, tested,
    outcome, words, rows: [{key, label, measured, warn, error, status, source}]}."""
    out = []
    for key, label, file, element, tested in SOLVERS:
        rows: list[dict] = []
        worst = PASS
        for fk, what, op, warn, error, src in CHECKS[key]:
            v = figures.get(fk)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                status = UNMEASURED
            elif error is not None and not _within(float(v), op, error):
                status = FAIL
            elif warn is not None and not _within(float(v), op, warn):
                status = WARNINGS
            else:
                status = PASS
            rows.append({"key": fk, "label": what, "op": op, "measured": v, "warn": warn, "error": error,
                         "status": status, "source": src})
        if any(r["status"] == FAIL for r in rows):
            worst = FAIL
        elif any(r["status"] == WARNINGS for r in rows):
            worst = WARNINGS
        elif any(r["status"] == UNMEASURED for r in rows):
            worst = UNMEASURED
        words = {PASS: f"passes {label}'s element checks", WARNINGS: f"{label} would warn about some elements (listed)",
                 FAIL: f"{label} would refuse this mesh", UNMEASURED: f"not every {label} check was measured"}[worst]
        out.append({"solver": key, "label": label, "file": file, "element": element, "tested": bool(tested),
                    "tested_words": "solved in CalculiX in our tests" if tested else NOT_TESTED,
                    "outcome": worst, "words": words[0].upper() + words[1:] + ".", "rows": rows})
    return out


__all__ = ["CHECKS", "FAIL", "PASS", "UNMEASURED", "WARNINGS", "judge"]
