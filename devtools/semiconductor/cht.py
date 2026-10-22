# Responsibility: Solve a built conjugate mesh and say whether the mesh held up, from outside.
# Boundaries: a measuring instrument for meshes. Nothing here is plumbed into the platform.
"""RUN A CONJUGATE SOLVE ON A FINISHED MESH, TO JUDGE THE MESH.

WHY A SOLVER AT ALL, WHEN THE PLATFORM DELIVERS MESHES. Because every geometric check a mesh can be
given passes on meshes that cannot be solved. We have the receipts: a coolant region in 15
disconnected islands passed every bar `checkMesh` has and died in the preconditioner, and a sealed
coolant region passed them too and died the same way. Conformal face counts, volume within a
fraction of a per cent, "Mesh OK" on both regions - all true, all of it, on a mesh no solver would
touch.

So this is an INSTRUMENT, not a feature. It lives in devtools and nothing in `src/` imports it. It
exists to answer one question - did the mesh hold up - and the answer is only worth having because
it is independent of everything the mesher's own checks assert.

WHAT MAKES IT A TEST RATHER THAN A DEMO. Heat is put in at a known rate: a fixed flux q on a named
face, so Q_in is q times that patch's area and nothing else. At steady state the same watts have to
leave through the coolant, and three independent readings of that number have to agree:

    Q_in            q x area of the heated face        from the boundary condition
    Q_solid         integrated flux, solid side        from the solid's interface patch
    Q_fluid         integrated flux, fluid side        from the fluid's interface patch
    Q_coolant       mdot x Cp x (T_out - T_in)         from the mass flux and bulk temperatures

Q_solid against Q_fluid tests the COUPLING: a conformal interface that is not actually transferring
what it says it is shows up as a gap here and nowhere else. Q_in against Q_solid tests whether the
solid conserves energy on this mesh. Q_coolant against Q_in tests the whole chain and is the one
that moves when the staircase has changed the wetted area, because heat crosses area.

Everything is read from the .dat files OpenFOAM's function objects write, never scraped from the
console log, because the log's wording is version-dependent and the .dat columns are not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: The two region names the conjugate build produces. Imported rather than retyped would be
#: better, but this module is deliberately standalone: it must not drag `src/` onto the path.
FLUID = "fluid"
SOLID = "solid"
FLUID_TO_SOLID = "fluid_to_solid"
SOLID_TO_FLUID = "solid_to_fluid"


class SolveError(RuntimeError):
    """A solve that could not be set up or could not be read."""


# #
# the physics, declared once
# #
@dataclass(frozen=True)
class Coolant:
    """Water with 25% propylene glycol near 300 K, which is what a cold plate actually runs.

    `const` transport in OpenFOAM takes mu and Pr and works kappa out as Cp*mu/Pr, so Pr is
    derived here from the conductivity rather than quoted, and the conductivity is the number
    anyone would want to check.
    """

    name: str = "water + 25% propylene glycol"
    rho: float = 1030.0          # kg/m3
    cp: float = 3600.0           # J/kg/K
    mu: float = 2.5e-3           # Pa s
    kappa: float = 0.42          # W/m/K
    mol_weight: float = 18.0     # g/mol, carried only because the thermo model asks for it

    @property
    def prandtl(self) -> float:
        return self.cp * self.mu / self.kappa

    def reynolds(self, velocity: float, hydraulic_diameter_m: float) -> float:
        return self.rho * velocity * hydraulic_diameter_m / self.mu


@dataclass(frozen=True)
class Metal:
    """Silicon, as the die and the channel walls."""

    name: str = "silicon"
    rho: float = 2329.0
    cp: float = 705.0
    kappa: float = 148.0
    mol_weight: float = 28.09


@dataclass(frozen=True)
class SolveSpec:
    """Everything a run needs beyond the mesh itself."""

    inlet: str
    outlet: str
    #: The face heat is put in through. Chip-side by convention, and reported either way so that a
    #: part built the other way up is noticed rather than silently cooled from the wrong side.
    heated_face: str = "zmin"
    inlet_velocity: float = 1.0          # m/s
    heat_flux: float = 1.0e6             # W/m2, which is 100 W/cm2
    inlet_temperature: float = 300.0     # K
    iterations: int = 200
    coolant: Coolant = field(default_factory=Coolant)
    metal: Metal = field(default_factory=Metal)
    #: How far the four readings of the heat may disagree before the mesh is not trusted. A steady
    #: SIMPLE run stopped at a fixed iteration count is never exactly converged, so some gap is the
    #: solver's and not the mesh's; 5% is loose enough not to blame the mesh for that and tight
    #: enough that a staircased wetted area shows.
    heat_tol: float = 0.05
    #: Ranks to solve on.
    #:
    #: THE SOLVER SCALES AND THE MESHER DOES NOT, and keeping the two straight matters. cfMesh was
    #: measured here saturating around four cores at 2.5x, with MPI buying nothing at all and
    #: changing the mesh when it was used. None of that applies to a CHT solve: it is a
    #: domain-decomposed linear solve, which is what MPI is for. Serial, one iteration of a 1.2 M
    #: cell conjugate case took 4.6 s, so a converged answer was two hours away and no sweep was
    #: ever going to happen.
    cores: int = 8


# #
# writing the case
# #
def _head(cls: str, obj: str, location: str | None = None) -> str:
    loc = f'    location    "{location}";\n' if location else ""
    return ("FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
            f"    class       {cls};\n{loc}    object      {obj};\n}}\n\n")


_AXIS = {"xmin": (0, -1), "xmax": (0, 1), "ymin": (1, -1), "ymax": (1, 1),
         "zmin": (2, -1), "zmax": (2, 1)}


def inlet_vector(face: str, speed: float) -> tuple[float, float, float]:
    """The inlet velocity, pointing INTO the part.

    Signs here are the one thing that is wrong silently: a velocity pointing out of the inlet gives
    a run that converges beautifully to coolant flowing backwards, and the temperature rise comes
    out plausible because the energy equation does not care which way round the ends are.
    """
    if face not in _AXIS:
        raise SolveError(f"{face!r} is not a block face")
    axis, sign = _AXIS[face]
    v = [0.0, 0.0, 0.0]
    v[axis] = -sign * abs(speed)
    return tuple(v)  # type: ignore[return-value]


def _boundary_field(entries: dict[str, str]) -> str:
    out = ["boundaryField\n{"]
    for patch, body in entries.items():
        out.append(f"    {patch}\n    {{\n{body}    }}")
    out.append("}\n")
    return "\n".join(out)


def fluid_T(spec: SolveSpec, patches: list[str]) -> str:
    coupled = ("        type            compressible::turbulentTemperatureRadCoupledMixed;\n"
               "        Tnbr            T;\n"
               "        kappaMethod     fluidThermo;\n"
               f"        value           uniform {spec.inlet_temperature};\n")
    entries: dict[str, str] = {}
    for p in patches:
        if p == spec.inlet:
            entries[p] = f"        type            fixedValue;\n        value           uniform {spec.inlet_temperature};\n"
        elif p == spec.outlet:
            entries[p] = (f"        type            inletOutlet;\n"
                          f"        inletValue      uniform {spec.inlet_temperature};\n"
                          f"        value           uniform {spec.inlet_temperature};\n")
        elif p == FLUID_TO_SOLID:
            entries[p] = coupled
        else:
            entries[p] = "        type            zeroGradient;\n"
    return (_head("volScalarField", "T", "0/fluid")
            + "dimensions      [0 0 0 1 0 0 0];\n"
            + f"internalField   uniform {spec.inlet_temperature};\n\n"
            + _boundary_field(entries))


def fluid_U(spec: SolveSpec, patches: list[str]) -> str:
    vx, vy, vz = inlet_vector(spec.inlet, spec.inlet_velocity)
    entries: dict[str, str] = {}
    for p in patches:
        if p == spec.inlet:
            entries[p] = ("        type            fixedValue;\n"
                          f"        value           uniform ({vx} {vy} {vz});\n")
        elif p == spec.outlet:
            entries[p] = ("        type            pressureInletOutletVelocity;\n"
                          "        value           uniform (0 0 0);\n")
        else:
            entries[p] = "        type            noSlip;\n"
    return (_head("volVectorField", "U", "0/fluid")
            + "dimensions      [0 1 -1 0 0 0 0];\n"
            + "internalField   uniform (0 0 0);\n\n"
            + _boundary_field(entries))


def fluid_p_rgh(spec: SolveSpec, patches: list[str]) -> str:
    entries: dict[str, str] = {}
    for p in patches:
        if p == spec.outlet:
            # The ONLY place pressure is pinned. Without it nothing fixes the level, the matrix is
            # singular and the run dies in the preconditioner - which is exactly how a sealed
            # coolant region fails, and why the mesher now refuses one.
            entries[p] = ("        type            fixedValue;\n"
                          "        value           uniform 0;\n")
        else:
            entries[p] = ("        type            fixedFluxPressure;\n"
                          "        value           uniform 0;\n")
    return (_head("volScalarField", "p_rgh", "0/fluid")
            + "dimensions      [1 -1 -2 0 0 0 0];\n"
            + "internalField   uniform 0;\n\n"
            + _boundary_field(entries))


def _uniform_pressure(location: str, patches: list[str]) -> str:
    """A `p` field that is uniform and calculated everywhere.

    Both regions need one and neither uses it directly: the fluid's is derived from p_rgh, and
    nothing in the metal varies with pressure at all. But `heRhoThermo` and `heSolidThermo` both
    look enthalpy and density up against pressure like any other thermo, so the field has to be
    there. A missing `0/solid/p` is the first error a hand-built CHT case gives you.
    """
    entries = {p: "        type            calculated;\n        value           uniform 1e5;\n"
               for p in patches}
    return (_head("volScalarField", "p", location)
            + "dimensions      [1 -1 -2 0 0 0 0];\n"
            + "internalField   uniform 1e5;\n\n"
            + _boundary_field(entries))


def fluid_p(spec: SolveSpec, patches: list[str]) -> str:
    return _uniform_pressure("0/fluid", patches)


def solid_T(spec: SolveSpec, patches: list[str]) -> str:
    entries: dict[str, str] = {}
    for p in patches:
        if p == SOLID_TO_FLUID:
            entries[p] = ("        type            compressible::turbulentTemperatureRadCoupledMixed;\n"
                          "        Tnbr            T;\n"
                          "        kappaMethod     solidThermo;\n"
                          f"        value           uniform {spec.inlet_temperature};\n")
        elif p == spec.heated_face:
            # A FIXED FLUX, not a fixed temperature, and that choice is what makes this a test.
            # Q_in is then q times this patch's area and nothing else, so it can be compared with
            # what comes out. Hold the face at a temperature instead and Q_in becomes an output of
            # the mesh, which is the thing being measured: there is nothing left to check against.
            entries[p] = ("        type            externalWallHeatFluxTemperature;\n"
                          "        mode            flux;\n"
                          f"        q               uniform {spec.heat_flux};\n"
                          "        kappaMethod     solidThermo;\n"
                          f"        value           uniform {spec.inlet_temperature};\n")
        else:
            entries[p] = "        type            zeroGradient;\n"
    return (_head("volScalarField", "T", "0/solid")
            + "dimensions      [0 0 0 1 0 0 0];\n"
            + f"internalField   uniform {spec.inlet_temperature};\n\n"
            + _boundary_field(entries))


def solid_p(spec: SolveSpec, patches: list[str]) -> str:
    return _uniform_pressure("0/solid", patches)


def region_properties() -> str:
    return (_head("dictionary", "regionProperties", "constant")
            + f"regions\n(\n    fluid       ({FLUID})\n    solid       ({SOLID})\n);\n")


def gravity() -> str:
    """Zero. A microchannel at a metre a second is forced convection by four orders of magnitude,
    and zero gravity removes the buoyancy coupling between p_rgh and T along with its own failure
    modes. The solver still asks for the file."""
    return (_head("uniformDimensionedVectorField", "g", "constant")
            + "dimensions      [0 1 -2 0 0 0 0];\nvalue           (0 0 0);\n")


def fluid_thermo(c: Coolant) -> str:
    return (_head("dictionary", "thermophysicalProperties", "constant/fluid")
            + "thermoType\n{\n"
            "    type            heRhoThermo;\n    mixture         pureMixture;\n"
            "    transport       const;\n    thermo          hConst;\n"
            "    equationOfState rhoConst;\n    specie          specie;\n"
            "    energy          sensibleEnthalpy;\n}\n\n"
            "mixture\n{\n"
            f"    specie          {{ molWeight {c.mol_weight}; }}\n"
            f"    equationOfState {{ rho {c.rho}; }}\n"
            f"    thermodynamics  {{ Cp {c.cp}; Hf 0; }}\n"
            f"    transport       {{ mu {c.mu}; Pr {c.prandtl:.6g}; }}\n"
            "}\n")


def solid_thermo(m: Metal) -> str:
    return (_head("dictionary", "thermophysicalProperties", "constant/solid")
            + "thermoType\n{\n"
            "    type            heSolidThermo;\n    mixture         pureMixture;\n"
            "    transport       constIso;\n    thermo          hConst;\n"
            "    equationOfState rhoConst;\n    specie          specie;\n"
            "    energy          sensibleEnthalpy;\n}\n\n"
            "mixture\n{\n"
            f"    specie          {{ molWeight {m.mol_weight}; }}\n"
            f"    equationOfState {{ rho {m.rho}; }}\n"
            f"    thermodynamics  {{ Cp {m.cp}; Hf 0; }}\n"
            f"    transport       {{ kappa {m.kappa}; }}\n"
            "}\n")


def turbulence() -> str:
    """Laminar, and not as a simplification. Re comes out near 70 in a 100 um channel at a metre a
    second, two orders below transition, so a turbulence model here would be inventing physics and
    dragging in wall functions whose first-cell requirements have nothing to do with this mesh."""
    return (_head("dictionary", "turbulenceProperties", "constant/fluid")
            + "simulationType  laminar;\n")


def fv_schemes(region: str) -> str:
    """Upwind convection on purpose.

    The question is whether the mesh solves and conserves energy, not how little numerical
    diffusion can be achieved on it. Upwind converges on cell shapes a higher-order scheme would
    oscillate on, so a failure here is the mesh's and not the scheme's. The one explicit entry is
    the stress term, which has no convecting flux and needs plain interpolation.
    """
    body = ("ddtSchemes      { default steadyState; }\n\n"
            "gradSchemes     { default Gauss linear; }\n\n"
            "divSchemes\n{\n    default         bounded Gauss upwind;\n"
            "    div(((rho*nuEff)*dev2(T(grad(U))))) Gauss linear;\n}\n\n"
            "laplacianSchemes { default Gauss linear corrected; }\n\n"
            "interpolationSchemes { default linear; }\n\n"
            "snGradSchemes   { default corrected; }\n")
    return _head("dictionary", "fvSchemes", f"system/{region}") + body


#: What counts as converged, per equation, read off the initial residuals in the log.
#:
#: JUDGED HERE AND NOT BY THE SOLVER, because `chtMultiRegionSimpleFoam` does not take a
#: `residualControl`. It has no `simpleControl` in it at all - the binary has no such symbol - so
#: it runs to endTime whatever the residuals do. A `residualControl` block in its fvSolution is
#: read by nobody and refused by nobody, which is the worst of both: it looks like the run is
#: stopping when it is finished, and nothing is checking.
#:
#: So the iteration count is a CAP and convergence is a measurement taken afterwards. A field is
#: matched by the longest target name it starts with, which is how Ux, Uy and Uz are all held to
#: U's target.
RESIDUAL_TARGETS: dict[str, float] = {"p_rgh": 1.0e-5, "U": 1.0e-5, "h": 1.0e-6}
#: For an equation nobody named a target for.
RESIDUAL_DEFAULT = 1.0e-5


def residual_target(field: str) -> float:
    """The target for one field, by longest matching prefix."""
    hits = [k for k in RESIDUAL_TARGETS if field.startswith(k)]
    return RESIDUAL_TARGETS[max(hits, key=len)] if hits else RESIDUAL_DEFAULT


def fv_solution(region: str) -> str:
    if region == FLUID:
        body = ("solvers\n{\n"
                "    p_rgh\n    {\n        solver          GAMG;\n"
                "        tolerance       1e-9;\n        relTol          0.01;\n"
                "        smoother        GaussSeidel;\n    }\n\n"
                '    "(U|h|e)"\n    {\n        solver          PBiCGStab;\n'
                "        preconditioner  DILU;\n        tolerance       1e-10;\n"
                "        relTol          0.01;\n    }\n}\n\n"
                "SIMPLE\n{\n    momentumPredictor yes;\n"
                "    nNonOrthogonalCorrectors 0;\n"
                # No residualControl: this solver does not read one (see RESIDUAL_TARGETS).
                "}\n\n"
                "relaxationFactors\n{\n    fields    { p_rgh 0.7; }\n"
                # h at 0.7 rather than the 0.3 a tutorial would use. These channels are hundreds
                # of cells long, so the energy field has to be carried the length of the part;
                # at 0.3 that took thousands of iterations and the run was stopped before the
                # coolant had warmed up at all, which reads as a failed energy balance and is
                # nothing of the kind.
                "    equations { U 0.5; h 0.7; }\n}\n")
    else:
        body = ("solvers\n{\n"
                '    "(h|e)"\n    {\n        solver          PCG;\n'
                "        preconditioner  DIC;\n        tolerance       1e-10;\n"
                "        relTol          0.01;\n    }\n}\n\n"
                "SIMPLE\n{\n    nNonOrthogonalCorrectors 0;\n}\n\n"
                "relaxationFactors { equations { h 1.0; } }\n")
    return _head("dictionary", "fvSolution", f"system/{region}") + body


#: Function-object names, used both to write the dictionary and to find the .dat files afterwards.
FN_INLET = "inletFlow"
FN_OUTLET = "outletFlow"
FN_HEAT_FLUID = "heatFluid"
FN_HEAT_SOLID = "heatSolid"


def _surface_value(name: str, region: str, patch: str, operation: str,
                   fields: str, weight: str | None = None) -> str:
    w = f"        weightField     {weight};\n" if weight else ""
    return (f"    {name}\n    {{\n"
            "        type            surfaceFieldValue;\n"
            '        libs            ("libfieldFunctionObjects.so");\n'
            f"        region          {region};\n"
            "        writeControl    timeStep;\n        writeInterval   1;\n"
            "        log             false;\n        writeFields     false;\n"
            "        writeArea       true;\n"
            "        regionType      patch;\n"
            f"        name            {patch};\n"
            f"        operation       {operation};\n{w}"
            f"        fields          ({fields});\n"
            "    }\n")


def _wall_heat_flux(name: str, region: str, patches: tuple[str, ...]) -> str:
    """Watts through named patches, written ONCE at the end of the run.

    `writeControl onEnd`, not `timeStep`, because this function object writes a volume field
    beside its table and `writeFields false` does not stop it in 2412. On `timeStep` that is one
    time directory per iteration: a 131-iteration run left 131 of them holding nothing but this
    field, which is wasted disk and, when the container runs as root, 262 files the person who
    started the run cannot delete. Only the last row is ever read.
    """
    return (f"    {name}\n    {{\n"
            "        type            wallHeatFlux;\n"
            '        libs            ("libfieldFunctionObjects.so");\n'
            f"        region          {region};\n"
            "        writeControl    onEnd;\n"
            "        log             true;\n        writeFields     false;\n"
            f"        patches         ({' '.join(patches)});\n    }}\n")


#: Two things were tried before the run was simply stopped and restarted, and both are worth not
#: trying again.
#:
#: `residualControl` in the SIMPLE dict does nothing: this solver has no `simpleControl` in it, so
#: the block is read by nobody and refused by nobody.
#:
#: `runTimeControl` with an `equationInitialResidual` condition and `satisfiedAction end` DOES get
#: run, and ended the solve at iteration 3 with every residual still above 0.2. Whatever satisfies
#: that condition, it is not what the field name suggests, and a stop that fires immediately is
#: worse than no stop at all: the run comes back looking like a mesh that cannot be solved.
#:
#: So the run is chunked instead - see `solve_commands` - which uses nothing but `startFrom
#: latestTime` and is checked against the same residuals the verdict uses.
_WHAT_DOES_NOT_WORK = ("residualControl", "runTimeControl")


def control_dict(spec: SolveSpec, *, start: int = 0, end: int | None = None) -> str:
    fns = [
        # Mass in and mass out, and both patch areas. The areas are the point as much as the
        # flows: a staircased port has a smaller area than the CAD's and that shows up here.
        _surface_value(FN_INLET, FLUID, spec.inlet, "sum", "phi"),
        _surface_value(FN_OUTLET, FLUID, spec.outlet, "sum", "phi"),
        # Bulk temperature, weighted by mass flux rather than by area. A channel's area-average
        # temperature is not its bulk temperature - the slow fluid at the wall is the hot fluid -
        # and using the wrong one puts a few per cent into the energy balance that is nothing to
        # do with the mesh.
        _surface_value("inletBulkT", FLUID, spec.inlet, "weightedAreaAverage", "T", weight="phi"),
        _surface_value("outletBulkT", FLUID, spec.outlet, "weightedAreaAverage", "T",
                       weight="phi"),
        _wall_heat_flux(FN_HEAT_FLUID, FLUID, (FLUID_TO_SOLID,)),
        _wall_heat_flux(FN_HEAT_SOLID, SOLID, (SOLID_TO_FLUID, spec.heated_face)),
    ]
    # `startFrom latestTime` on every chunk after the first, which is what lets the run be stopped,
    # looked at, and picked up again where it left off. `purgeWrite 2` so a long run does not keep
    # a full field set per chunk: two is enough to restart from and to look at.
    last = end if end is not None else spec.iterations
    begin = ("startFrom       startTime;\nstartTime       0;\n" if start <= 0
             else "startFrom       latestTime;\n")
    return (_head("dictionary", "controlDict", "system")
            + "application     chtMultiRegionSimpleFoam;\n"
            + begin
            + f"stopAt          endTime;\nendTime         {last};\n"
            "deltaT          1;\nwriteControl    timeStep;\n"
            f"writeInterval   {max(1, last - max(start, 0))};\npurgeWrite      2;\n"
            "writeFormat     binary;\nwritePrecision  8;\nwriteCompression off;\n"
            "timeFormat      general;\ntimePrecision   6;\nrunTimeModifiable false;\n\n"
            "functions\n{\n" + "\n".join(fns) + "}\n")


def decompose_dict(cores: int) -> str:
    """How the mesh is cut up for MPI.

    `scotch`, which partitions by the mesh's own connectivity and needs no geometric hint. A
    hierarchical split would want to be told how many pieces along each axis, and these parts are
    long thin plates where a wrong guess puts a processor boundary down the length of every
    channel.
    """
    return (_head("dictionary", "decomposeParDict", "system")
            + f"numberOfSubdomains {max(1, int(cores))};\nmethod          scotch;\n")


#: OpenMPI refuses to start as root without being told to. The container runs as root because the
#: mesh is written into a mounted tree, so this is not a choice either.
MPI_FLAGS = ("--allow-run-as-root", "-mca", "btl_base_warn_component_unused", "0")


SOLVER = "chtMultiRegionSimpleFoam"


def decompose_command() -> str:
    """`-allRegions`, because a multi-region case has to be cut up the same way in both regions or
    the interface stops being conformal across processor boundaries. Cutting one region at a time is
    the mistake available here."""
    return "decomposePar -allRegions -force"


def solver_command(cores: int) -> str:
    if cores <= 1:
        return SOLVER
    return f"mpirun {' '.join(MPI_FLAGS)} -np {cores} {SOLVER} -parallel"


def solve_commands(spec: SolveSpec) -> list[tuple[str, str]]:
    """The (stage, shell command) list for one unchunked solve. Kept for a single-shot run."""
    cmds = [("solve", solver_command(spec.cores))]
    if spec.cores > 1:
        cmds.insert(0, ("decomposePar", decompose_command()))
    return cmds


def chunks(total: int, size: int) -> list[tuple[int, int]]:
    """(start, end) iteration ranges for a chunked run.

    Chunking is how the run gets stopped when it is finished, given that neither of the solver's
    own mechanisms will do it. Each chunk runs to its end, writes its fields, and the caller reads
    the residuals out of the log and decides whether to ask for another. The cost is one field
    write per chunk and the saving is every iteration after convergence, which on the measured case
    was most of them.
    """
    if total < 1 or size < 1:
        raise SolveError(f"need at least one iteration and one per chunk, got {total} and {size}")
    out = []
    done = 0
    while done < total:
        nxt = min(done + size, total)
        out.append((done, nxt))
        done = nxt
    return out


def write_case(workspace: Path, spec: SolveSpec, fluid_patches: list[str],
               solid_patches: list[str]) -> list[str]:
    """Write every file the solve needs onto a workspace that already holds the two meshes.

    The patch lists come from the MESH, read out of each region's polyMesh/boundary, never from a
    list of what the block was supposed to have. `splitMeshRegions` keeps on each side only the
    faces that side owns, so the fluid region of a part whose coolant never reaches zmax has no
    zmax patch at all - and a field file naming a patch that is not there is a fatal error before
    the first iteration.
    """
    ws = Path(workspace)
    for role, face, present in (("inlet", spec.inlet, fluid_patches),
                                ("outlet", spec.outlet, fluid_patches),
                                ("heated face", spec.heated_face, solid_patches)):
        if face not in present:
            raise SolveError(
                f"the {role} is {face}, and that region's mesh has no such patch. It has "
                f"{sorted(present)}. Either the coolant does not reach that face or the part is "
                "built the other way up.")
    written = []
    for rel, text in (
        ("constant/regionProperties", region_properties()),
        ("constant/g", gravity()),
        ("constant/fluid/thermophysicalProperties", fluid_thermo(spec.coolant)),
        ("constant/fluid/turbulenceProperties", turbulence()),
        ("constant/solid/thermophysicalProperties", solid_thermo(spec.metal)),
        ("0/fluid/T", fluid_T(spec, fluid_patches)),
        ("0/fluid/U", fluid_U(spec, fluid_patches)),
        ("0/fluid/p_rgh", fluid_p_rgh(spec, fluid_patches)),
        ("0/fluid/p", fluid_p(spec, fluid_patches)),
        ("0/solid/T", solid_T(spec, solid_patches)),
        ("0/solid/p", solid_p(spec, solid_patches)),
        ("system/controlDict", control_dict(spec)),
        ("system/fvSchemes", fv_schemes(FLUID)),
        ("system/fvSolution", fv_solution(FLUID)),
        ("system/fluid/fvSchemes", fv_schemes(FLUID)),
        ("system/fluid/fvSolution", fv_solution(FLUID)),
        ("system/solid/fvSchemes", fv_schemes(SOLID)),
        ("system/solid/fvSolution", fv_solution(SOLID)),
        ("system/decomposeParDict", decompose_dict(spec.cores)),
        ("system/fluid/decomposeParDict", decompose_dict(spec.cores)),
        ("system/solid/decomposeParDict", decompose_dict(spec.cores)),
    ):
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(rel)
    return written


# #
# reading what happened
# #
def _num(token: str):
    try:
        return float(token)
    except ValueError:
        return token


def read_dat(path: Path) -> tuple[list[str], list[list]]:
    """A function object's .dat file: '#' header lines, then whitespace columns.

    The LAST header line names the columns, which is how a value gets found by name rather than by
    position: the column order changes with the operation and with the version, and the names do
    not. Tokens that are not numbers are kept as text, because `wallHeatFlux` puts the patch name
    in a column and a row-wide float() throws the whole row away.
    """
    header: list[str] = []
    rows: list[list] = []
    for line in path.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if (cols := s.lstrip("#").split()):
                header = cols
            continue
        rows.append([_num(t) for t in s.split()])
    return header, rows


def dat_files(root: Path, function: str) -> list[Path]:
    """Every .dat this function object wrote, wherever OpenFOAM put it.

    Searched rather than constructed, because the path is
    `postProcessing/<region>/<function>/<startTime>/<file>.dat` in a multi-region case and
    `postProcessing/<function>/<startTime>/<file>.dat` in a single-region one. Hard-coding either
    shape gives a run that reports every reading as missing and looks like a failed solve.
    """
    base = root / "postProcessing"
    if not base.is_dir():
        return []
    return sorted(d for d in base.rglob("*.dat") if function in d.parts)


#: Never the answer to "which column holds the value", whatever is being looked for. `Time` is the
#: first column of every one of these files, and a substring search for a field called T finds it
#: there before it finds `weightedAreaAverage(T)`. That read the iteration number as a temperature,
#: so the inlet and the outlet both came back as 1500 K, their difference was exactly zero, and the
#: coolant was reported as carrying no heat at all on a mesh that was carrying 6.5 W correctly.
#: Caught by the energy balance, which is the point of taking four readings of the same watts.
_NEVER_A_VALUE = ("Time",)


def dat_value(root: Path, function: str, column_contains: str,
              *, where: tuple[str, str] | None = None) -> float | None:
    """The last value in the column whose name contains `column_contains`.

    An exact name wins over a substring, and the time column is never a candidate. `where` filters
    rows on a text column first, which is what `wallHeatFlux` needs: it writes one row per patch
    per time, so the last row of the file is whichever patch happened to be listed last and not
    the patch asked for.
    """
    # THE LATEST ROW ACROSS EVERY FILE, by its Time column. A chunked run restarts the solver, and
    # each restart writes a fresh `<name>_<startTime>.dat` beside the one before it, so "the last
    # row of the first file that matches" is whichever chunk sorted first alphabetically. On a
    # three-chunk run that is the first 500 iterations, reported as the final answer.
    best_time = float("-inf")
    best: float | None = None
    for dat in dat_files(root, function):
        header, rows = read_dat(dat)
        candidates = [i for i, c in enumerate(header) if c not in _NEVER_A_VALUE]
        want = next((i for i in candidates if header[i] == column_contains), None)
        if want is None:
            want = next((i for i in candidates if column_contains in header[i]), None)
        if want is None:
            continue
        key = next((i for i, c in enumerate(header) if where and c == where[0]), None)
        when = next((i for i, c in enumerate(header) if c == "Time"), None)
        for row in rows:
            if where is not None and (key is None or key >= len(row) or row[key] != where[1]):
                continue
            if want >= len(row) or not isinstance(row[want], float):
                continue
            t = row[when] if when is not None and when < len(row) else 0.0
            if not isinstance(t, float):
                t = 0.0
            if t >= best_time:
                best_time, best = t, row[want]
    return best


def heat_through(root: Path, function: str, patch: str) -> float | None:
    """Watts through one patch, from `wallHeatFlux`'s integral column."""
    return dat_value(root, function, "integral", where=("patch", patch))


