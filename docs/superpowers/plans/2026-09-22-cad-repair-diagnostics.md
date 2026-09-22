# CAD Repair Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first CAD repair slice: a diagnostics-only repair core that inspects uploaded geometry and returns a structured repair report without mutating input bytes.

**Architecture:** Add a small `meshpipeline.cad.repair` package with typed report contracts, B-rep inspection for STEP/IGES via OCP, surface inspection for STL/VTP via existing STL/PyVista helpers, and one dispatcher over `MaterializedGeometry`. This plan intentionally does not add a pipeline node, API route, database table, harness target, or repair mutation.

**Tech Stack:** Python dataclasses/enums, existing `GeometrySourceRef`/`GeometryInterpretationRef`/`MaterializedGeometry`, OCP/OpenCASCADE, PyVista, NumPy, pytest.

**Spec:** `docs/superpowers/specs/2026-09-22-cad-repair-system-design.md`

## Global Constraints

- First slice is diagnostics only: no geometry mutation and no durable source replacement.
- Original uploads must never be overwritten.
- Inspection must not guess units. It may report file/source identity and raw geometric counts; physical mesh preparation remains in `cad.staging`.
- Defect reports are product output, not exceptions. Only infrastructure/contract failures should raise.
- The report vocabulary must use the defect codes from the design spec.
- No CGAL runtime dependency in this implementation.
- No graph, API, migration, console, or native mesh-image changes in this plan.

---

## File Structure

- Create `src/meshpipeline/cad/repair/__init__.py`: public exports for the repair diagnostics core.
- Create `src/meshpipeline/cad/repair/contracts.py`: enums, dataclasses, report/result serialization, and status derivation.
- Create `src/meshpipeline/cad/repair/surface.py`: inspect STL/VTP triangle surfaces without mutation.
- Create `src/meshpipeline/cad/repair/brep.py`: inspect STEP/IGES B-reps through OpenCASCADE without mutation.
- Create `src/meshpipeline/cad/repair/inspect.py`: dispatch from `MaterializedGeometry` to B-rep or surface inspection.
- Create `tests/unit/cad/test_repair_contracts.py`: contract/status serialization tests.
- Create `tests/unit/cad/test_repair_surface_inspect.py`: STL/VTP diagnostics tests.
- Create `tests/unit/cad/test_repair_brep_inspect.py`: STEP/IGES diagnostics tests.
- Create `tests/unit/cad/test_repair_inspect.py`: dispatcher tests over existing geometry contracts.

## Task 1: Repair Report Contracts

**Files:**
- Create: `src/meshpipeline/cad/repair/__init__.py`
- Create: `src/meshpipeline/cad/repair/contracts.py`
- Test: `tests/unit/cad/test_repair_contracts.py`

**Interfaces:**
- Produces:
  - `RepairMode`, `RepairProfile`, `RepairTarget`, `RepairStatus`, `DefectCode`, `DefectSeverity`
  - `RepairPolicy(mode: RepairMode, profile: RepairProfile, target: RepairTarget, engine: str = "")`
  - `RepairInput(source_id: str, sha256: str, suffix: str, interpretation_id: str, unit: str, basis: str, size_bytes: int)`
  - `RepairDefect(code: DefectCode, severity: DefectSeverity, message: str, count: int = 1, details: dict = field(default_factory=dict))`
  - `RepairMeasurement(name: str, value: object, unit: str = "")`
  - `RepairReport(defects: tuple[RepairDefect, ...], measurements: tuple[RepairMeasurement, ...], operations: tuple[dict, ...], summary: str, diagnostics: dict)`
  - `RepairResult(status: RepairStatus, input: RepairInput, policy: RepairPolicy, report: RepairReport, output: dict | None = None)`
  - `status_from_defects(defects: Sequence[RepairDefect]) -> RepairStatus`
  - `clean_report(summary: str, measurements: Sequence[RepairMeasurement] = (), diagnostics: dict | None = None) -> RepairReport`

- Consumes:
  - Standard library only.

- [ ] **Step 1: Write the failing contract tests**

Create `tests/unit/cad/test_repair_contracts.py`:

