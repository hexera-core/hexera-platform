# Responsibility: Write the chtMultiRegionFoam case around a delivered conjugate mesh - every region, every patch, the coupled interface - with the physics left blank.
# Boundaries: wiring only. It decides no velocity, no temperature and no material; it reads the delivered mesh's own patch names and nothing else.
# Collaborates with: cfmesh_conjugate/case_scaffold.py (the system/ dictionaries), conjugate.py (FLUID_REGION, the interface names), finalize.py (which records what was written).
"""THE CASE AROUND THE MESH, with the numbers that are not ours left out.

WHAT IS OURS AND WHAT IS THEIRS. A mesh is not a case: chtMultiRegionFoam will not start without
a field file per region naming every patch, a thermophysical model per region, and the coupled
condition on the interface. All of that is structure, and structure is what the mesh determines -
which regions exist, what the patches are called, which patch is the interface, which are the
ports. None of it is a physical choice.

The physical choices are not ours and are not guessed at: the flow rate, the inlet temperature,
the outlet pressure, the metal and the coolant. Those are the customer's to decide after they
have the mesh, and this writes `BLANK` in their place - a token that is not a NUMBER, so a
case nobody finished REFUSES TO RUN rather than running on a number someone invented. A plausible
default is the worse failure here: 1 m/s of water at 20 C will solve, publish a peak temperature,
and be wrong about the part.

WHY THE INTERFACE CONDITION IS OURS. `splitMeshRegions` creates the coupled `mappedWall` pair,
and the condition that belongs on it follows from that pair and the OpenFOAM in use - nothing a
user chooses. Getting it wrong is the commonest way a conjugate case runs and silently transfers
no heat, so it is written out in full.

DIALECT. v2412 (ESI) is what this deployment builds with:
`compressible::turbulentTemperatureRadCoupledMixed`. OpenFOAM 11 (Foundation) calls the same
condition `coupledTemperature`; both are written by `_coupled`, from the same seam
engines/snapgrid/thin_layers reads (see its header for where each is defined).
"""
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    FLUID_REGION,
    FLUID_TO_SOLID,
    SOLID_REGION,
    SOLID_TO_FLUID,
)

#: THE TOKEN THAT STOPS AN UNFINISHED CASE: it is not a NUMBER, so the first thing that reads it
#: as one stops on it instead of solving something plausible. The word says who has to fill it in.
#:
#: NOT "a token OpenFOAM cannot parse", which is what this said until it was checked. Every file
#: this module writes was put through `foamDictionary` in the mesh image on 2026-10-29 and all
#: twelve parse, blanks and all - a dictionary is a stream of tokens and this is a legal word in
#: one. What refuses it is the read: `Istream >> scalar` on a word is a FOAM FATAL ERROR, so the
#: case stops when a field is read rather than when it is parsed. Later, and still before anything
#: is solved, which is all the mechanism needs - but the two are not the same claim and only one
#: of them is true.
BLANK = "<<HEXERA_FILL_THIS_IN>>"

#: The two OpenFOAM families this writes for, and the name each gives the coupled condition.
ESI, FOUNDATION = "esi", "foundation"

_HEAD = ("FoamFile\n{{\n    version     2.0;\n    format      ascii;\n"
         "    class       {cls};\n    location    \"{loc}\";\n    object      {obj};\n}}\n")

#: WHICH PATCHES ARE NEVER AN INLET OR A WALL TO ASK ABOUT: the interface pair. They are the one
#: boundary this file fills in completely, so they are excluded from everything that gets a blank.
_INTERFACE = (FLUID_TO_SOLID, SOLID_TO_FLUID)


def _coupled(fluid: bool, dialect: str) -> list[str]:
    """The coupled-temperature condition for one side of the interface, in full.

    `kappaMethod` is the side's own: a fluid side reads its conductivity from the fluid thermo and
    a solid side from the solid thermo, and swapping them is a case that runs and transfers the
    wrong heat. `qrNbr`/`qr none` says there is no radiation field to couple - stating it is how a
    case without a radiation model starts at all.
    """
    if dialect == FOUNDATION:
        return ["        type            coupledTemperature;",
                "        Tnbr            T;",
                "        value           $internalField;"]
    return ["        type            compressible::turbulentTemperatureRadCoupledMixed;",
            "        Tnbr            T;",
            f"        kappaMethod     {'fluidThermo' if fluid else 'solidThermo'};",
            "        qrNbr           none;",
            "        qr              none;",
            "        value           $internalField;"]