_RESIDUAL = re.compile(
    r"Solving for (?P<field>\w+), Initial residual = (?P<value>[-0-9.eE+]+)")
_REGION_MARK = re.compile(r"Solving for (?:fluid|solid) region (?P<region>\w+)")
_MINMAX_T = re.compile(r"Min/max T:\s*([-0-9.eE+]+)\s+([-0-9.eE+]+)")
_TIME = re.compile(r"^Time = ([0-9.eE+-]+)", re.M)


@dataclass
class SolveReport:
    iterations_run: int = 0
    #: True when every equation's last initial residual is at or below its target. Computed here
    #: because the solver will not do it (see RESIDUAL_TARGETS), and it is the difference between
    #: a converged answer and a half-finished one: an unfinished run has a coolant that has not
    #: warmed up yet, which reads as a broken energy balance and is nothing of the kind.
    converged: bool = False
    #: field -> (first initial residual, last initial residual), keyed region:field.
    residuals: dict[str, tuple[float, float]] = field(default_factory=dict)
    t_min: float | None = None
    t_max: float | None = None
    mass_in: float | None = None
    mass_out: float | None = None
    inlet_area: float | None = None
    outlet_area: float | None = None
    interface_area: float | None = None
    bulk_t_in: float | None = None
    bulk_t_out: float | None = None
    q_in: float | None = None
    q_solid: float | None = None
    q_fluid: float | None = None
    q_coolant: float | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unmet(self) -> list[tuple[str, float, float]]:
        """(field, residual reached, target) for every equation still above its target."""
        out = []
        for key, (_first, last) in sorted(self.residuals.items()):
            target = residual_target(key.split(":")[-1])
            if last > target:
                out.append((key, last, target))
        return out

    @property
    def mass_closes(self) -> bool:
        if self.mass_in is None or self.mass_out is None:
            return False
        scale = max(abs(self.mass_in), abs(self.mass_out))
        return scale > 0 and abs(self.mass_in + self.mass_out) / scale < 1e-3

    def heat_gap(self, a: float | None, b: float | None) -> float | None:
        if a is None or b is None or a == 0:
            return None
        return abs(abs(b) - abs(a)) / abs(a)

    @property
    def ok(self) -> bool:
        return not self.problems