```python
# Responsibility: Verify CAD repair reports have stable vocabulary, status derivation and JSON shape.
from __future__ import annotations

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairInput,
    RepairMode,
    RepairPolicy,
    RepairProfile,
    RepairReport,
    RepairResult,
    RepairStatus,
    RepairTarget,
    clean_report,
    status_from_defects,
)


def _input() -> RepairInput:
    return RepairInput(
        source_id="source-1",
        sha256="a" * 64,
        suffix=".step",
        interpretation_id="interp-1",
        unit="millimetre",
        basis="user_confirmed",
        size_bytes=123,
    )


def test_clean_report_derives_clean_status_and_serializes():
    policy = RepairPolicy(
        mode=RepairMode.inspect,
        profile=RepairProfile.conservative,
        target=RepairTarget.standalone,
    )
    result = RepairResult(
        status=status_from_defects(()),
        input=_input(),
        policy=policy,
        report=clean_report("No repair needed."),
    )

    payload = result.to_dict()

    assert payload["status"] == "clean"
    assert payload["policy"]["mode"] == "inspect"
    assert payload["report"]["summary"] == "No repair needed."
    assert payload["report"]["defects"] == []
    assert payload["output"] is None


def test_error_severity_is_repairable_and_warning_is_clean_enough():
    warning = RepairDefect(
        code=DefectCode.small_edge,
        severity=DefectSeverity.warning,
        message="small edges found",
        count=2,
    )
    error = RepairDefect(
        code=DefectCode.invalid_brep,
        severity=DefectSeverity.error,
        message="B-rep validity failed",
    )

    assert status_from_defects((warning,)) is RepairStatus.clean
    assert status_from_defects((warning, error)) is RepairStatus.repairable


def test_fatal_defect_is_unrepairable():
    defect = RepairDefect(
        code=DefectCode.engine_staging_failure,
        severity=DefectSeverity.fatal,
        message="could not read CAD file",
    )

    assert status_from_defects((defect,)) is RepairStatus.unrepairable


def test_report_serialization_keeps_codes_and_details_stable():
    defect = RepairDefect(
        code=DefectCode.duplicate_surface_data,
        severity=DefectSeverity.warning,
        message="duplicate triangles found",
        count=3,
        details={"duplicate_faces": 3},
    )
    report = RepairReport(
        defects=(defect,),
        measurements=(),
        operations=({"name": "inspect_surface", "mutated": False},),
        summary="Repair recommended before meshing.",
        diagnostics={"format": "stl"},
    )

    payload = report.to_dict()

    assert payload["defects"][0]["code"] == "duplicate_surface_data"
    assert payload["defects"][0]["details"] == {"duplicate_faces": 3}
    assert payload["operations"] == [{"name": "inspect_surface", "mutated": False}]
```

- [ ] **Step 2: Run the contract tests to verify they fail**

Run:

```bash
pytest tests/unit/cad/test_repair_contracts.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'meshpipeline.cad.repair'`.

- [ ] **Step 3: Implement the contracts**

Create `src/meshpipeline/cad/repair/contracts.py`:

```python
# Responsibility: Define the stable product report emitted by CAD repair inspection.
# Boundaries: typed report shape only; it reads no geometry and mutates nothing.
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class RepairMode(str, Enum):
    inspect = "inspect"
    repair = "repair"


class RepairProfile(str, Enum):
    conservative = "conservative"
    mesh_ready = "mesh_ready"
    manual_review = "manual_review"


class RepairTarget(str, Enum):
    standalone = "standalone"
    meshing = "meshing"


class RepairStatus(str, Enum):
    clean = "clean"
    repairable = "repairable"
    repaired = "repaired"
    unrepairable = "unrepairable"
    inconclusive = "inconclusive"


class DefectSeverity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    fatal = "fatal"


class DefectCode(str, Enum):
    invalid_brep = "invalid_brep"
    open_shell = "open_shell"
    wire_gap = "wire_gap"
    small_edge = "small_edge"
    small_face = "small_face"
    degenerate_edge = "degenerate_edge"
    curve_inconsistency = "curve_inconsistency"
    self_intersection = "self_intersection"
    non_manifold_surface = "non_manifold_surface"
    duplicate_surface_data = "duplicate_surface_data"
    engine_staging_failure = "engine_staging_failure"


@dataclass(frozen=True, slots=True)
class RepairPolicy:
    mode: RepairMode
    profile: RepairProfile
    target: RepairTarget
    engine: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "profile": self.profile.value,
            "target": self.target.value,
            "engine": self.engine,
        }


@dataclass(frozen=True, slots=True)
class RepairInput:
    source_id: str
    sha256: str
    suffix: str
    interpretation_id: str
    unit: str
    basis: str
    size_bytes: int

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "sha256": self.sha256,
            "suffix": self.suffix,
            "interpretation_id": self.interpretation_id,
            "unit": self.unit,
            "basis": self.basis,
            "size_bytes": int(self.size_bytes),
        }


@dataclass(frozen=True, slots=True)
class RepairDefect:
    code: DefectCode
    severity: DefectSeverity
    message: str
    count: int = 1
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "count": int(self.count),
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class RepairMeasurement:
    name: str
    value: object
    unit: str = ""

    def to_dict(self) -> dict:
        out = {"name": self.name, "value": self.value}
        if self.unit:
            out["unit"] = self.unit
        return out


@dataclass(frozen=True, slots=True)
class RepairReport:
    defects: tuple[RepairDefect, ...]
    measurements: tuple[RepairMeasurement, ...]
    operations: tuple[dict, ...]
    summary: str
    diagnostics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "defects": [d.to_dict() for d in self.defects],
            "measurements": [m.to_dict() for m in self.measurements],
            "operations": [dict(op) for op in self.operations],
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class RepairResult:
    status: RepairStatus
    input: RepairInput
    policy: RepairPolicy
    report: RepairReport
    output: dict | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "input": self.input.to_dict(),
            "policy": self.policy.to_dict(),
            "report": self.report.to_dict(),
            "output": dict(self.output) if self.output is not None else None,
        }


def status_from_defects(defects: Sequence[RepairDefect]) -> RepairStatus:
    severities = {d.severity for d in defects}
    if DefectSeverity.fatal in severities:
        return RepairStatus.unrepairable
    if DefectSeverity.error in severities:
        return RepairStatus.repairable
    return RepairStatus.clean


def clean_report(
    summary: str,
    measurements: Sequence[RepairMeasurement] = (),
    diagnostics: dict | None = None,
) -> RepairReport:
    return RepairReport(
        defects=(),
        measurements=tuple(measurements),
        operations=({"name": "inspect", "mutated": False},),
        summary=summary,
        diagnostics=diagnostics or {},
    )
```

Create `src/meshpipeline/cad/repair/__init__.py`:

```python
"""Diagnostics-only CAD repair core."""

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairInput,
    RepairMeasurement,
    RepairMode,
    RepairPolicy,
    RepairProfile,
    RepairReport,
    RepairResult,
    RepairStatus,
    RepairTarget,
)

__all__ = [
    "DefectCode",
    "DefectSeverity",
    "RepairDefect",
    "RepairInput",
    "RepairMeasurement",
    "RepairMode",
    "RepairPolicy",
    "RepairProfile",
    "RepairReport",
    "RepairResult",
    "RepairStatus",
    "RepairTarget",
]
```

- [ ] **Step 4: Run the contract tests**

Run:

```bash
pytest tests/unit/cad/test_repair_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/__init__.py \
        src/meshpipeline/cad/repair/contracts.py \
        tests/unit/cad/test_repair_contracts.py
git commit -m "Add CAD repair report contracts"
```

## Task 2: Surface Diagnostics for STL and VTP

**Files:**
- Create: `src/meshpipeline/cad/repair/surface.py`
- Test: `tests/unit/cad/test_repair_surface_inspect.py`

**Interfaces:**
- Consumes:
  - `RepairDefect`, `RepairMeasurement`, `RepairReport`, `DefectCode`, `DefectSeverity` from Task 1.
  - `read_stl_triangles(path: Path) -> list[tuple]` from `meshpipeline.cad.stl_io`.
- Produces:
  - `inspect_surface_file(path: Path) -> RepairReport`

- [ ] **Step 1: Write failing surface diagnostics tests**

Create `tests/unit/cad/test_repair_surface_inspect.py`:

```python
# Responsibility: Verify STL/VTP repair inspection reports surface defects without changing bytes.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_stl, write_vtp

from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.surface import inspect_surface_file


def _codes(report):
    return {d.code for d in report.defects}


def test_simple_stl_reports_measurements_and_no_defects(tmp_path):
    path = write_stl(tmp_path / "tri.stl")
    before = path.read_bytes()

    report = inspect_surface_file(path)

    assert path.read_bytes() == before
    assert report.summary == "No repair needed."
    assert _codes(report) == set()
    measurements = {m.name: m.value for m in report.measurements}
    assert measurements["n_triangles"] == 1
    assert measurements["n_points"] == 3
    assert measurements["boundary_edges"] == 3


def test_duplicate_stl_triangle_is_reported(tmp_path):
    path = tmp_path / "dup.stl"
    base = write_stl(tmp_path / "base.stl").read_text()
    tri = "\n".join(base.splitlines()[1:-1])
    path.write_text("solid dup\n" + tri + "\n" + tri + "\nendsolid dup\n")

    report = inspect_surface_file(path)

    assert DefectCode.duplicate_surface_data in _codes(report)
    defect = next(d for d in report.defects if d.code is DefectCode.duplicate_surface_data)
    assert defect.details["duplicate_faces"] == 1


def test_degenerate_stl_triangle_is_reported(tmp_path):
    path = tmp_path / "degenerate.stl"
    path.write_text(
        "solid bad\n"
        "facet normal 0 0 1\n  outer loop\n"
        "    vertex 0 0 0\n    vertex 0 0 0\n    vertex 1 0 0\n"
        "  endloop\nendfacet\nendsolid bad\n"
    )

    report = inspect_surface_file(path)

    assert DefectCode.degenerate_edge in _codes(report)


def test_vtp_surface_reports_measurements(tmp_path):
    pytest.importorskip("pyvista")
    path = write_vtp(tmp_path / "tri.vtp")

    report = inspect_surface_file(path)

    measurements = {m.name: m.value for m in report.measurements}
    assert measurements["format"] == "vtp"
    assert measurements["n_triangles"] == 1
    assert measurements["n_points"] == 3
```

- [ ] **Step 2: Run the surface tests to verify they fail**

Run:

```bash
pytest tests/unit/cad/test_repair_surface_inspect.py -q
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `meshpipeline.cad.repair.surface`.

- [ ] **Step 3: Implement surface diagnostics**

Create `src/meshpipeline/cad/repair/surface.py`:

```python
# Responsibility: Inspect triangulated surface files for repair-relevant defects.
# Boundaries: diagnostics only; never mutates geometry and never infers physical units.
from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMeasurement,
    RepairReport,
)
from meshpipeline.cad.stl_io import read_stl_triangles


def _triangles_from_stl(path: Path) -> np.ndarray:
    tris = read_stl_triangles(path)
    return np.asarray(tris, dtype=float).reshape((-1, 3, 3)) if tris else np.empty((0, 3, 3))


def _triangles_from_vtp(path: Path) -> np.ndarray:
    import pyvista as pv

    mesh = pv.read(str(path)).extract_surface().triangulate()
    if mesh.n_cells == 0:
        return np.empty((0, 3, 3))
    faces = mesh.faces.reshape(-1, 4)[:, 1:]
    pts = np.asarray(mesh.points, dtype=float)
    return pts[faces]


def _triangle_key(tri: np.ndarray) -> tuple:
    return tuple(sorted(tuple(round(float(c), 12) for c in p) for p in tri))


def _edge_key(a: np.ndarray, b: np.ndarray) -> tuple:
    pa = tuple(round(float(c), 12) for c in a)
    pb = tuple(round(float(c), 12) for c in b)
    return tuple(sorted((pa, pb)))


def _surface_metrics(tris: np.ndarray) -> dict:
    if len(tris) == 0:
        return {
            "n_triangles": 0,
            "n_points": 0,
            "bbox_min": [],
            "bbox_max": [],
            "degenerate_faces": 0,
            "duplicate_faces": 0,
            "boundary_edges": 0,
            "non_manifold_edges": 0,
        }

    points = tris.reshape(-1, 3)
    unique_points = {tuple(round(float(c), 12) for c in p) for p in points}
    areas = 0.5 * np.linalg.norm(
        np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1
    )
    face_counts = Counter(_triangle_key(tri) for tri in tris)
    edge_counts = Counter()
    for tri in tris:
        edge_counts[_edge_key(tri[0], tri[1])] += 1
        edge_counts[_edge_key(tri[1], tri[2])] += 1
        edge_counts[_edge_key(tri[2], tri[0])] += 1

    return {
        "n_triangles": int(len(tris)),
        "n_points": int(len(unique_points)),
        "bbox_min": points.min(axis=0).round(12).tolist(),
        "bbox_max": points.max(axis=0).round(12).tolist(),
        "degenerate_faces": int((areas <= 0.0).sum()),
        "duplicate_faces": int(sum(v - 1 for v in face_counts.values() if v > 1)),
        "boundary_edges": int(sum(1 for v in edge_counts.values() if v == 1)),
        "non_manifold_edges": int(sum(1 for v in edge_counts.values() if v > 2)),
    }