def _field(region: str, name: str, dims: str, internal: str, entries: list[str],
           why: str = "") -> str:
    """One field file. `why` is a trailing comment on the internalField line, AFTER the
    semicolon: put it before and the dictionary reads `uniform <<...>> // text;`, which moves
    the terminator into the comment and leaves the entry with no end at all."""
    body = "\n".join(entries)
    note = f"   // {why}" if why else ""
    return (_HEAD.format(cls="volScalarField" if dims.count(" ") == 6 else "volVectorField",
                         loc=f"0/{region}", obj=name)
            + f"\ndimensions      {dims};\n\ninternalField   uniform {internal};{note}\n\n"
              f"boundaryField\n{{\n{body}\n}}\n")


def _entry(patch: str, lines: list[str]) -> str:
    return f"    {patch}\n    {{\n" + "\n".join(lines) + "\n    }"


def _roles(patches: dict, ports: dict | None
           ) -> tuple[list[str], list[str], list[str], list[str]]:
    """One region's patches split four ways: the interface, the inlets, the outlets, the rest.

    THE PORTS ARE NAMED BY THE PLAN, not guessed from a patch name. A face called `coolant_in` is
    an inlet because the port plan says so; one called `xmax` can be either, and a cut port wears
    whatever the customer called it.

    AN OUTLET IS NOT AN INLET, and the first version of this treated them as one "port": both ends
    came out with a fixed inlet temperature and a prescribed flow rate, which is a case with no
    way out and two conflicting flow rates. Where each condition goes is not symmetric - the
    velocity is prescribed at one end and the pressure at the other - so they are told apart here
    rather than at four separate call sites.
    """
    inlet = str((ports or {}).get("inlet") or "")
    outlet = str((ports or {}).get("outlet") or "")
    iface = [p for p in patches if p in _INTERFACE]
    ins = [p for p in patches if p == inlet and p not in _INTERFACE]
    outs = [p for p in patches if p == outlet and p not in _INTERFACE]
    rest = [p for p in patches if p not in (inlet, outlet) and p not in _INTERFACE]
    return sorted(iface), sorted(ins), sorted(outs), sorted(rest)


def write_fluid_fields(case: Path, patches: dict, ports: dict | None,
                       dialect: str = ESI) -> list[str]:
    """`0/fluid/{T,U,p,p_rgh}`: every patch named, the interface complete, the physics blank.

    WHICH CONDITION GOES WHERE IS STRUCTURE, and it is the part routinely got wrong: the
    temperature is fixed where the coolant comes IN and carried out where it leaves, the velocity
    is prescribed at the inlet, and the pressure is fixed at the OUTLET. One port has to fix the
    pressure or the matrix is singular, which is the failure conjugate.py's own header records -
    the first conjugate solve run here died inside the preconditioner for exactly that reason
    while the energy equation solved happily.
    """
    iface, ins, outs, rest = _roles(patches, ports)
    out = []

    # T
    bits = [_entry(p, _coupled(True, dialect)) for p in iface]
    bits += [_entry(p, ["        type            fixedValue;",
                        f"        value           uniform {BLANK};   // inlet temperature, K"])
             for p in ins]
    # inletOutlet, not zeroGradient: on the one face where the flow can reverse, zeroGradient
    # carries whatever is outside the domain back in at the cell's own temperature.
    bits += [_entry(p, ["        type            inletOutlet;",
                        f"        inletValue      uniform {BLANK};   // only read if the flow "
                        "reverses here, K",
                        "        value           $internalField;"]) for p in outs]
    bits += [_entry(p, ["        type            zeroGradient;"]) for p in rest]
    (case / "0" / FLUID_REGION / "T").write_text(
        _field(FLUID_REGION, "T", "[0 0 0 1 0 0 0]", BLANK, bits,
               "initial coolant temperature, K"), encoding="utf-8")
    out.append(f"0/{FLUID_REGION}/T")

    # U - no slip on everything that is not a port, the interface included
    bits = [_entry(p, ["        type            noSlip;"]) for p in (*iface, *rest)]
    bits += [_entry(p, ["        type            flowRateInletVelocity;",
                        f"        volumetricFlowRate {BLANK};   // m3/s into this port",
                        "        value           uniform (0 0 0);"]) for p in ins]
    bits += [_entry(p, ["        type            inletOutlet;",
                        "        inletValue      uniform (0 0 0);",
                        "        value           $internalField;"]) for p in outs]
    (case / "0" / FLUID_REGION / "U").write_text(
        _field(FLUID_REGION, "U", "[0 1 -1 0 0 0 0]", "(0 0 0)", bits), encoding="utf-8")
    out.append(f"0/{FLUID_REGION}/U")

    # p_rgh, and p beside it
    for name in ("p_rgh", "p"):
        bits = [_entry(p, ["        type            fixedFluxPressure;",
                           "        value           $internalField;"])
                for p in (*iface, *rest, *ins)]
        if name == "p_rgh":
            bits += [_entry(p, ["        type            prghPressure;",
                                f"        p               uniform {BLANK};   // outlet "
                                "pressure, Pa",
                                "        value           $internalField;"]) for p in outs]
        else:
            bits += [_entry(p, ["        type            fixedValue;",
                                f"        value           uniform {BLANK};   // outlet "
                                "pressure, Pa"]) for p in outs]
        (case / "0" / FLUID_REGION / name).write_text(
            _field(FLUID_REGION, name, "[1 -1 -2 0 0 0 0]", BLANK, bits,
                   "reference pressure, Pa"), encoding="utf-8")
        out.append(f"0/{FLUID_REGION}/{name}")
    return out


