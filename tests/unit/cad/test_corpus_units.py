# Responsibility: Verify, over every real file in the CFD corpus, that the unit the product would use
# - the file's declared unit, or the other reading the stage proposes when the part's size makes
# that implausible - is the file's true unit, and that the triangle files are proposed a unit
# rather than refused.
# Boundaries: the real corpus (licensed files a clean clone does not have), measured the way the
# product measures it: OpenCASCADE's transfer for STEP and IGES, the scout's triangle reader for
# STL and OBJ. The truth for each file is the corpus notes (cfd_cases.json, cfd2_cases.json,
# cfd2_src/MANIFEST.csv, the trainshapes generator's meta), or the file's declared unit where the
# notes are silent. Run with `make test-external-fixtures`.
from __future__ import annotations

import functools
from pathlib import Path

import pytest

CORPUS = Path(__file__).parents[3] / "tests" / "fixtures" / "external" / "corpus"
# external_fixture: needs tests/fixtures/external/corpus (the licensed CFD corpus; a link to it).
pytestmark = [
    pytest.mark.external_fixture,
    pytest.mark.skipif(not CORPUS.exists(), reason="the CFD corpus is not linked at tests/fixtures/external/corpus"),
]

M, MM, IN, CM = "m", "mm", "in", "cm"