def _defects(metrics: dict) -> tuple[RepairDefect, ...]:
    defects: list[RepairDefect] = []
    if metrics["degenerate_faces"]:
        defects.append(RepairDefect(
            code=DefectCode.degenerate_edge,
            severity=DefectSeverity.error,
            message="Degenerate surface triangles were found.",
            count=metrics["degenerate_faces"],
            details={"degenerate_faces": metrics["degenerate_faces"]},
        ))
    if metrics["duplicate_faces"]:
        defects.append(RepairDefect(
            code=DefectCode.duplicate_surface_data,
            severity=DefectSeverity.warning,
            message="Duplicate surface triangles were found.",
            count=metrics["duplicate_faces"],
            details={"duplicate_faces": metrics["duplicate_faces"]},
        ))
    if metrics["non_manifold_edges"]:
        defects.append(RepairDefect(
            code=DefectCode.non_manifold_surface,
            severity=DefectSeverity.error,
            message="Surface edges used by more than two triangles were found.",
            count=metrics["non_manifold_edges"],
            details={"non_manifold_edges": metrics["non_manifold_edges"]},
        ))
    return tuple(defects)


def inspect_surface_file(path: Path) -> RepairReport:
    p = Path(path)
    suffix = p.suffix.lower()
    tris = _triangles_from_vtp(p) if suffix == ".vtp" else _triangles_from_stl(p)
    metrics = _surface_metrics(tris)
    metrics["format"] = suffix.lstrip(".")
    defects = _defects(metrics)
    summary = "Repair recommended before meshing." if defects else "No repair needed."
    measurements = tuple(RepairMeasurement(name=k, value=v) for k, v in metrics.items())
    return RepairReport(
        defects=defects,
        measurements=measurements,
        operations=({"name": "inspect_surface", "mutated": False},),
        summary=summary,
        diagnostics={"path_suffix": suffix},
    )
```

- [ ] **Step 4: Run the surface tests**

Run:

```bash
pytest tests/unit/cad/test_repair_surface_inspect.py -q
```

Expected: PASS, with the VTP test skipped only if PyVista is genuinely unavailable in the unit environment.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/surface.py \
        tests/unit/cad/test_repair_surface_inspect.py
git commit -m "Inspect surface geometry for CAD repair"
```

## Task 3: B-rep Diagnostics for STEP and IGES

**Files:**
- Create: `src/meshpipeline/cad/repair/brep.py`
- Test: `tests/unit/cad/test_repair_brep_inspect.py`

**Interfaces:**
- Consumes:
  - `RepairDefect`, `RepairMeasurement`, `RepairReport`, `DefectCode`, `DefectSeverity` from Task 1.
  - OCP modules already present in the runtime requirements through `cadquery-ocp`.
- Produces:
  - `inspect_brep_file(path: Path) -> RepairReport`

- [ ] **Step 1: Write failing B-rep diagnostics tests**

Create `tests/unit/cad/test_repair_brep_inspect.py`:

```python
# Responsibility: Verify STEP/IGES repair inspection reports B-rep validity without mutation.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_iges, write_step

pytest.importorskip("OCP.STEPControl")

from meshpipeline.cad.repair.brep import inspect_brep_file  # noqa: E402
from meshpipeline.cad.repair.contracts import DefectCode  # noqa: E402


def _measurements(report):
    return {m.name: m.value for m in report.measurements}


def _codes(report):
    return {d.code for d in report.defects}


def test_valid_step_reports_shape_counts_and_no_mutation(tmp_path):
    path = write_step(tmp_path / "box.step", "MM")
    before = path.read_bytes()

    report = inspect_brep_file(path)

    assert path.read_bytes() == before
    assert report.summary == "No repair needed."
    assert _codes(report) == set()
    measurements = _measurements(report)
    assert measurements["format"] == "step"
    assert measurements["is_valid"] is True
    assert measurements["faces"] >= 6
    assert measurements["edges"] >= 12


def test_valid_iges_reports_shape_counts(tmp_path):
    path = write_iges(tmp_path / "box.iges", "MM")

    report = inspect_brep_file(path)

    measurements = _measurements(report)
    assert measurements["format"] == "iges"
    assert measurements["is_valid"] is True
    assert measurements["faces"] >= 6


def test_unreadable_cad_is_reported_as_invalid_brep(tmp_path):
    path = tmp_path / "broken.step"
    path.write_text("not a STEP file")

    report = inspect_brep_file(path)

    assert DefectCode.invalid_brep in _codes(report)
    assert _measurements(report)["is_valid"] is False
```

- [ ] **Step 2: Run the B-rep tests to verify they fail**

Run:

```bash
pytest tests/unit/cad/test_repair_brep_inspect.py -q
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `meshpipeline.cad.repair.brep`.

- [ ] **Step 3: Implement B-rep diagnostics**

Create `src/meshpipeline/cad/repair/brep.py`:

```python
# Responsibility: Inspect CAD B-rep files for repair-relevant defects.
# Boundaries: diagnostics only; never mutates geometry and never exports repaired shapes.
from __future__ import annotations

from pathlib import Path

from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairMeasurement,
    RepairReport,
)


def _reader_for(path: Path):
    from OCP.IGESControl import IGESControl_Reader
    from OCP.STEPControl import STEPControl_Reader

    return IGESControl_Reader() if path.suffix.lower() in (".iges", ".igs") else STEPControl_Reader()


def _read_shape(path: Path):
    from OCP.IFSelect import IFSelect_RetDone

    reader = _reader_for(path)
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"OpenCASCADE could not read CAD file: {path.name}")
    reader.TransferRoots()
    return reader.OneShape()


def _count_subshapes(shape) -> dict:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer

    def count(kind) -> int:
        n = 0
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            n += 1
            explorer.Next()
        return n

    return {
        "solids": count(TopAbs_SOLID),
        "shells": count(TopAbs_SHELL),
        "faces": count(TopAbs_FACE),
        "edges": count(TopAbs_EDGE),
        "vertices": count(TopAbs_VERTEX),
    }


def _is_valid(shape) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape, True).IsValid())


def inspect_brep_file(path: Path) -> RepairReport:
    p = Path(path)
    suffix = p.suffix.lower()
    fmt = "iges" if suffix in (".iges", ".igs") else "step"
    try:
        shape = _read_shape(p)
        counts = _count_subshapes(shape)
        is_valid = _is_valid(shape)
        defects = ()
        if not is_valid:
            defects = (RepairDefect(
                code=DefectCode.invalid_brep,
                severity=DefectSeverity.error,
                message="OpenCASCADE reported the B-rep as invalid.",
            ),)
        measurements = {"format": fmt, "is_valid": is_valid, **counts}
        summary = "Repair recommended before meshing." if defects else "No repair needed."
    except Exception as exc:  # noqa: BLE001 - an unreadable CAD file is a reportable input defect
        defects = (RepairDefect(
            code=DefectCode.invalid_brep,
            severity=DefectSeverity.fatal,
            message="OpenCASCADE could not read this CAD file.",
            details={"error": type(exc).__name__},
        ),)
        measurements = {
            "format": fmt,
            "is_valid": False,
            "solids": 0,
            "shells": 0,
            "faces": 0,
            "edges": 0,
            "vertices": 0,
        }
        summary = "Automatic repair was not safe for this geometry."

    return RepairReport(
        defects=defects,
        measurements=tuple(RepairMeasurement(name=k, value=v) for k, v in measurements.items()),
        operations=({"name": "inspect_brep", "mutated": False},),
        summary=summary,
        diagnostics={"path_suffix": suffix},
    )
```

- [ ] **Step 4: Run the B-rep tests**

Run:

```bash
pytest tests/unit/cad/test_repair_brep_inspect.py -q
```

Expected: PASS where OCP is available; SKIP only if the environment genuinely lacks OCP.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/brep.py \
        tests/unit/cad/test_repair_brep_inspect.py
git commit -m "Inspect B-rep geometry for CAD repair"
```