def write_solid_fields(case: Path, patches: dict, dialect: str = ESI) -> list[str]:
    """`0/solid/T`: the interface complete, and every outer face a question.

    EVERY OUTER FACE IS LEFT BLANK ON PURPOSE. Where the heat comes from is the whole physical
    question on the metal side - a chip's footprint, a heater, a convecting outer skin - and there
    is no defensible default. `zeroGradient` everywhere is a defensible default and it is also an
    adiabatic plate, which solves to a flat temperature field and looks like an answer.
    """
    iface, _ins, _outs, rest = _roles(patches, None)
    bits = [_entry(p, _coupled(False, dialect)) for p in iface]
    bits += [_entry(p, ["        type            zeroGradient;   // " + BLANK
                        + ": the heat load, or a convecting skin, goes on the faces that have one"])
             for p in rest]
    (case / "0" / SOLID_REGION / "T").write_text(
        _field(SOLID_REGION, "T", "[0 0 0 1 0 0 0]", BLANK, bits,
               "initial metal temperature, K"), encoding="utf-8")
    return [f"0/{SOLID_REGION}/T"]


def write_materials(case: Path) -> list[str]:
    """`constant/<region>/thermophysicalProperties` for both regions, with the numbers blank.

    The MODEL is structure - a fluid region needs a fluid thermo and a solid region a solid one,
    and the entry names are OpenFOAM's - so the shape is written out. Which coolant and which
    metal is the customer's, and so is every coefficient under it.
    """
    out = []
    (case / "constant" / FLUID_REGION / "thermophysicalProperties").write_text(
        _HEAD.format(cls="dictionary", loc=f"constant/{FLUID_REGION}",
                     obj="thermophysicalProperties")
        + "\nthermoType\n{\n    type            heRhoThermo;\n    mixture         pureMixture;\n"
          "    transport       const;\n    thermo          hConst;\n    equationOfState rhoConst;\n"
          "    specie          specie;\n    energy          sensibleEnthalpy;\n}\n\n"
          "mixture\n{\n    specie      { molWeight  " + BLANK + "; }   // g/mol\n"
          "    equationOfState { rho  " + BLANK + "; }   // kg/m3\n"
          "    thermodynamics  { Hf   0; Cp  " + BLANK + "; }   // J/kg/K\n"
          "    transport       { mu   " + BLANK + "; Pr  " + BLANK + "; }   // Pa s, -\n}\n\n"
          "// The coolant. Water at the temperature you run at, a glycol mix, a dielectric - this\n"
          "// is yours to state; the mesh does not depend on it and neither does anything Hexera\n"
          "// measured about it.\n", encoding="utf-8")
    out.append(f"constant/{FLUID_REGION}/thermophysicalProperties")

    (case / "constant" / SOLID_REGION / "thermophysicalProperties").write_text(
        _HEAD.format(cls="dictionary", loc=f"constant/{SOLID_REGION}",
                     obj="thermophysicalProperties")
        + "\nthermoType\n{\n    type            heSolidThermo;\n    mixture         pureMixture;\n"
          "    transport       constIso;\n    thermo          hConst;\n"
          "    equationOfState rhoConst;\n    specie          specie;\n"
          "    energy          sensibleEnthalpy;\n}\n\n"
          "mixture\n{\n    specie      { molWeight  " + BLANK + "; }   // g/mol\n"
          "    equationOfState { rho  " + BLANK + "; }   // kg/m3\n"
          "    thermodynamics  { Hf   0; Cp  " + BLANK + "; }   // J/kg/K\n"
          "    transport       { kappa " + BLANK + "; }   // W/m/K\n}\n\n"
          "// The metal. constIso is one conductivity in every direction; a bonded stack or a\n"
          "// directional material wants constAnIso and a tensor instead.\n", encoding="utf-8")
    out.append(f"constant/{SOLID_REGION}/thermophysicalProperties")

    (case / "constant" / "g").write_text(
        _HEAD.format(cls="uniformDimensionedVectorField", loc="constant", obj="g")
        + "\ndimensions      [0 1 -2 0 0 0 0];\nvalue           (0 0 -9.81);\n\n"
          "// WHICH WAY IS DOWN IN THE FILE, not a physical choice: the mesh is in the CAD's own\n"
          "// axes and -z is what the stage took as down. Turn it if the part stands another way.\n",
        encoding="utf-8")
    out.append("constant/g")
    return out