#: file -> (true unit, what the user says the part is, why that is the truth). The words are
#: what a user types in the first answer; the IEA blade and the building block are the files
#: whose unit only those words can settle (`NEEDS_WORDS`).
TRUTH: dict[str, tuple[str, str, str]] = {
    # --- cfd_raw
    "cfd_raw/crm_highlift_landing.step": (IN, "the NASA high-lift CRM airliner, landing", "cfd_cases: 64.6 m full-scale aircraft"),
    "cfd_raw/crm_highlift_takeoff.step": (IN, "the NASA high-lift CRM airliner, takeoff", "cfd_cases: 64.6 m full-scale aircraft"),
    "cfd_raw/crm_transonic_airliner.step": (IN, "the NASA CRM transonic airliner", "MANIFEST (DPW-4 CRM): 64.5 m"),
    "cfd_raw/drivaer_estate.step": (M, "the DrivAer estate car in its wind tunnel", "declared; ANSA export, car in a 172 m tunnel box"),
    "cfd_raw/drivaer_fastback.step": (M, "the DrivAer fastback car in its wind tunnel", "declared; ANSA export, car in a 172 m tunnel box"),
    "cfd_raw/enodise_propeller.step": (MM, "a two-blade propeller", "cfd_cases: 305 mm"),
    "cfd_raw/fda_pump_housing.step": (MM, "the FDA blood pump housing", "cfd_cases: millimetres (MANIFEST: suspect, 10x)"),
    "cfd_raw/fda_pump_rotor.step": (MM, "the FDA blood pump rotor", "cfd_cases: millimetres (MANIFEST: suspect, 10x)"),
    "cfd_raw/hvac_transition_duct.step": (MM, "an HVAC square-to-round duct", "cfd_cases: 897 mm"),
    "cfd_raw/iea_15mw_turbine_blade.step": (M, "External flow around the IEA 15 MW wind turbine blade", "MANIFEST: 117 m span; header says mm"),
    "cfd_raw/nasa_c608_supersonic.step": (IN, "the NASA C608 supersonic low-boom aircraft", "cfd_cases: 31.9 m"),
    "cfd_raw/onera_m6_wing.step": (MM, "the ONERA M6 wing", "cfd_cases: 1.15 m"),
    "cfd_raw/pipe_reducer.step": (MM, "a concentric pipe reducer", "cfd_cases: 1000 mm"),
    "cfd_raw/pvc_mixing_tee.step": (MM, "a PVC mixing tee pipe fitting", "cfd_cases: 82 mm"),
    "cfd_raw/rocket_nozzle.step": (IN, "a rocket engine nozzle", "cfd_cases: 76.2 mm = 3 in"),
    "cfd_raw/sae_notchback.step": (IN, "the SAE notchback car body", "cfd_cases: 840 mm"),
    "cfd_raw/volute_pump_domains.step": (MM, "a centrifugal pump flow path", "cfd_cases: 662 mm"),
    "cfd_raw/windsor_body.step": (MM, "the Windsor body", "declared; the WindsorML body, 1.06 m"),
    # --- cfd2_raw (the same files as cfd2_src, flattened)
    "cfd2_raw/dlr_f6.stp": (IN, "the DLR-F6 wing-body aircraft model", "MANIFEST: 1500 mm"),
    "cfd2_raw/duct_circ_bend_red.step": (MM, "a circular duct bend", "cfd2_cases: 1110 mm"),
    "cfd2_raw/duct_radius_elbow.step": (MM, "a rectangular duct elbow", "cfd2_cases: 1316 mm"),
    "cfd2_raw/duct_square_round.step": (MM, "a square-to-round duct", "MANIFEST: 897 mm"),
    "cfd2_raw/fan_reducer_duct_120mm.step": (MM, "a 120 mm fan adapter plate", "MANIFEST: 120 mm"),
    "cfd2_raw/fan_right_angle_duct_80mm.step": (MM, "an 80 mm fan adapter plate", "MANIFEST: 80 mm"),
    "cfd2_raw/fluid_circ_bend_reduction.step": (MM, "the fluid volume of a duct bend", "cfd2_cases: 1080 mm"),
    "cfd2_raw/fluid_radius_elbow.step": (MM, "the fluid volume of a duct elbow", "cfd2_cases: 1222 mm"),
    "cfd2_raw/fluid_square_round.step": (MM, "the fluid volume of a duct transition", "cfd2_cases: 496 mm"),
    "cfd2_raw/iea15mw_turbine.stp": (M, "External flow around the IEA 15 MW wind turbine blade", "MANIFEST: 117 m span; header says mm"),
    "cfd2_raw/impeller_cfd_domain.step": (MM, "one blade passage of an impeller", "cfd2_cases: 203 mm"),
    "cfd2_raw/pump_cfd_domains.step": (MM, "a centrifugal pump flow path", "MANIFEST: 662 mm"),
    "cfd2_raw/pump_housing.stp": (MM, "the FDA blood pump housing", "declared (MANIFEST: suspect, 10x - no unit we carry fixes it)"),
    "cfd2_raw/pump_impeller_blades.step": (MM, "a centrifugal pump impeller", "cfd2_cases: 288 mm"),
    "cfd2_raw/pump_rotor.stp": (MM, "the FDA blood pump rotor", "declared (MANIFEST: suspect, 10x)"),
    "cfd2_raw/stealthburner_assembly.step": (MM, "a 3D printer toolhead assembly", "MANIFEST: millimetres"),
    "cfd2_raw/t106_turbine_cascade.step": (M, "the T106 turbine blade cascade", "MANIFEST: 816 mm; the numbers are metres"),
    "cfd2_raw/windsor_body.stp": (MM, "the Windsor body", "cfd2_cases: 1059 mm"),
    "cfd2_raw/wing_fuselage.step": (M, "a small aircraft wing and fuselage", "cfd2_cases: 447 mm; the numbers are metres"),
    # --- cfd2_src, triangle files and the IGES wing
    "cfd2_src/01_external_aero/ahmed_25deg.stl": (M, "External aero over the Ahmed body", "MANIFEST: metres, 1.044 m"),
    "cfd2_src/01_external_aero/onera_m6.igs": (MM, "the ONERA M6 wing", "MANIFEST: 1218 mm"),
    "cfd2_src/02_internal_flow/cyclone.stl": (M, "a cyclone separator", "MANIFEST: metres"),
    "cfd2_src/04_stress_cases/motorBike.stl": (M, "the OpenFOAM motorbike and rider", "MANIFEST: metres"),
    "cfd2_src/extras/ahmed_35deg.stl": (M, "the Ahmed body, 35 degree slant", "MANIFEST: metres"),
    "cfd2_src/extras/buildings.obj": (M, "Wind around a city block of buildings", "MANIFEST: metres, 229 m"),
    "cfd2_src/extras/drivaer_body.obj": (M, "the DrivAer fastback car body", "MANIFEST: metres"),
    "cfd2_src/extras/flange.stl": (M, "a pipe flange", "MANIFEST: metres, 52 mm"),
    "cfd2_src/extras/pipe.obj": (MM, "a straight pipe", "MANIFEST: millimetres, 144 mm"),
    "cfd2_src/extras/propeller.obj": (M, "a propeller", "MANIFEST: metres"),
    "cfd2_src/extras/windsor_body_STLmirror.stl": (M, "the Windsor body", "MANIFEST: metres"),
    # --- final15 (the admitted fifteen)
    "final15/01_airliner_highlift_takeoff.step": (IN, "an airliner in takeoff configuration", "cfd_cases: 64.6 m"),
    "final15/02_supersonic_aircraft.step": (IN, "a supersonic aircraft", "cfd_cases: 31.9 m"),
    "final15/03_onera_m6_wing.step": (MM, "the ONERA M6 wing", "cfd_cases: 1.15 m"),
    "final15/04_sae_car_body.step": (IN, "the SAE car body", "cfd_cases: 840 mm"),
    "final15/05_propeller.step": (MM, "a propeller", "cfd_cases: 305 mm"),
    "final15/06_pump_impeller.step": (MM, "a pump impeller", "cfd_cases: millimetres"),
    "final15/07_rocket_nozzle.step": (IN, "a rocket nozzle", "cfd_cases: 76.2 mm"),
    "final15/08_pump_volute_housing.step": (MM, "a pump volute housing", "cfd_cases: millimetres"),
    "final15/09_mixing_tee.step": (MM, "a mixing tee", "cfd_cases: 82 mm"),
    "final15/10_pipe_reducer.step": (MM, "a pipe reducer", "cfd_cases: 1000 mm"),
    "final15/11_heat_sink.step": (MM, "a heat sink", "declared; 80 mm"),
    "final15/12_y_manifold.step": (MM, "a Y manifold", "declared; 433 mm"),
    "final15/13_pipe_elbow.step": (MM, "a pipe elbow", "declared; 230 mm"),
    "final15/14_combining_wye.step": (MM, "a combining wye", "declared; 433 mm"),
    "final15/15_mounting_bracket.step": (CM, "a mounting bracket", "declared; 12 cm"),
}
# the cfd2_src copies of the cfd2_raw STEP files carry the same truth
for _name in ("dlr_f6.stp", "windsor_body.stp", "wing_fuselage.step"):
    TRUTH[f"cfd2_src/01_external_aero/{_name}"] = TRUTH[f"cfd2_raw/{_name}"]