def read_log(text: str) -> tuple[int, dict[str, tuple[float, float]],
                                 float | None, float | None]:
    """Iterations reached, residuals per region:field, and the temperature range.

    The region marker matters: both regions solve a field called `h`, and a parse that does not
    keep them apart reports the solid's energy residual as the fluid's.
    """
    times = _TIME.findall(text)
    iterations = int(float(times[-1])) if times else 0
    region = "?"
    res: dict[str, list[float]] = {}
    for line in text.splitlines():
        if (m := _REGION_MARK.search(line)):
            region = m.group("region")
            continue
        if (m := _RESIDUAL.search(line)):
            key = f"{region}:{m.group('field')}"
            res.setdefault(key, []).append(float(m.group("value")))
    lo = hi = None
    pairs = _MINMAX_T.findall(text)
    if pairs:
        lo = min(float(a) for a, _ in pairs)
        hi = max(float(b) for _, b in pairs)
    return iterations, {k: (v[0], v[-1]) for k, v in res.items() if v}, lo, hi


def progress(log_text: str) -> tuple[int, bool]:
    """(iterations reached, whether every equation is at or below target) from a log so far.

    The between-chunks question, answered with exactly the same residuals and the same targets the
    final verdict uses. A cheaper check here than there would be a run that stops on one criterion
    and is judged on another.
    """
    iterations, residuals, _lo, _hi = read_log(log_text)
    probe = SolveReport(residuals=residuals)
    return iterations, bool(residuals) and not probe.unmet