def write_region_system(case: Path) -> list[str]:
    """`system/<region>/{fvSchemes,fvSolution}` - one per region, which cht needs and the mesher's
    own scaffold does not write (its pair is for the meshing utilities at the case root)."""
    out = []
    for region in (FLUID_REGION, SOLID_REGION):
        d = case / "system" / region
        d.mkdir(parents=True, exist_ok=True)
        (d / "fvSchemes").write_text(
            _HEAD.format(cls="dictionary", loc=f"system/{region}", obj="fvSchemes")
            + "\nddtSchemes      { default Euler; }\n"
              "gradSchemes     { default Gauss linear; }\n"
              "divSchemes      { default none; div(phi,U) bounded Gauss limitedLinearV 1;\n"
              "                  div(phi,K) bounded Gauss limitedLinear 1;\n"
              "                  div(phi,h) bounded Gauss limitedLinear 1;\n"
              "                  div(phi,e) bounded Gauss limitedLinear 1;\n"
              "                  div(phi,Ekp) bounded Gauss limitedLinear 1;\n"
              "                  div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear; }\n"
              "laplacianSchemes { default Gauss linear corrected; }\n"
              "interpolationSchemes { default linear; }\n"
              "snGradSchemes   { default corrected; }\n", encoding="utf-8")
        (d / "fvSolution").write_text(
            _HEAD.format(cls="dictionary", loc=f"system/{region}", obj="fvSolution")
            + "\nsolvers\n{\n    \"(rho|U|h|e|k|epsilon|omega)\"\n    {\n"
              "        solver          PBiCGStab;\n        preconditioner  DILU;\n"
              "        tolerance       1e-7;\n        relTol          0.01;\n    }\n\n"
              "    \"(p_rgh|T)\"\n    {\n        solver          GAMG;\n"
              "        tolerance       1e-7;\n        relTol          0.01;\n"
              "        smoother        GaussSeidel;\n    }\n}\n\n"
              "PIMPLE { momentumPredictor no; nCorrectors 2; nNonOrthogonalCorrectors 0; }\n\n"
              "relaxationFactors { equations { \".*\" 1; } }\n", encoding="utf-8")
        out += [f"system/{region}/fvSchemes", f"system/{region}/fvSolution"]
    return out


def write_solver_case(workspace, *, fluid_patches: dict, solid_patches: dict,
                      ports: dict | None = None, dialect: str = ESI) -> list[str]:
    """The whole case around a delivered mesh. Returns what it wrote, relative to the case.

    `fluid_patches` and `solid_patches` are the DELIVERED patch names and their face counts, as
    the report carries them - read off the mesh rather than off the plan, so a patch that the
    split renamed or a port that was cut appears here under the name a solver will look for.
    """
    case = Path(workspace)
    for region in (FLUID_REGION, SOLID_REGION):
        (case / "0" / region).mkdir(parents=True, exist_ok=True)
        (case / "constant" / region).mkdir(parents=True, exist_ok=True)
    written = write_fluid_fields(case, fluid_patches, ports, dialect)
    written += write_solid_fields(case, solid_patches, dialect)
    written += write_materials(case)
    written += write_region_system(case)
    (case / "0" / "README.txt").write_text(
        "This is the chtMultiRegionFoam case around the mesh Hexera delivered.\n\n"
        "The structure is complete: both regions, every patch named as the mesh names it, and the\n"
        f"coupled condition on the {FLUID_TO_SOLID}/{SOLID_TO_FLUID} pair written out in full.\n\n"
        f"Every {BLANK} is a number we did not choose for you - the flow rate, the inlet\n"
        "temperature, the outlet pressure, the coolant and the metal. They are not defaulted,\n"
        "because a case that runs on an invented flow rate publishes a peak temperature that is\n"
        "wrong about your part. The solver will stop on the first one it reads.\n", encoding="utf-8")
    written.append("0/README.txt")
    return written


__all__ = ["BLANK", "ESI", "FOUNDATION", "write_solver_case", "write_fluid_fields",
           "write_solid_fields", "write_materials", "write_region_system"]