for _name in ("duct_circ_bend_red.step", "duct_radius_elbow.step", "duct_square_round.step",
              "fluid_circ_bend_reduction.step", "fluid_radius_elbow.step", "fluid_square_round.step",
              "impeller_cfd_domain.step", "pump_housing.stp", "pump_rotor.stp"):
    TRUTH[f"cfd2_src/02_internal_flow/{_name}"] = TRUTH[f"cfd2_raw/{_name}"]
for _name in ("iea15mw_turbine.stp", "pump_impeller_blades.step"):
    TRUTH[f"cfd2_src/03_turbomachinery/{_name}"] = TRUTH[f"cfd2_raw/{_name}"]
TRUTH["cfd2_src/04_stress_cases/stealthburner_assembly.step"] = TRUTH["cfd2_raw/stealthburner_assembly.step"]
for _name in ("fan_reducer_duct_120mm.step", "fan_right_angle_duct_80mm.step", "pump_cfd_domains.step",
              "t106_turbine_cascade.step"):
    TRUTH[f"cfd2_src/extras/{_name}"] = TRUTH[f"cfd2_raw/{_name}"]

#: The files whose unit the size cannot settle on its own: a 117 "mm" blade is a believable pin,
#: a 229 "mm" block a believable box. What the user says the part is settles them.
NEEDS_WORDS = {"cfd_raw/iea_15mw_turbine_blade.step", "cfd2_raw/iea15mw_turbine.stp",
               "cfd2_src/03_turbomachinery/iea15mw_turbine.stp", "cfd2_src/extras/buildings.obj"}