## Task 4: Geometry Dispatcher

**Files:**
- Create: `src/meshpipeline/cad/repair/inspect.py`
- Modify: `src/meshpipeline/cad/repair/__init__.py`
- Test: `tests/unit/cad/test_repair_inspect.py`

**Interfaces:**
- Consumes:
  - `MaterializedGeometry` from `meshpipeline.contracts.geometry_source`.
  - `inspect_surface_file(path: Path) -> RepairReport` from Task 2.
  - `inspect_brep_file(path: Path) -> RepairReport` from Task 3.
- Produces:
  - `repair_input_for(geometry: MaterializedGeometry) -> RepairInput`
  - `inspect_geometry(geometry: MaterializedGeometry, *, profile: RepairProfile = RepairProfile.conservative, target: RepairTarget = RepairTarget.standalone, engine: str = "") -> RepairResult`

- [ ] **Step 1: Write failing dispatcher tests**

Create `tests/unit/cad/test_repair_inspect.py`:

```python
# Responsibility: Verify CAD repair inspection dispatches from the materialized geometry contract.
from __future__ import annotations

import pytest
from tests._geometry_support import materialized
from tests.cad_fixtures import write_step, write_stl

from meshpipeline.cad.repair.contracts import RepairStatus, RepairTarget
from meshpipeline.cad.repair.inspect import inspect_geometry
from meshpipeline.contracts.geometry_units import LengthUnit


def test_inspect_geometry_reports_source_and_interpretation_identity_for_stl(tmp_path):
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.stl")
    write_stl(geom.local_path)

    result = inspect_geometry(geom, target=RepairTarget.meshing, engine="snappy")
    payload = result.to_dict()

    assert result.status is RepairStatus.clean
    assert payload["input"]["source_id"] == geom.ref.source_id
    assert payload["input"]["interpretation_id"] == geom.interpretation.interpretation_id
    assert payload["input"]["suffix"] == ".stl"
    assert payload["policy"]["target"] == "meshing"
    assert payload["policy"]["engine"] == "snappy"
    assert payload["output"] is None


def test_inspect_geometry_dispatches_step(tmp_path):
    pytest.importorskip("OCP.STEPControl")
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.step")
    write_step(geom.local_path, "MM")

    result = inspect_geometry(geom)

    assert result.status is RepairStatus.clean
    measurements = {m.name: m.value for m in result.report.measurements}
    assert measurements["format"] == "step"


def test_unsupported_suffix_is_unrepairable_report(tmp_path):
    geom = materialized(tmp_path / "src", unit=LengthUnit.millimetre, filename="part.obj")
    geom.local_path.write_text("not supported")

    result = inspect_geometry(geom)

    assert result.status is RepairStatus.unrepairable
    assert result.report.defects[0].code.value == "engine_staging_failure"
```

- [ ] **Step 2: Run the dispatcher tests to verify they fail**

Run:

```bash
pytest tests/unit/cad/test_repair_inspect.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing `inspect_geometry`.

- [ ] **Step 3: Implement the dispatcher**

Create `src/meshpipeline/cad/repair/inspect.py`:

```python
# Responsibility: Dispatch repair inspection for one verified materialized geometry.
# Boundaries: diagnostics only; it never stages, repairs, uploads, or replaces geometry.
from __future__ import annotations

from pathlib import Path

from meshpipeline.cad.repair.brep import inspect_brep_file
from meshpipeline.cad.repair.contracts import (
    DefectCode,
    DefectSeverity,
    RepairDefect,
    RepairInput,
    RepairMode,
    RepairPolicy,
    RepairProfile,
    RepairReport,
    RepairResult,
    RepairStatus,
    RepairTarget,
    status_from_defects,
)
from meshpipeline.cad.repair.surface import inspect_surface_file
from meshpipeline.contracts.geometry_source import MaterializedGeometry

_BREP_SUFFIXES = {".step", ".stp", ".iges", ".igs"}
_SURFACE_SUFFIXES = {".stl", ".vtp"}


