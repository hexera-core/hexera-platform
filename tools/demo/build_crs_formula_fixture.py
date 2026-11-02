"""Build the deterministic multi-defect Formula CAD used by the CRS client demo."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FaceSelector:
    name: str
    centroid_mm: tuple[float, float, float]
    area_mm2: float
    centroid_tolerance_mm: float = 0.5
    area_tolerance_mm2: float = 1.0


FORMULA_OPENINGS = (
    FaceSelector("floor_panel_starboard", (1497.5, 58.8, 495.3), 4891.3),
    FaceSelector("floor_panel_port", (505.9, 48.8, -495.4), 6072.4),
    FaceSelector("diffuser_floor_tab", (2761.7, 53.8, -477.1), 269.0),
)


def _read_step(path: Path):
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_Reader

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"could not read STEP source: {path}")
    reader.TransferRoots()
    return reader.OneShape()


def _write_step(shape, path: Path) -> None:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    path.parent.mkdir(parents=True, exist_ok=True)
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise ValueError(f"could not write STEP fixture: {path}")


def _properties(face) -> tuple[float, tuple[float, float, float]]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    centre = props.CentreOfMass()
    return float(props.Mass()), (float(centre.X()), float(centre.Y()), float(centre.Z()))


def _distance(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def _selected_faces(shape, selectors: Iterable[FaceSelector]):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    faces = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        faces.append(explorer.Current())
        explorer.Next()

    selected = []
    measurements = []
    for selector in selectors:
        matches = []
        for face in faces:
            area, centroid = _properties(face)
            if (abs(area - selector.area_mm2) <= selector.area_tolerance_mm2
                    and _distance(centroid, selector.centroid_mm)
                    <= selector.centroid_tolerance_mm):
                matches.append((face, area, centroid))
        if len(matches) != 1:
            raise ValueError(f"{selector.name} matched {len(matches)} faces, expected exactly one")
        face, area, centroid = matches[0]
        selected.append(face)
        measurements.append({
            "name": selector.name,
            "code": "open_shell",
            "severity": "error",
            "area_mm2": round(area, 6),
            "centroid_mm": [round(value, 6) for value in centroid],
        })
    return selected, measurements


def _box(shape) -> tuple[float, float, float, float, float, float]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    bounds = Bnd_Box()
    BRepBndLib.Add_s(shape, bounds)
    return tuple(float(value) for value in bounds.Get())


def _prior_demo_solid(shape):
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    expected = (1100.0, 25.0, -50.0, 1200.0, 35.0, 50.0)
    matches = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        solid = explorer.Current()
        if all(abs(actual - wanted) <= 0.01 for actual, wanted in zip(
                _box(solid), expected, strict=True)):
            matches.append(solid)
        explorer.Next()
    if len(matches) != 1:
        raise ValueError(f"prior demo solid matched {len(matches)} solids, expected exactly one")
    return matches[0]


def build_fixture(source: Path, output: Path, manifest: Path, *,
                  selectors: Iterable[FaceSelector], remove_prior_demo_solid: bool = False,
                  vehicle: str = "test fixture") -> None:
    from OCP.ShapeBuild import ShapeBuild_ReShape

    shape = _read_step(source)
    faces, defects = _selected_faces(shape, tuple(selectors))
    reshape = ShapeBuild_ReShape()
    for face in faces:
        reshape.Remove(face)
    if remove_prior_demo_solid:
        reshape.Remove(_prior_demo_solid(shape))
    broken = reshape.Apply(shape)
    _write_step(broken, output)

    payload = {
        "vehicle": vehicle,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "removed_prior_demo_solid": remove_prior_demo_solid,
        "defects": defects,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def build_formula_fixture(source: Path, output: Path, manifest: Path) -> None:
    build_fixture(source, output, manifest, selectors=FORMULA_OPENINGS,
                  remove_prior_demo_solid=True, vehicle="Formula 1000 CFD body")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    build_formula_fixture(args.source, args.output, args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