TRAINSHAPES = "trainshapes/raw"


@functools.cache
def _longest_file_units(path: Path) -> float:
    """The part's longest side in the file's own numbers, measured the way the product does.
    Measured once per file: the airliners take most of a minute to transfer."""
    suffix = path.suffix.lower()
    if suffix in (".stl", ".obj", ".vtp"):
        from meshpipeline.cad.scout_mesh import read_triangles

        pts = read_triangles(path).reshape(-1, 3)
        return float((pts.max(axis=0) - pts.min(axis=0)).max())
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.IFSelect import IFSelect_RetDone

    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.geometry_units import scale_to_metres

    if suffix in (".igs", ".iges"):
        from OCP.IGESControl import IGESControl_Reader
        reader = IGESControl_Reader()
    else:
        from OCP.STEPControl import STEPControl_Reader
        reader = STEPControl_Reader()
    assert reader.ReadFile(str(path)) == IFSelect_RetDone
    reader.TransferRoots()
    box = Bnd_Box()
    BRepBndLib.Add_s(reader.OneShape(), box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    occ_mm = max(x1 - x0, y1 - y0, z1 - z0)
    # OpenCASCADE hands back millimetres; the file's own numbers are those over its applied unit
    return occ_mm * 0.001 / scale_to_metres(parser_applied_unit(path))


def product_unit(path: Path, words: str) -> tuple[str, str | None]:
    """The unit the product uses for this file when the user proceeds on the stage: the file's
    declaration (millimetres when it has none), or the other reading the stage proposes for
    them to pick. Returns (that unit, the declared unit or None)."""
    from meshpipeline.cad.unit_evidence import read_declared_unit
    from meshpipeline.contracts.geometry_units import LengthUnit
    from meshpipeline.contracts.unit_plausibility import expected_from_words, suggest

    evidence = read_declared_unit(path)
    declared = evidence.unit if evidence.resolved else None
    in_effect = declared or LengthUnit.millimetre
    s = suggest(_longest_file_units(path), in_effect, declared=declared is not None,
                expected=expected_from_words(words))
    return (s.unit if s else in_effect).value, (declared.value if declared else None)


def _named() -> list[str]:
    return sorted(TRUTH)


@pytest.mark.parametrize("rel", _named())
def test_every_named_corpus_file_is_read_in_its_true_unit(rel):
    path = CORPUS / rel
    assert path.exists(), f"{rel} is missing from the corpus"
    truth, words, why = TRUTH[rel]
    used, declared = product_unit(path, words)
    assert used == truth, f"{rel}: the product would use {used} (declared {declared}); the truth is {truth} ({why})"
    # without the user's words the size alone settles every file but the few that need them
    bare, _ = product_unit(path, "")
    assert (bare == truth) is (rel not in NEEDS_WORDS), (
        f"{rel}: with no words the product would use {bare}; the truth is {truth}")


def test_every_trainshapes_file_is_read_in_millimetres():
    """The 276 generated shapes: the generator writes millimetres, and every header says so."""
    from meshpipeline.cad.unit_evidence import read_declared_unit

    files = sorted((CORPUS / TRAINSHAPES).glob("*.step"))
    assert len(files) >= 270
    wrong = []
    for f in files:
        ev = read_declared_unit(f)
        family = " ".join(p for p in f.stem.split("_") if not p.isdigit() and p != "fluid")
        if not (ev.resolved and ev.unit.value == MM) or product_unit(f, family)[0] != MM:
            wrong.append(f.name)
    assert wrong == []


def test_no_named_corpus_file_is_left_out():
    """A file added to the corpus without a truth here would go untested."""
    on_disk = {str(p.relative_to(CORPUS)) for d in ("cfd_raw", "cfd2_raw", "cfd2_src", "final15")
               for p in (CORPUS / d).rglob("*")
               if p.suffix.lower() in (".step", ".stp", ".igs", ".iges", ".stl", ".obj", ".vtp")}
    assert on_disk - set(TRUTH) == set()
