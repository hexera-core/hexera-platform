# Responsibility: Write ECXML files (JEDEC JEP181A, schema Rev 2.0) for the reader's tests and the lab: schema-ordered elements, SI units, realistic electronics.
# Owns: a small writer for every geometry element the schema defines, and the sample models (a vented set-top box, a ducted board, a tiny board).
# Boundaries: test support only; every file it writes validates against the official ECXML Rev 2.0 XSD (JEDEC's copyright, so not in this repo: set ECXML_XSD to a licensed copy to check).
from __future__ import annotations

from xml.sax.saxutils import escape


def _num(v: float) -> str:
    return f"{float(v):.8f}"


class Ecxml:
    """An ECXML document under construction. Every element is written in the order the schema's
    xs:sequence requires; lengths are metres, temperatures kelvin, powers watts."""

    def __init__(self, name: str, producer: str = "FloTHERM") -> None:
        self.name, self.producer = name, producer
        self._domain: str = ""
        self._materials: list[str] = []
        self._stack: list[list[str]] = [[]]

    # -- top level ------------------------------------------------------------------------------
    def domain(self, location, size, *, ambient=(298.15, 298.15, 0.0)) -> Ecxml:
        amb = ""
        if ambient is not None:
            t, tr, p = ambient
            amb = ("<ambientConditions>"
                   f"<temperature>{_num(t)}</temperature>"
                   f"<radiantTemperature>{_num(tr)}</radiantTemperature>"
                   f"<pressure>{_num(p)}</pressure></ambientConditions>")
        self._domain = (f"<solutionDomain>{self._xyz('location', location)}"
                        f"{self._xyz('size', size)}{amb}</solutionDomain>")
        return self

    def material(self, name: str, density: float, specific_heat: float, emissivity: float,
                 conductivity) -> Ecxml:
        kind, value = conductivity
        if kind == "isotropic":
            k = f"<isotropic><conductivity>{_num(value)}</conductivity></isotropic>"
        elif kind == "orthotropic":
            k = f"<orthotropic>{''.join(f'<{a}>{_num(v)}</{a}>' for a, v in zip('xyz', value))}" \
                "</orthotropic>"
        elif kind == "linear":
            c, coeff, tref = value
            k = (f"<linearTemperatureDependant><conductivity>{_num(c)}</conductivity>"
                 f"<coeff>{_num(coeff)}</coeff><tref>{_num(tref)}</tref>"
                 "</linearTemperatureDependant>")
        else:
            pts = "".join(f"<tempCurvePoint><temperature>{_num(t)}</temperature>"
                          f"<conductivity>{_num(c)}</conductivity></tempCurvePoint>"
                          for t, c in value)
            k = f"<nonLinearTemperatureDependant>{pts}</nonLinearTemperatureDependant>"
        self._materials.append(
            f"<material><name>{escape(name)}</name><density>{_num(density)}</density>"
            f"<specific_heat>{_num(specific_heat)}</specific_heat>"
            f"<surfaceEmissivity>{_num(emissivity)}</surfaceEmissivity>"
            f"<thermalConductivity>{k}</thermalConductivity></material>")
        return self

    # -- helpers ----------------------------------------------------------------------------
    @staticmethod
    def _xyz(tag: str, v) -> str:
        return f"<{tag}>{''.join(f'<{a}>{_num(x)}</{a}>' for a, x in zip('xyz', v))}</{tag}>"

    @staticmethod
    def _head(name: str, active: bool) -> str:
        return f"<name>{escape(name)}</name><active>{'true' if active else 'false'}</active>"

    def _add(self, xml: str) -> Ecxml:
        self._stack[-1].append(xml)
        return self

    # -- the 16 geometry elements -------------------------------------------------------------
    def block(self, name, location, size, material, power=0.0, *, active=True) -> Ecxml:
        return self._add(f"<solid3dBlock>{self._head(name, active)}{self._xyz('location', location)}"
                         f"{self._xyz('size', size)}<material>{escape(material)}</material>"
                         f"<powerDissipation>{_num(power)}</powerDissipation></solid3dBlock>")

    def plate(self, name, location, size, plane, material, power=0.0, *, active=True) -> Ecxml:
        return self._add(f"<solid2dBlock>{self._head(name, active)}{self._xyz('location', location)}"
                         f"{self._xyz('size', size)}<plane>{plane}</plane>"
                         f"<material>{escape(material)}</material>"
                         f"<powerDissipation>{_num(power)}</powerDissipation></solid2dBlock>")

    def cylinder(self, name, location, size, plane, material, power=0.0, *, active=True) -> Ecxml:
        return self._add(f"<solidCylinder>{self._head(name, active)}"
                         f"{self._xyz('location', location)}{self._xyz('size', size)}"
                         f"<plane>{plane}</plane><material>{escape(material)}</material>"
                         f"<powerDissipation>{_num(power)}</powerDissipation></solidCylinder>")

    def source(self, name, location, size, power, *, active=True) -> Ecxml:
        return self._add(f"<sourceBlock>{self._head(name, active)}{self._xyz('location', location)}"
                         f"{self._xyz('size', size)}"
                         f"<powerDissipation>{_num(power)}</powerDissipation></sourceBlock>")

    def source2d(self, name, location, size, plane, power, *, active=True) -> Ecxml:
        return self._add(f"<source2dBlock>{self._head(name, active)}"
                         f"{self._xyz('location', location)}{self._xyz('size', size)}"
                         f"<plane>{plane}</plane>"
                         f"<powerDissipation>{_num(power)}</powerDissipation></source2dBlock>")

    def pcb(self, name, location, size, plane, material, *, active=True) -> Ecxml:
        return self._add(f"<printedCircuitBoard>{self._head(name, active)}"
                         f"{self._xyz('location', location)}{self._xyz('size', size)}"
                         f"<plane>{plane}</plane><material>{escape(material)}</material>"
                         "</printedCircuitBoard>")

    def grille(self, name, location, size, plane, loss, free_area, *, active=True) -> Ecxml:
        return self._add(f"<grille>{self._head(name, active)}{self._xyz('location', location)}"
                         f"{self._xyz('size', size)}<plane>{plane}</plane>"
                         f"<lossCoefficient>{_num(loss)}</lossCoefficient>"
                         f"<freeAreaRatio>{_num(free_area)}</freeAreaRatio></grille>")

    def resistance(self, name, location, size, loss_xyz, free_xyz, *, active=True) -> Ecxml:
        return self._add(f"<flowResistance>{self._head(name, active)}"
                         f"{self._xyz('location', location)}{self._xyz('size', size)}"
                         f"{self._xyz('lossCoefficient', loss_xyz)}"
                         f"{self._xyz('freeAreaRatio', free_xyz)}</flowResistance>")

    def enclosure(self, name, location, size, material, wall, *, active=True) -> Ecxml:
        return self._add(f"<enclosure>{self._head(name, active)}{self._xyz('location', location)}"
                         f"{self._xyz('size', size)}<material>{escape(material)}</material>"
                         f"<wallThickness>{_num(wall)}</wallThickness></enclosure>")

    def heatsink(self, name, parts, *, active=True) -> Ecxml:
        """parts: (name, location, size, material, power) tuples - the base, then each fin."""
        blocks = "".join(
            f"<solid3dBlock>{self._head(n, True)}{self._xyz('location', loc)}"
            f"{self._xyz('size', sz)}<material>{escape(m)}</material>"
            f"<powerDissipation>{_num(p)}</powerDissipation></solid3dBlock>"
            for n, loc, sz, m, p in parts)
        return self._add(f"<heatsink>{self._head(name, active)}<parts>{blocks}</parts></heatsink>")

    def two_resistor(self, name, location, size, plane, power, theta_jc, theta_jb, *,
                     active=True) -> Ecxml:
        return self._add(f"<twoResistorModel>{self._head(name, active)}"
                         f"{self._xyz('location', location)}{self._xyz('size', size)}"
                         f"<plane>{plane}</plane><powerDissipation>{_num(power)}</powerDissipation>"
                         f"<thetaJC>{_num(theta_jc)}</thetaJC><thetaJB>{_num(theta_jb)}</thetaJB>"
                         "</twoResistorModel>")

    @staticmethod
    def _flow(flow) -> str:
        if isinstance(flow, (int, float)):
            return f"<flowDefinition><fixedFlowRate>{_num(flow)}</fixedFlowRate></flowDefinition>"
        pts = "".join(f"<fanCurvePoint><pressure>{_num(p)}</pressure>"
                      f"<volumeFlow>{_num(q)}</volumeFlow></fanCurvePoint>" for q, p in flow)
        return f"<flowDefinition><fanCurve>{pts}</fanCurve></flowDefinition>"

    def fan2d(self, name, location, size, plane, flow, *, active=True) -> Ecxml:
        return self._add(f"<rectangular2dFan>{self._head(name, active)}"
                         f"{self._xyz('location', location)}{self._xyz('size', size)}"
                         f"<plane>{plane}</plane>{self._flow(flow)}</rectangular2dFan>")

    def fan3d(self, name, location, size, plane, hub, flow, *, active=True) -> Ecxml:
        return self._add(f"<axial3dFan>{self._head(name, active)}{self._xyz('location', location)}"
                         f"{self._xyz('size', size)}<plane>{plane}</plane>"
                         f"<hubSize>{_num(hub)}</hubSize>{self._flow(flow)}</axial3dFan>")

    def monitor(self, name, location, *, active=True) -> Ecxml:
        return self._add(f"<monitorPoint>{self._head(name, active)}"
                         f"{self._xyz('location', location)}</monitorPoint>")

    def mcad(self, name, location, file_name, material, power=0.0, *, active=True) -> Ecxml:
        rot = "".join(f"<r{i}{j}>{_num(1.0 if i == j else 0.0)}</r{i}{j}>"
                      for i in (1, 2, 3) for j in (1, 2, 3))
        return self._add(f"<externalMcadFile>{self._head(name, active)}"
                         f"{self._xyz('location', location)}<rotationMatrix>{rot}</rotationMatrix>"
                         f"<material>{escape(material)}</material>"
                         f"<powerDissipation>{_num(power)}</powerDissipation>"
                         f"<fileName>{escape(file_name)}</fileName></externalMcadFile>")

    def assembly(self, name, *, active=True):
        doc = self

        class _Assembly:
            def __enter__(self_inner):
                doc._stack.append([])
                return doc

            def __exit__(self_inner, *exc):
                body = "".join(doc._stack.pop())
                doc._add(f"<assembly>{doc._head(name, active)}<geometry>{body}</geometry>"
                         "</assembly>")
                return False

        return _Assembly()

    def xml(self) -> bytes:
        assert len(self._stack) == 1, "an assembly was left open"
        mats = f"<materials>{''.join(self._materials)}</materials>" if self._materials else ""
        body = (f'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n<neutralXML>'
                f"<name>{escape(self.name)}</name><producer>{escape(self.producer)}</producer>"
                f"{self._domain}{mats}<geometry>{''.join(self._stack[0])}</geometry>"
                "</neutralXML>\n")
        return body.encode("utf-8")


