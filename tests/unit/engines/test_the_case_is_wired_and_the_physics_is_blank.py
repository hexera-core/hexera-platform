"""The chtMultiRegionFoam case around a delivered conjugate mesh: wiring ours, numbers theirs.

WHY THIS EXISTS. Rehaan, 2026-10-29: "lets still give them a case cause why not so include that
too but yea none fo the bstuff like how fast water comes in etc matter foir us cause thats smth
they will decide after we provide the mesh".

So the split is explicit and this file holds it to both halves.

OURS, because the mesh determines it and no user chooses it: which regions exist, what every
patch is called, which patch is the interface, which are the ports, and the coupled condition on
the interface pair - which `splitMeshRegions` creates, and which is the commonest way a conjugate
case comes to run and silently transfer no heat.

THEIRS, and never guessed at: the flow rate, the inlet temperature, the outlet pressure, the
coolant and the metal. Those are written as a token that is not a NUMBER, so an unfinished case
REFUSES TO RUN. A plausible default is the worse failure: 1 m/s of water at 20 C solves, publishes
a peak temperature, and is wrong about the part.
"""
import inspect
import re

import pytest

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    FLUID_REGION,
    FLUID_TO_SOLID,
    SOLID_REGION,
    SOLID_TO_FLUID,
)
from meshpipeline.engines.cfmesh_conjugate.solver_case import (
    BLANK,
    ESI,
    FOUNDATION,
    write_solver_case,
)

#: The delivered plate, as its report carries it (the ported two-scale plate, 2026-10-29).
FLUID = {FLUID_TO_SOLID: 27_250, "coolant_in": 36, "coolant_out": 36}
SOLID = {SOLID_TO_FLUID: 27_250, "xmin": 1_312, "xmax": 580, "ymin": 1_298,
         "ymax": 1_298, "zmin": 9_968, "zmax": 9_896}
PORTS = {"inlet": "coolant_in", "outlet": "coolant_out"}


@pytest.fixture
def case(tmp_path):
    write_solver_case(tmp_path, fluid_patches=FLUID, solid_patches=SOLID, ports=PORTS)
    return tmp_path


def _read(case, rel):
    return (case / rel).read_text(encoding="utf-8")


#   -- every patch is named, which is what makes the case start at all -------------------------

@pytest.mark.parametrize("field", ["T", "U", "p", "p_rgh"])
def test_every_fluid_patch_appears_in_every_fluid_field(case, field):
    """chtMultiRegionFoam refuses a field file that leaves one patch out, and the patch it leaves
    out is whichever one the mesh gained last - a cut port, a renamed face."""
    text = _read(case, f"0/{FLUID_REGION}/{field}")
    for patch in FLUID:
        assert f"    {patch}\n" in text, patch


def test_every_solid_patch_appears_in_the_solid_field(case):
    text = _read(case, f"0/{SOLID_REGION}/T")
    for patch in SOLID:
        assert f"    {patch}\n" in text, patch


#   -- the interface, which is the part that is ours ------------------------------------------

def test_the_interface_carries_the_coupled_condition_on_both_sides(case):
    f = _read(case, f"0/{FLUID_REGION}/T")
    s = _read(case, f"0/{SOLID_REGION}/T")
    for text in (f, s):
        assert "compressible::turbulentTemperatureRadCoupledMixed" in text
        assert "Tnbr            T;" in text


def test_each_side_reads_its_own_conductivity(case):
    """A fluid side reads the fluid thermo and a solid side the solid thermo. Swapping them is a
    case that runs and transfers the wrong heat, which nothing downstream would catch."""
    assert "kappaMethod     fluidThermo;" in _read(case, f"0/{FLUID_REGION}/T")
    assert "kappaMethod     solidThermo;" in _read(case, f"0/{SOLID_REGION}/T")


def test_radiation_is_said_to_be_absent_rather_than_left_out(case):
    """`qr none` is how a case with no radiation model starts; omitting it is a missing entry."""
    text = _read(case, f"0/{FLUID_REGION}/T")
    assert "qrNbr           none;" in text and "qr              none;" in text