def collect(workspace: Path, spec: SolveSpec, log_text: str) -> SolveReport:
    """Everything measurable about the run, and the verdict on the mesh."""
    ws = Path(workspace)
    r = SolveReport()
    r.iterations_run, r.residuals, r.t_min, r.t_max = read_log(log_text)
    r.converged = bool(r.residuals) and not r.unmet

    r.mass_in = dat_value(ws, FN_INLET, "sum(phi)")
    r.mass_out = dat_value(ws, FN_OUTLET, "sum(phi)")
    r.inlet_area = dat_value(ws, FN_INLET, "Area")
    r.outlet_area = dat_value(ws, FN_OUTLET, "Area")
    r.bulk_t_in = dat_value(ws, "inletBulkT", "(T)")
    r.bulk_t_out = dat_value(ws, "outletBulkT", "(T)")

    r.interface_area = dat_value(ws, FN_HEAT_FLUID, "Area")
    r.q_fluid = heat_through(ws, FN_HEAT_FLUID, FLUID_TO_SOLID)
    r.q_solid = heat_through(ws, FN_HEAT_SOLID, SOLID_TO_FLUID)
    r.q_in = heat_through(ws, FN_HEAT_SOLID, spec.heated_face)

    if r.mass_in is not None and r.bulk_t_in is not None and r.bulk_t_out is not None:
        r.q_coolant = abs(r.mass_in) * spec.coolant.cp * (r.bulk_t_out - r.bulk_t_in)

    if r.iterations_run < spec.iterations and not r.converged:
        r.problems.append(
            f"the run stopped at iteration {r.iterations_run} of {spec.iterations} with residuals "
            "still above target, so it did not stop because it was finished. It either diverged or "
            "hit an error; the tail of the log says which.")
    if r.converged:
        r.notes.append(
            f"every equation met its target by iteration {r.iterations_run} of the "
            f"{spec.iterations} allowed")
    elif r.residuals:
        detail = "; ".join(f"{f} reached {v:.3g} against {t:.0e}" for f, v, t in r.unmet)
        r.problems.append(
            f"these equations never met their targets: {detail}. On a mesh of reasonable cells "
            "every equation falls; a stalled residual is usually the mesh fighting the solver "
            "rather than the physics being hard. Raise --iterations to tell a slow mesh from a bad "
            "one, because at the cap the two look identical.")
    else:
        r.problems.append(
            "no residuals were printed at all, so the solver never got as far as its first "
            "equation. The tail of the log says why.")
    if r.t_min is not None and r.t_min < spec.inlet_temperature - 1.0:
        r.problems.append(
            f"the minimum temperature anywhere is {r.t_min:.2f} K, below the "
            f"{spec.inlet_temperature:.2f} K the coolant enters at. Nothing in this case is a heat "
            "sink, so there is no physical way to get colder than the inlet: this is overshoot.")
    if not r.mass_closes:
        r.problems.append(
            f"mass does not balance: {r.mass_in} kg/s in against {r.mass_out} out. They should sum "
            "to zero to solver tolerance.")
    for label, value in (("solid side against the heated face", r.heat_gap(r.q_in, r.q_solid)),
                         ("fluid side against the solid side", r.heat_gap(r.q_solid, r.q_fluid)),
                         ("the coolant against the heated face", r.heat_gap(r.q_in, r.q_coolant))):
        if value is None:
            r.notes.append(f"could not compare heat, {label}: a reading is missing")
        elif value > spec.heat_tol:
            r.problems.append(
                f"the heat read {label} differs by {value * 100:.1f}%, past the "
                f"{spec.heat_tol * 100:.0f}% allowance. Watts in have to equal watts out at steady "
                "state whatever the mesh looks like, so a gap is either a mesh that is not "
                "transferring what its interface says it is or a run nowhere near converged.")
        else:
            r.notes.append(f"heat agrees to {value * 100:.2f}%, {label}")
    return r


__all__ = ["Coolant", "Metal", "SolveSpec", "SolveError", "SolveReport",
           "FLUID", "SOLID", "FLUID_TO_SOLID", "SOLID_TO_FLUID", "MPI_FLAGS",
           "RESIDUAL_TARGETS", "RESIDUAL_DEFAULT", "residual_target",
           "SOLVER", "inlet_vector", "solid_p", "decompose_dict", "decompose_command",
           "solver_command", "solve_commands", "chunks", "control_dict", "write_case",
           "read_log", "read_dat", "dat_files", "dat_value", "heat_through", "collect"]