# ------------------------------------------------------------------------------ materials ---
def standard_materials(doc: Ecxml) -> Ecxml:
    """Typical electronics-cooling materials (handbook values)."""
    return (doc
            .material("Aluminium 6063", 2700, 900, 0.1, ("isotropic", 201))
            .material("FR4 board", 1900, 1150, 0.9, ("orthotropic", (17.0, 17.0, 0.4)))
            .material("Mold compound", 1900, 900, 0.9, ("isotropic", 0.8))
            .material("Steel sheet", 7850, 460, 0.3, ("linear", (52.0, -0.03, 293.15)))
            .material("Silicon", 2330, 700, 0.8,
                      ("nonlinear", ((250.0, 190.0), (300.0, 150.0), (400.0, 100.0)))))


# ------------------------------------------------------------------------------ the models --
def set_top_box() -> Ecxml:
    """A vented set-top box in still room air: a steel enclosure with an inlet grille low on the
    front and an axial fan in the back wall, a board on stand-offs, a processor (a 2-resistor
    model) under an aluminium heat sink, memory and power parts, a volume source and monitor
    points. The solution domain leaves room-air all round."""
    doc = standard_materials(Ecxml("Set-top box, vented"))
    doc.domain((-0.03, -0.03, -0.02), (0.18, 0.15, 0.08), ambient=(308.15, 308.15, 0.0))
    ex0, ey0, ez0 = 0.0, 0.0, 0.0
    ex, ey, ez = 0.12, 0.09, 0.035
    wall = 0.0025
    with doc.assembly("Housing"):
        doc.enclosure("Case", (ex0, ey0, ez0), (ex, ey, ez), "Steel sheet", wall)
        # front inlet grille in the -x wall, back fan in the +x wall
        doc.grille("Front vent", (ex0, 0.015, 0.006), (wall, 0.06, 0.012), "+yz", 2.5, 0.5)
        doc.fan3d("Exhaust fan", (ex - wall - 0.008, 0.0325, 0.006), (0.008 + wall, 0.025, 0.025),
                  "+yz", 0.01, [(0.0, 45.0), (0.0002, 30.0), (0.0004, 12.0), (0.0005, 0.0)])
    with doc.assembly("Mainboard"):
        doc.pcb("PCB", (0.01, 0.01, 0.008), (0.09, 0.07, 0.0016), "+xy", "FR4 board")
        top = 0.008 + 0.0016
        doc.two_resistor("SoC", (0.03, 0.025, top), (0.02, 0.02, 0.002), "+xy", 6.0, 0.3, 4.5)
        fins = [(f"Fin {i + 1}", (0.03 + 0.0035 * i, 0.025, top + 0.004),
                 (0.0015, 0.02, 0.012), "Aluminium 6063", 0.0) for i in range(6)]
        doc.heatsink("SoC heat sink",
                     [("Base", (0.03, 0.025, top + 0.002), (0.02, 0.02, 0.002), "Aluminium 6063",
                       0.0)] + fins)
        doc.block("DDR 1", (0.06, 0.02, top), (0.012, 0.009, 0.0012), "Mold compound", 0.6)
        doc.block("DDR 2", (0.06, 0.045, top), (0.012, 0.009, 0.0012), "Mold compound", 0.6)
        doc.block("PMIC", (0.08, 0.03, top), (0.006, 0.006, 0.001), "Silicon", 0.9)
        doc.cylinder("Bulk cap", (0.015, 0.05, top), (0.008, 0.008, 0.011), "+xy",
                     "Aluminium 6063", 0.1)
        doc.source("SoC die heat", (0.036, 0.031, top + 0.0005), (0.008, 0.008, 0.001), 0.5)
        doc.monitor("Tj SoC", (0.04, 0.035, top + 0.001))
        doc.monitor("Air exhaust", (0.115, 0.045, 0.02))
    return doc


