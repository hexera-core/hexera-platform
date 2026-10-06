# Responsibility: Read an ECXML file (JEDEC JEP181A, ECXML schema Rev 2.0) into a plain model: every object, its box, its material and its physics.
# Owns: the safe XML read (no DTD, no entities, bounded size, depth and count), the schema's element rules, and the model the solid builder and the sidecar read.
# Boundaries: no OpenCASCADE and no geometry; every number stays in the file's SI units (lengths in metres); a missing required value is an error that says where it is.
# Collaborates with: cad/ingest/ecxml_build.py (builds the solids and the air), cad/ingest/canonical.py, cad/ingest/sniff.py.
"""ECXML - the neutral electronics-cooling model format of JEDEC JEP181A (schema "ECXML Rev 2.0").

Simcenter Flotherm, Ansys Icepak, Cadence Celsius EC / 6SigmaET and MSC scSTREAM exchange system
level thermal models in it. The format, as JEP181A defines it:

* the root is ``neutralXML``: ``name``, ``producer``, an optional ``solutionDomain`` (location,
  size, optional ambient conditions), an optional ``materials`` list and a ``geometry`` list;
* every value is a child ELEMENT (the schema declares no attributes), in SI units: metres, kelvin,
  pascals, watts, W/mK, K/W, J/kgK, kg/m3, m3/s (JEP181A 4.1); the schema has no way to say
  anything else;
* every object is an axis-aligned box: ``location`` is the minimum corner and ``size`` the extent
  of its bounding box (4.2.1, 4.2.2). Rotation is not supported, except for a referenced MCAD file;
* two-dimensional objects carry a ``plane`` (+xy, -xy, +yz, -yz, +xz, -xz): the letters are the
  plane, the sign the direction the object points (4.2.4);
* an ``assembly`` only groups: it has no location of its own (5.1);
* objects later in the file overwrite earlier ones where solids overlap (4.5.1).

The reader is deliberately safe: an upload is untrusted XML. No DTD is accepted (so no entity can
be declared, external or internal, and no "billion laughs"), the size, nesting depth and element
count are bounded, and nothing in the file is ever fetched - a referenced MCAD file is recorded and
left out, never opened.
"""
from __future__ import annotations

import math
import xml.parsers.expat as expat
from dataclasses import dataclass, field
from pathlib import Path

#: What the reader was written against.
SPEC = "JEDEC JEP181A (November 2023), ECXML schema Rev 2.0"

#: The largest ECXML file read. A detailed server model is a few MB of XML; 256 MB is far beyond
#: any real one and still bounded.
MAX_ECXML_BYTES = 256 * 1024 * 1024
#: Element nesting. Every assembly level adds two (assembly/geometry); 200 is ~95 levels deep.
MAX_DEPTH = 200
#: Elements in the whole file (each object is ~20 elements). The tree is held in memory while it
#: is read - about 150 bytes an element, so this caps the read near 150 MB however the file is
#: built.
MAX_ELEMENTS = 1_000_000
#: Model objects (blocks, boards, fans...) - a whole server rack is a few thousand.
MAX_OBJECTS = 50_000
#: The text of one element: a name or a number, never megabytes.
MAX_TEXT_CHARS = 64 * 1024

ROOT = "neutralXML"
#: The producers the schema enumerates. Others (6SigmaET, Celsius EC, and the spelling "Flotherm"
#: JEP181A's own Annex A example uses) are read the same way and named in a note.
PRODUCERS = ("FloTHERM", "Icepak")
PLANES = ("+xy", "-xy", "+yz", "-yz", "+xz", "-xz")