def test_the_other_dialect_is_the_other_name_for_the_same_condition(tmp_path):
    """OpenFOAM 11 (Foundation) calls it coupledTemperature; the same seam
    engines/snapgrid/thin_layers reads has where each is defined."""
    write_solver_case(tmp_path, fluid_patches=FLUID, solid_patches=SOLID, ports=PORTS,
                      dialect=FOUNDATION)
    text = _read(tmp_path, f"0/{FLUID_REGION}/T")
    assert "coupledTemperature" in text
    assert "turbulentTemperatureRadCoupledMixed" not in text


def test_the_interface_is_a_wall_to_the_flow(case):
    """It is metal. A fluid/solid interface that is not no-slip is a free-slip channel."""
    u = _read(case, f"0/{FLUID_REGION}/U")
    block = u[u.index(f"    {FLUID_TO_SOLID}\n"):]
    assert "noSlip" in block[:200]


#   -- an outlet is not an inlet ---------------------------------------------------------------

def test_the_inlet_takes_the_flow_and_the_outlet_does_not(case):
    u = _read(case, f"0/{FLUID_REGION}/U")
    assert "flowRateInletVelocity" in u
    assert u.count("flowRateInletVelocity") == 1, "only the inlet has a prescribed flow"
    out = u[u.index("    coolant_out\n"):]
    assert "inletOutlet" in out[:200]
    assert "flowRateInletVelocity" not in out


def test_the_outlet_fixes_the_pressure_and_the_inlet_does_not(case):
    """One port has to fix the pressure or the matrix is singular - conjugate.py's own header:
    "the first conjugate solve run here died inside the preconditioner for exactly that reason
    while the energy equation solved happily"."""
    p = _read(case, f"0/{FLUID_REGION}/p_rgh")
    out = p[p.index("    coolant_out\n"):]
    assert "prghPressure" in out[:200]
    inlet = p[p.index("    coolant_in\n"):p.index("    coolant_out\n")]
    assert "fixedFluxPressure" in inlet


def test_the_inlet_temperature_is_fixed_and_the_outlet_is_carried_out(case):
    t = _read(case, f"0/{FLUID_REGION}/T")
    inlet = t[t.index("    coolant_in\n"):t.index("    coolant_out\n")]
    assert "fixedValue" in inlet
    out = t[t.index("    coolant_out\n"):]
    assert "inletOutlet" in out[:300], (
        "zeroGradient on the one face where flow can reverse carries the outside back in at the "
        "cell's own temperature")


#   -- the physics is blank, and blank means it will not run ------------------------------------

@pytest.mark.parametrize("rel", [
    f"0/{FLUID_REGION}/T", f"0/{FLUID_REGION}/U", f"0/{FLUID_REGION}/p_rgh",
    f"0/{SOLID_REGION}/T",
    f"constant/{FLUID_REGION}/thermophysicalProperties",
    f"constant/{SOLID_REGION}/thermophysicalProperties",
])
def test_nothing_physical_was_chosen_for_them(case, rel):
    assert BLANK in _read(case, rel), rel


def test_the_token_is_not_a_number(case):
    """THE MECHANISM, stated as what it is. Every file this writes parses under `foamDictionary`
    in the mesh image, blanks and all - a dictionary is a stream of tokens and this is a legal
    word in one. What refuses it is reading it AS a number, which happens when a field is read and
    still before anything is solved. "A token OpenFOAM cannot parse" was the first wording and it
    was wrong; it was checked on 2026-10-29 and all twelve files parsed."""
    assert BLANK.startswith("<<") and BLANK.endswith(">>")
    assert not BLANK.strip("<>").replace("_", "").isdigit()


def test_the_blank_does_not_swallow_the_terminator(case):
    """`internalField uniform <<...>> // initial temperature;` moves the semicolon into the
    comment and the entry then has no end at all. The comment goes after it."""
    for rel in (f"0/{FLUID_REGION}/T", f"0/{SOLID_REGION}/T", f"0/{FLUID_REGION}/p_rgh"):
        line = next(ln for ln in _read(case, rel).split("\n") if ln.startswith("internalField"))
        assert line.index(";") < line.index("//"), line
        assert line.rstrip().endswith(("K", "Pa")), line