def ducted_board() -> Ecxml:
    """A board in a wind-tunnel duct - the kind of system JEP181A's annotated example describes,
    with dimensions of our own: a fan blows into the duct through one end, a grille lets the air
    out of the other, the four long sides are thin walls on the domain boundary. On the board: a
    BGA as a 2-resistor model under a finned heat sink, a regulator with a surface heat source, a
    shield plate with a thickness, and a cable bundle as a flow resistance."""
    doc = standard_materials(Ecxml("Ducted board", producer="FloTHERM"))
    x0, y0, z0 = -0.06, -0.12, -0.006
    sx, sy, sz = 0.12, 0.24, 0.036
    doc.domain((x0, y0, z0), (sx, sy, sz), ambient=(313.15, 313.15, 0.0))
    with doc.assembly("Tunnel"):
        with doc.assembly("Sides"):
            doc.plate("Low X", (x0, y0, z0), (0.001, sy, sz), "+yz", "Steel sheet")
            doc.plate("High X", (x0 + sx, y0, z0), (0.001, sy, sz), "+yz", "Steel sheet")
            doc.plate("Low Z", (x0, y0, z0), (sx, sy, 0.001), "+xy", "Steel sheet")
            doc.plate("High Z", (x0, y0, z0 + sz), (sx, sy, 0.001), "+xy", "Steel sheet")
        doc.fan2d("Inlet fan", (x0, y0 + sy, z0), (sx, 0.0, sz), "-xz", 0.004)
        doc.grille("Outlet", (x0, y0, z0), (sx, 0.002, sz), "+xz", 120.0, 0.8)
    doc.pcb("Board", (-0.04, -0.05, 0.002), (0.08, 0.1, 0.0016), "+xy", "FR4 board")
    top = 0.002 + 0.0016
    with doc.assembly("BGA 27x27"):
        doc.two_resistor("BGA", (-0.0135, -0.0135, top), (0.027, 0.027, 0.0025), "+xy", 8.0,
                         0.25, 3.0)
        fins = [(f"Fin {i + 1}", (-0.0135 + 0.005 * i, -0.0135, top + 0.0045),
                 (0.002, 0.027, 0.015), "Aluminium 6063", 0.0) for i in range(6)]
        doc.heatsink("BGA heat sink", [("Base", (-0.0135, -0.0135, top + 0.0025),
                                        (0.027, 0.027, 0.002), "Aluminium 6063", 0.0)] + fins)
        doc.monitor("Tj", (0.0, 0.0, top + 0.001))
    doc.block("VRM", (0.015, 0.025, top), (0.01, 0.008, 0.003), "Mold compound", 1.5)
    doc.source2d("VRM top", (0.015, 0.025, top + 0.003), (0.01, 0.008, 0.0), "+xy", 0.5)
    doc.plate("Shield", (-0.035, 0.03, top), (0.03, 0.001, 0.008), "+xz", "Steel sheet")
    doc.resistance("Cable bundle", (-0.03, -0.1, 0.0), (0.06, 0.02, 0.012), (40.0, 40.0, 40.0),
                   (0.6, 0.6, 0.6))
    return doc