#: The 16 geometry elements of the schema, each with the children it REQUIRES, in schema order.
#: Every one of them also allows any further element (xs:any) except externalMcadFile.
OBJECT_FIELDS: dict[str, tuple[str, ...]] = {
    "solid2dBlock": ("name", "active", "location", "size", "plane", "material", "powerDissipation"),
    "solid3dBlock": ("name", "active", "location", "size", "material", "powerDissipation"),
    "solidCylinder": ("name", "active", "location", "size", "plane", "material",
                      "powerDissipation"),
    "sourceBlock": ("name", "active", "location", "size", "powerDissipation"),
    "source2dBlock": ("name", "active", "location", "size", "plane", "powerDissipation"),
    "printedCircuitBoard": ("name", "active", "location", "size", "plane", "material"),
    "grille": ("name", "active", "location", "size", "plane", "lossCoefficient", "freeAreaRatio"),
    "flowResistance": ("name", "active", "location", "size", "lossCoefficient", "freeAreaRatio"),
    "assembly": ("name", "active", "geometry"),
    "enclosure": ("name", "active", "location", "size", "material", "wallThickness"),
    "heatsink": ("name", "active", "parts"),
    "twoResistorModel": ("name", "active", "location", "size", "plane", "powerDissipation",
                         "thetaJC", "thetaJB"),
    "rectangular2dFan": ("name", "active", "location", "size", "plane", "flowDefinition"),
    "axial3dFan": ("name", "active", "location", "size", "plane", "hubSize", "flowDefinition"),
    "monitorPoint": ("name", "active", "location"),
    "externalMcadFile": ("name", "active", "location", "rotationMatrix", "material",
                         "powerDissipation", "fileName"),
}
#: Every element name the schema defines. Tags are matched without regard to capitals (Icepak's
#: documentation writes solid2DBlock and rectangular2dfan for the schema's solid2dBlock and
#: rectangular2dFan) and read under the schema's own spelling.
SCHEMA_TAGS = frozenset({
    ROOT, "name", "producer", "solutionDomain", "materials", "material", "geometry", "location",
    "size", "ambientConditions", "temperature", "radiantTemperature", "pressure", "density",
    "specific_heat", "surfaceEmissivity", "thermalConductivity", "isotropic", "orthotropic",
    "linearTemperatureDependant", "nonLinearTemperatureDependant", "conductivity", "coeff", "tref",
    "tempCurvePoint", "x", "y", "z", "active", "plane", "powerDissipation", "lossCoefficient",
    "freeAreaRatio", "wallThickness", "parts", "thetaJC", "thetaJB", "flowDefinition",
    "fixedFlowRate", "fanCurve", "fanCurvePoint", "volumeFlow", "hubSize", "rotationMatrix",
    "r11", "r12", "r13", "r21", "r22", "r23", "r31", "r32", "r33", "fileName",
    *OBJECT_FIELDS})
_CANONICAL_TAG = {t.lower(): t for t in SCHEMA_TAGS}
#: Objects whose size may be zero on an axis (schema type sizeAllowZero): the 2D ones.
ZERO_SIZE_ALLOWED = frozenset({"solid2dBlock", "source2dBlock", "rectangular2dFan"})
#: Objects the schema describes as two-dimensional (a plate, a face): their plane is the object.
TWO_D = frozenset({"solid2dBlock", "source2dBlock", "grille", "rectangular2dFan"})


class EcxmlError(ValueError):
    """A file that is not a readable ECXML model. The message is a sentence for the user."""


# ------------------------------------------------------------------------------ safe XML ----
@dataclass(slots=True)
class Node:
    tag: str                                  # local name, any namespace prefix dropped
    line: int
    children: list[Node] = field(default_factory=list)
    text: str = ""

    def all(self, tag: str) -> list[Node]:
        return [c for c in self.children if c.tag == tag]

    def first(self, tag: str) -> Node | None:
        return next((c for c in self.children if c.tag == tag), None)


class _TreeBuilder:
    def __init__(self, parser) -> None:
        self.parser = parser
        self.stack: list[Node] = []
        self.root: Node | None = None
        self.count = 0
        self.text: list[list[str]] = []
        self.text_len: list[int] = []

    def start(self, name: str, _attrs) -> None:
        self.count += 1
        if self.count > MAX_ELEMENTS:
            raise EcxmlError(f"the file holds more than {MAX_ELEMENTS:,} XML elements - far more "
                             "than any thermal model needs")
        if len(self.stack) >= MAX_DEPTH:
            raise EcxmlError(f"the XML nests deeper than {MAX_DEPTH} levels")
        local = name.rsplit(" ", 1)[-1].rsplit(":", 1)[-1]
        node = Node(tag=_CANONICAL_TAG.get(local.lower(), local),
                    line=int(self.parser.CurrentLineNumber))
        if self.stack:
            self.stack[-1].children.append(node)
        elif self.root is None:
            self.root = node
        self.stack.append(node)
        self.text.append([])
        self.text_len.append(0)

    def end(self, _name: str) -> None:
        node = self.stack.pop()
        node.text = "".join(self.text.pop()).strip()
        self.text_len.pop()

    def data(self, chunk: str) -> None:
        if not self.stack:
            return
        self.text_len[-1] += len(chunk)
        if self.text_len[-1] > MAX_TEXT_CHARS:
            raise EcxmlError(f"an element on line {self.parser.CurrentLineNumber} holds more than "
                             f"{MAX_TEXT_CHARS:,} characters of text; ECXML values are names and "
                             "numbers")
        self.text[-1].append(chunk)