def test_the_materials_name_the_model_but_not_the_material(case):
    """The model is structure - a fluid region needs a fluid thermo - and which coolant is not.

    Read off the ENTRIES rather than the prose, and off whole words rather than substrings. Two
    earlier versions of this got it wrong in both directions: one grepped the file for "water" and
    failed on the sentence explaining that the coolant is theirs to state, and the next matched
    `rho` inside `rhoConst`, which is the equation of state and not a density.
    """
    f = _read(case, f"constant/{FLUID_REGION}/thermophysicalProperties")
    s = _read(case, f"constant/{SOLID_REGION}/thermophysicalProperties")
    assert "heRhoThermo" in f and "heSolidThermo" in s
    assert "kappa" in s, "a solid needs a conductivity entry even with no number in it"
    for text, keys in ((f, ("molWeight", "rho", "Cp", "mu", "Pr")),
                       (s, ("molWeight", "rho", "Cp", "kappa"))):
        mixture = text[text.index("mixture"):]
        for key in keys:
            m = re.search(rf"\b{key}\s+(\S+?);", mixture)
            assert m, f"{key} has no entry at all"
            assert m.group(1) == BLANK, f"{key} was given the value {m.group(1)!r}"


def test_the_readme_says_who_fills_what_in(case):
    r = _read(case, "0/README.txt")
    assert BLANK in r
    assert "will stop" in r


#   -- what it needs to be a case at all --------------------------------------------------------

def test_every_region_has_its_own_schemes_and_solution(case):
    """cht reads them per region; the mesher's own scaffold writes the pair at the case root for
    the meshing utilities, which is a different pair."""
    for region in (FLUID_REGION, SOLID_REGION):
        assert (case / "system" / region / "fvSchemes").is_file()
        assert (case / "system" / region / "fvSolution").is_file()


def test_gravity_is_the_file_s_own_axes_and_says_so(case):
    g = _read(case, "constant/g")
    assert "(0 0 -9.81)" in g
    assert "-z" in g, "which way is down in the FILE is not a physical choice, and it is turnable"


def test_a_part_with_no_ports_still_gets_a_case(tmp_path):
    """Eight of the nine corpus parts take their ports from renamed block faces, and a part whose
    ports could not be found at all still has a mesh worth a case."""
    files = write_solver_case(tmp_path, fluid_patches={FLUID_TO_SOLID: 10, "ymin": 4, "ymax": 4},
                              solid_patches={SOLID_TO_FLUID: 10, "xmin": 2}, ports=None)
    assert f"0/{FLUID_REGION}/T" in files
    t = _read(tmp_path, f"0/{FLUID_REGION}/T")
    assert "    ymin\n" in t and "    ymax\n" in t
    assert "fixedValue" not in t, "with no port named, no face is an inlet"


#   -- the runner writes it ---------------------------------------------------------------------

def test_the_runner_writes_the_case_beside_the_mesh():
    """THE GAP THIS PACKAGE HAS HAD TWICE: a renderer tested through its own front door cannot
    catch a caller that never calls it."""
    from meshpipeline.engines.cfmesh_conjugate import native

    src = inspect.getsource(native._run_conjugate_local)
    assert "write_solver_case" in src
    assert "fluid_patches=" in src and "solid_patches=" in src
    assert "report.fluid.patches" in src, "the DELIVERED names, not the plan's"


def test_the_case_never_fails_the_run():
    """A mesh is never lost over the case written beside it."""
    from meshpipeline.engines.cfmesh_conjugate import native

    src = inspect.getsource(native._run_conjugate_local)
    block = src[src.index("write_solver_case"):]
    assert "except Exception" in src[:src.index("write_solver_case") + 2000]
    assert "non-fatal" in block[:1200] or "non-fatal" in src