def tiny_board() -> Ecxml:
    """The smallest useful model: a board and one hot part in a box of air."""
    doc = Ecxml("Tiny board")
    doc.material("FR4", 1900, 1150, 0.9, ("isotropic", 0.35))
    doc.material("Alu", 2700, 900, 0.1, ("isotropic", 200))
    doc.domain((0.0, 0.0, 0.0), (0.05, 0.04, 0.02))
    doc.pcb("Board", (0.01, 0.01, 0.005), (0.03, 0.02, 0.0016), "+xy", "FR4")
    doc.block("Chip", (0.02, 0.015, 0.0066), (0.01, 0.01, 0.002), "Alu", 2.0)
    return doc


def wirebond_package(cap_gap_m: float | None = None) -> Ecxml:
    """A wire-bond package on its substrate: a mold block written first, then a 25 um die attach
    and a 0.3 mm die that overwrite it (Flotherm's rule: the later object wins). With `cap_gap_m`
    a capacitor stands that far above the substrate beside the mold - a real air gap."""
    doc = Ecxml("Wire-bond package")
    doc.material("Cu", 8900, 385, 0.1, ("isotropic", 390.0))
    doc.material("Si", 2330, 700, 0.8, ("isotropic", 150.0))
    doc.material("Epoxy", 1900, 900, 0.9, ("isotropic", 0.8))
    doc.material("Ag epoxy", 3000, 300, 0.9, ("isotropic", 2.0))
    doc.domain((0.0, 0.0, 0.0), (0.01, 0.01, 0.004))
    doc.block("Substrate", (0.002, 0.002, 0.001), (0.006, 0.006, 0.0005), "Cu")
    doc.block("Mold", (0.0025, 0.0025, 0.0015), (0.003, 0.003, 0.0008), "Epoxy")
    doc.block("Die attach", (0.003, 0.003, 0.0015), (0.002, 0.002, 0.000025), "Ag epoxy")
    doc.block("Die", (0.003, 0.003, 0.001525), (0.002, 0.002, 0.0003), "Si", 1.0)
    if cap_gap_m is not None:
        doc.block("Cap", (0.006, 0.006, 0.0015 + cap_gap_m), (0.001, 0.001, 0.0005), "Cu")
    return doc


MODELS = {"set_top_box": set_top_box, "ducted_board": ducted_board, "tiny_board": tiny_board}