def repair_input_for(geometry: MaterializedGeometry) -> RepairInput:
    return RepairInput(
        source_id=geometry.ref.source_id,
        sha256=geometry.ref.sha256,
        suffix=geometry.ref.suffix_hint,
        interpretation_id=geometry.interpretation.interpretation_id,
        unit=geometry.interpretation.unit,
        basis=geometry.interpretation.basis,
        size_bytes=geometry.ref.size_bytes,
    )


def _unsupported_report(suffix: str) -> RepairReport:
    return RepairReport(
        defects=(RepairDefect(
            code=DefectCode.engine_staging_failure,
            severity=DefectSeverity.fatal,
            message=f"CAD repair inspection does not support geometry suffix {suffix!r}.",
        ),),
        measurements=(),
        operations=({"name": "inspect_dispatch", "mutated": False},),
        summary="Automatic repair was not safe for this geometry.",
        diagnostics={"path_suffix": suffix},
    )


def inspect_geometry(
    geometry: MaterializedGeometry,
    *,
    profile: RepairProfile = RepairProfile.conservative,
    target: RepairTarget = RepairTarget.standalone,
    engine: str = "",
) -> RepairResult:
    path = Path(geometry.path)
    suffix = path.suffix.lower()
    if suffix in _BREP_SUFFIXES:
        report = inspect_brep_file(path)
    elif suffix in _SURFACE_SUFFIXES:
        report = inspect_surface_file(path)
    else:
        report = _unsupported_report(suffix)

    return RepairResult(
        status=status_from_defects(report.defects),
        input=repair_input_for(geometry),
        policy=RepairPolicy(
            mode=RepairMode.inspect,
            profile=profile,
            target=target,
            engine=engine,
        ),
        report=report,
        output=None,
    )
```

Modify `src/meshpipeline/cad/repair/__init__.py` to export the dispatcher:

```python
from meshpipeline.cad.repair.inspect import inspect_geometry, repair_input_for

__all__ = [
    # keep the existing names
    "inspect_geometry",
    "repair_input_for",
]
```

When editing, preserve the existing exports from Task 1 and add the two new names rather than
replacing the list.

- [ ] **Step 4: Run the dispatcher tests**

Run:

```bash
pytest tests/unit/cad/test_repair_inspect.py -q
```

Expected: PASS where OCP is available; STEP-specific test may skip if OCP is unavailable.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/cad/repair/__init__.py \
        src/meshpipeline/cad/repair/inspect.py \
        tests/unit/cad/test_repair_inspect.py
git commit -m "Dispatch CAD repair inspection by geometry type"
```

## Task 5: Slice Verification

**Files:**
- No new files.

**Interfaces:**
- Consumes all interfaces from Tasks 1-4.
- Produces a verified diagnostics-only CAD repair core.

- [ ] **Step 1: Run the focused CAD repair tests**

Run:

```bash
pytest \
  tests/unit/cad/test_repair_contracts.py \
  tests/unit/cad/test_repair_surface_inspect.py \
  tests/unit/cad/test_repair_brep_inspect.py \
  tests/unit/cad/test_repair_inspect.py \
  -q
```

Expected: PASS. Skips are acceptable only for tests explicitly guarded by `pytest.importorskip`
when the local environment lacks OCP or PyVista.

- [ ] **Step 2: Run the existing CAD unit tests most likely to catch boundary drift**

Run:

```bash
pytest \
  tests/unit/cad/test_unit_evidence.py \
  tests/unit/cad/test_surface_family_metres.py \
  tests/unit/cad/test_metre_normalisation.py \
  -q
```

Expected: PASS. This proves the repair diagnostics did not disturb existing unit resolution,
surface preparation, or normalization behavior.

- [ ] **Step 3: Run import hygiene for the new package**

Run:

```bash
python - <<'PY'
from meshpipeline.cad.repair import inspect_geometry, RepairResult
print(inspect_geometry.__name__, RepairResult.__name__)
PY
```

Expected output:

```text
inspect_geometry RepairResult
```

- [ ] **Step 4: Check formatting-sensitive whitespace**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 5: Commit any verification-only fixes**

If Step 1-4 required fixes, commit them:

```bash
git add src/meshpipeline/cad/repair tests/unit/cad/test_repair_*.py
git commit -m "Stabilize CAD repair diagnostics"
```

If no fixes were needed, do not create an empty commit.