def _forbid_dtd(*_args) -> None:
    raise EcxmlError("the file declares a DTD (<!DOCTYPE>); ECXML has none, and a DTD is how an "
                     "XML file makes a reader fetch other files or expand entities without end, "
                     "so it is not read")


def _forbid_entity(*_args) -> None:
    raise EcxmlError("the file declares an XML entity; ECXML has none, so it is not read")


def _forbid_external(*_args) -> int:
    raise EcxmlError("the file refers to an external XML entity; it is never fetched")


def parse_xml(source) -> Node:
    """The element tree of an XML file or bytes, read with every network- and entity-feature of
    XML refused and every size bounded. Raises EcxmlError for anything else."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.buffer_text = True
    builder = _TreeBuilder(parser)
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = _forbid_dtd
    parser.EntityDeclHandler = _forbid_entity
    parser.UnparsedEntityDeclHandler = _forbid_entity
    parser.ExternalEntityRefHandler = _forbid_external
    try:
        if isinstance(source, (bytes, bytearray)):
            if len(source) > MAX_ECXML_BYTES:
                raise EcxmlError(_too_big())
            parser.Parse(bytes(source), True)
        else:
            path = Path(source)
            if path.stat().st_size > MAX_ECXML_BYTES:
                raise EcxmlError(_too_big())
            with path.open("rb") as fh:
                read = 0
                while True:
                    chunk = fh.read(1 << 20)
                    read += len(chunk)
                    if read > MAX_ECXML_BYTES:
                        raise EcxmlError(_too_big())
                    if not chunk:
                        parser.Parse(b"", True)
                        break
                    parser.Parse(chunk, False)
    except expat.ExpatError as exc:
        raise EcxmlError(f"the file is not well-formed XML ({expat.ErrorString(exc.code)}, "
                         f"line {exc.lineno})") from exc
    if builder.root is None:
        raise EcxmlError("the file holds no XML element")
    return builder.root


def _too_big() -> str:
    return (f"the file is larger than {MAX_ECXML_BYTES // (1024 * 1024)} MB - far larger than "
            "any ECXML thermal model")


# ------------------------------------------------------------------------------ the model ---
Vec = tuple[float, float, float]


@dataclass(frozen=True)
class Material:
    name: str
    density: float                 # kg/m3
    specific_heat: float           # J/kgK
    emissivity: float              # 0..1
    conductivity: dict             # {"kind": isotropic|orthotropic|linear|nonlinear, ...} W/mK

    def as_dict(self) -> dict:
        return {"name": self.name, "density_kg_m3": self.density,
                "specific_heat_J_kgK": self.specific_heat, "surface_emissivity": self.emissivity,
                "thermal_conductivity": self.conductivity}


@dataclass(frozen=True)
class Obj:
    """One geometry object of the file, as the schema describes it, with its place in the tree."""
    kind: str                      # the schema element: solid3dBlock, grille, ...
    name: str
    path: tuple[str, ...]          # names of the assemblies (and heatsink) it sits in
    order: int                     # position in the file: later overwrites earlier (4.5.1)
    active: bool
    location: Vec | None = None    # minimum corner, metres
    size: Vec | None = None        # bounding-box extent, metres
    plane: str = ""                # +xy ... -xz, for the objects that have one
    material: str = ""
    power: float | None = None     # W
    props: dict = field(default_factory=dict)   # the rest of the object's physics, by schema name

    @property
    def label(self) -> str:
        return "/".join(self.path + (self.name,))

    @property
    def lo(self) -> Vec:
        assert self.location is not None
        return self.location

    @property
    def hi(self) -> Vec:
        assert self.location is not None and self.size is not None
        return (self.location[0] + self.size[0], self.location[1] + self.size[1],
                self.location[2] + self.size[2])


@dataclass(frozen=True)
class Domain:
    location: Vec
    size: Vec
    ambient: dict | None = None    # {"temperature_K", "radiant_temperature_K", "pressure_Pa"}


@dataclass
class EcxmlModel:
    name: str
    producer: str
    domain: Domain | None
    materials: dict[str, Material]
    objects: list[Obj]                      # every object, depth-first in file order
    notes: list[str] = field(default_factory=list)
    ignored_elements: dict[str, int] = field(default_factory=dict)   # vendor extensions, by tag

    @property
    def active_objects(self) -> list[Obj]:
        return [o for o in self.objects if o.active]


def axis_of_plane(plane: str) -> int:
    """The axis a plane is normal to: xy -> z (2), yz -> x (0), xz -> y (1)."""
    letters = plane[1:]
    return {"xy": 2, "yz": 0, "xz": 1}[letters]


def sign_of_plane(plane: str) -> int:
    return 1 if plane.startswith("+") else -1


# ------------------------------------------------------------------------------ reading -----
class _Reader:
    def __init__(self) -> None:
        self.ignored: dict[str, int] = {}
        self.order = 0
        self.notes: list[str] = []

    # -- values --------------------------------------------------------------------------------
    @staticmethod
    def _where(path: tuple[str, ...], what: str) -> str:
        return f"{'/'.join(path)}: {what}" if path else what

    def child(self, node: Node, tag: str, path: tuple[str, ...]) -> Node:
        found = node.first(tag)
        if found is None:
            raise EcxmlError(self._where(path, f"<{node.tag}> on line {node.line} has no <{tag}>, "
                                               "which the ECXML schema requires"))
        return found

    def number(self, node: Node, tag: str, path, *, minimum: float | None = None,
               exclusive: bool = False, maximum: float | None = None) -> float:
        el = self.child(node, tag, path)
        return self.parse_number(el, path, minimum=minimum, exclusive=exclusive, maximum=maximum)

    def parse_number(self, el: Node, path, *, minimum: float | None = None,
                     exclusive: bool = False, maximum: float | None = None) -> float:
        text = el.text.strip()
        try:
            value = float(text)
        except ValueError:
            raise EcxmlError(self._where(path, f"<{el.tag}> on line {el.line} is {text[:40]!r}, "
                                               "not a number")) from None
        if not math.isfinite(value):
            raise EcxmlError(self._where(path, f"<{el.tag}> on line {el.line} is {text[:40]!r}; "
                                               "a model needs finite numbers"))
        if minimum is not None and (value < minimum or (exclusive and value == minimum)):
            bound = f"greater than {minimum:g}" if exclusive else f"at least {minimum:g}"
            raise EcxmlError(self._where(path, f"<{el.tag}> on line {el.line} is {value:g}; the "
                                               f"schema requires it {bound}"))
        if maximum is not None and value > maximum:
            raise EcxmlError(self._where(path, f"<{el.tag}> on line {el.line} is {value:g}; the "
                                               f"schema requires at most {maximum:g}"))
        return value

    def text(self, node: Node, tag: str, path) -> str:
        return self.child(node, tag, path).text

    def boolean(self, node: Node, tag: str, path) -> bool:
        el = self.child(node, tag, path)
        value = el.text.strip().lower()
        if value in ("true", "1"):
            return True
        if value in ("false", "0"):
            return False
        raise EcxmlError(self._where(path, f"<{tag}> on line {el.line} is {el.text[:20]!r}; it "
                                           "must be true, false, 1 or 0"))

    def vec(self, node: Node, tag: str, path, *, minimum: float | None = None,
            exclusive: bool = False) -> Vec:
        el = self.child(node, tag, path)
        here = path + (tag,)
        x, y, z = (self.number(el, a, here, minimum=minimum, exclusive=exclusive)
                   for a in ("x", "y", "z"))
        self._extensions(el, ("x", "y", "z"))
        return (x, y, z)

    def plane(self, node: Node, path) -> str:
        el = self.child(node, "plane", path)
        value = el.text.strip().lower()
        if value not in PLANES:
            raise EcxmlError(self._where(path, f"<plane> on line {el.line} is {el.text[:20]!r}; it "
                                               f"must be one of {', '.join(PLANES)}"))
        return value

    def _extensions(self, node: Node, known) -> None:
        """Children the schema does not name - vendor extensions where it allows them (xs:any),
        strays where it does not - counted and reported; nothing in them is read."""
        for c in node.children:
            if c.tag not in known:
                key = f"{node.tag}/{c.tag}"
                self.ignored[key] = self.ignored.get(key, 0) + 1

    # -- top level ----------------------------------------------------------------------------
    def model(self, root: Node) -> EcxmlModel:
        if root.tag != ROOT:
            raise EcxmlError(f"the XML root element is <{root.tag}>, not <{ROOT}>: this is not an "
                             "ECXML (JEP181) file")
        name = self.text(root, "name", ())
        producer = self.text(root, "producer", ())
        if producer not in PRODUCERS:
            self.notes.append(f"the producer is {producer[:60]!r}, which the ECXML Rev 2.0 schema "
                              f"does not list ({', '.join(PRODUCERS)}); it is read the same way")
        domain = self.domain(root.first("solutionDomain"))
        materials = self.materials(root.first("materials"))
        objects: list[Obj] = []
        self.geometry(self.child(root, "geometry", ()), (), objects)
        self._extensions(root, ("name", "producer", "solutionDomain", "materials", "geometry"))
        model = EcxmlModel(name=name, producer=producer, domain=domain, materials=materials,
                           objects=objects, notes=self.notes, ignored_elements=dict(self.ignored))
        missing = sorted({o.material for o in model.active_objects
                          if o.material and o.material not in materials})
        for m in missing:
            model.notes.append(f"material {m!r} is used but not defined in <materials>; its "
                               "properties are unknown")
        return model

    def domain(self, node: Node | None) -> Domain | None:
        if node is None:
            return None
        path = ("solutionDomain",)
        location = self.vec(node, "location", path)
        size = self.vec(node, "size", path, minimum=0.0, exclusive=True)
        ambient = None
        amb = node.first("ambientConditions")
        if amb is not None:
            here = path + ("ambientConditions",)
            ambient = {"temperature_K": self.number(amb, "temperature", here),
                       "radiant_temperature_K": self.number(amb, "radiantTemperature", here),
                       "pressure_Pa": self.number(amb, "pressure", here)}
            self._extensions(amb, ("temperature", "radiantTemperature", "pressure"))
        self._extensions(node, ("location", "size", "ambientConditions"))
        return Domain(location=location, size=size, ambient=ambient)

    def materials(self, node: Node | None) -> dict[str, Material]:
        out: dict[str, Material] = {}
        if node is None:
            return out
        for m in node.all("material"):
            name = self.text(m, "name", ("materials",))
            path = ("materials", name)
            mat = Material(
                name=name,
                density=self.number(m, "density", path, minimum=0.0, exclusive=True),
                specific_heat=self.number(m, "specific_heat", path, minimum=0.0),
                emissivity=self.number(m, "surfaceEmissivity", path, minimum=0.0, maximum=1.0),
                conductivity=self.conductivity(self.child(m, "thermalConductivity", path), path))
            if name in out:
                self.notes.append(f"material {name!r} is defined twice; the later definition is "
                                  "used")
            out[name] = mat
            self._extensions(m, ("name", "density", "specific_heat", "surfaceEmissivity",
                                 "thermalConductivity"))
        self._extensions(node, ("material",))
        return out

    def conductivity(self, node: Node, path) -> dict:
        here = path + ("thermalConductivity",)
        kinds = list(node.children)
        if len(kinds) != 1:
            raise EcxmlError(self._where(here, "it must hold exactly one of isotropic, orthotropic, "
                                               "linearTemperatureDependant, "
                                               "nonLinearTemperatureDependant"))
        k = kinds[0]
        if k.tag == "isotropic":
            return {"kind": "isotropic",
                    "W_mK": self.number(k, "conductivity", here, minimum=0.0)}
        if k.tag == "orthotropic":
            return {"kind": "orthotropic",
                    "W_mK": [self.number(k, a, here, minimum=0.0) for a in ("x", "y", "z")]}
        if k.tag == "linearTemperatureDependant":
            return {"kind": "linear_in_temperature",
                    "W_mK": self.number(k, "conductivity", here, minimum=0.0),
                    "coeff_W_mK2": self.number(k, "coeff", here),
                    "tref_K": self.number(k, "tref", here),
                    "formula": "k(T) = W_mK + coeff_W_mK2 * (T - tref_K)"}
        if k.tag == "nonLinearTemperatureDependant":
            pts = []
            for p in k.all("tempCurvePoint"):
                pts.append([self.number(p, "temperature", here),
                            self.number(p, "conductivity", here, minimum=0.0)])
            if len(pts) < 2:
                raise EcxmlError(self._where(here, "a nonLinearTemperatureDependant conductivity "
                                                   "needs at least two tempCurvePoint pairs"))
            return {"kind": "piecewise_linear_in_temperature", "points_K_W_mK": pts,
                    "outside_range": "constant at the end values (JEP181A 4.3.4)"}
        raise EcxmlError(self._where(here, f"<{k.tag}> is not a conductivity kind the schema "
                                           "defines"))

    # -- geometry -------------------------------------------------------------------------------
    def geometry(self, node: Node, path: tuple[str, ...], out: list[Obj],
                 inactive_parent: bool = False) -> None:
        for el in node.children:
            if el.tag not in OBJECT_FIELDS:
                key = f"geometry/{el.tag}"
                self.ignored[key] = self.ignored.get(key, 0) + 1
                continue
            if len(out) >= MAX_OBJECTS:
                raise EcxmlError(f"the model holds more than {MAX_OBJECTS:,} objects")
            obj = self.obj(el, path, inactive_parent)
            if el.tag == "assembly":
                out.append(obj)
                self.geometry(self.child(el, "geometry", path + (obj.name,)),
                              path + (obj.name,), out, inactive_parent or not obj.active)
            elif el.tag == "heatsink":
                out.append(obj)
                parts = el.first("parts")
                if parts is None:
                    # Icepak's import table describes a heatsink by location and size alone; the
                    # schema says parts. Without its blocks there are no fins to build.
                    self.notes.append(f"{'/'.join(path + (obj.name,))}: the heatsink lists no "
                                      "<parts>, so it has no blocks to build and is left out")
                    continue
                blocks = parts.all("solid3dBlock")
                if not blocks:
                    raise EcxmlError(self._where(path + (obj.name,), "a heatsink needs at least one "
                                                                     "solid3dBlock in <parts>"))
                for b in blocks:
                    if len(out) >= MAX_OBJECTS:
                        raise EcxmlError(f"the model holds more than {MAX_OBJECTS:,} objects")
                    out.append(self.obj(b, path + (obj.name,), inactive_parent or not obj.active,
                                        heatsink=obj.name))
                self._extensions(parts, ("solid3dBlock",))
            else:
                out.append(obj)

    def obj(self, el: Node, path: tuple[str, ...], inactive_parent: bool,
            heatsink: str = "") -> Obj:
        kind = el.tag
        fields = OBJECT_FIELDS[kind]
        name = self.text(el, "name", path + (f"<{kind}> on line {el.line}",))
        here = path + (name,)
        active = self.boolean(el, "active", here) and not inactive_parent
        self.order += 1
        location = size = None
        plane = material = ""
        power = None
        props: dict = {}
        if kind == "heatsink":
            fields = ("name", "active")         # its <parts> are read by the caller
        if "location" in fields:
            location = self.vec(el, "location", here)
        if "size" in fields:
            if kind in ZERO_SIZE_ALLOWED:
                size = self.vec(el, "size", here, minimum=0.0)
            else:
                size = self.vec(el, "size", here, minimum=0.0, exclusive=True)
        if "plane" in fields:
            plane = self.plane(el, here)
        if "material" in fields:
            material = self.text(el, "material", here)
        if "powerDissipation" in fields:
            power = self.number(el, "powerDissipation", here)
        if kind == "grille":
            props["loss_coefficient"] = self.number(el, "lossCoefficient", here)
            props["free_area_ratio"] = self.number(el, "freeAreaRatio", here, minimum=0.0,
                                                   maximum=1.0)
        elif kind == "flowResistance":
            lc = self.child(el, "lossCoefficient", here)
            fa = self.child(el, "freeAreaRatio", here)
            props["loss_coefficient_xyz"] = [self.number(lc, a, here + ("lossCoefficient",),
                                                         minimum=0.0) for a in "xyz"]
            props["free_area_ratio_xyz"] = [self.number(fa, a, here + ("freeAreaRatio",),
                                                        minimum=0.0, maximum=1.0) for a in "xyz"]
        elif kind == "enclosure":
            props["wall_thickness_m"] = self.number(el, "wallThickness", here, minimum=0.0,
                                                    exclusive=True)
        elif kind == "twoResistorModel":
            props["theta_jc_K_W"] = self.number(el, "thetaJC", here)
            props["theta_jb_K_W"] = self.number(el, "thetaJB", here)
        elif kind in ("rectangular2dFan", "axial3dFan"):
            props["flow"] = self.flow(self.child(el, "flowDefinition", here), here)
            if kind == "axial3dFan":
                props["hub_diameter_m"] = self.number(el, "hubSize", here, minimum=0.0,
                                                      exclusive=True)
        elif kind == "externalMcadFile":
            rm = self.child(el, "rotationMatrix", here)
            props["rotation_matrix"] = [[self.number(rm, f"r{i}{j}", here + ("rotationMatrix",))
                                         for j in (1, 2, 3)] for i in (1, 2, 3)]
            props["file_name"] = self.text(el, "fileName", here)[:500]
        if heatsink:
            props["heatsink"] = heatsink
        self._extensions(el, OBJECT_FIELDS[kind])
        return Obj(kind=kind, name=name, path=path, order=self.order, active=active,
                   location=location, size=size, plane=plane, material=material, power=power,
                   props=props)

    def flow(self, node: Node, path) -> dict:
        here = path + ("flowDefinition",)
        fixed = node.first("fixedFlowRate")
        curve = node.first("fanCurve")
        if (fixed is None) == (curve is None):
            raise EcxmlError(self._where(here, "it must hold exactly one of fixedFlowRate or "
                                               "fanCurve"))
        if fixed is not None:
            return {"kind": "fixed_flow_rate", "m3_s": self.parse_number(fixed, here)}
        assert curve is not None
        pts = [[self.number(p, "volumeFlow", here, minimum=0.0),
                self.number(p, "pressure", here, minimum=0.0)]
               for p in curve.all("fanCurvePoint")]
        if not pts:
            raise EcxmlError(self._where(here, "a fanCurve needs at least one fanCurvePoint"))
        return {"kind": "fan_curve", "points_m3_s_Pa": pts}


def read_ecxml(source) -> EcxmlModel:
    """The model in an ECXML file (a path or bytes). Raises EcxmlError with a sentence that says
    what is wrong and where."""
    return _Reader().model(parse_xml(source))


def looks_like_ecxml(head: bytes) -> bool:
    """Whether the first bytes of a file are an ECXML document: XML whose root is neutralXML.
    Read from the head only (comments and the XML declaration may come first)."""
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):           # UTF-16, as XML allows
        try:
            head = head[:4096].decode("utf-16", "ignore").encode("utf-8", "ignore")
        except (UnicodeError, ValueError):
            return False
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if not text.startswith(b"<"):
        return False
    at = 0
    while True:
        at = text.find(b"<", at)
        if at < 0 or at + 1 >= len(text):
            return False
        nxt = text[at + 1:at + 2]
        if text.startswith(b"<!--", at):   # a comment may hold any text, '>' included
            end = text.find(b"-->", at)
            if end < 0:
                return False
            at = end + 3
            continue
        if nxt in (b"?", b"!"):            # the XML declaration, a doctype: skip past it
            end = text.find(b">", at)
            if end < 0:
                return False
            at = end + 1
            continue
        tag = text[at + 1:at + 64].split(None, 1)[0].split(b">", 1)[0].split(b"/", 1)[0]
        return tag.rsplit(b":", 1)[-1].lower() == ROOT.lower().encode()
