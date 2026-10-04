# Responsibility: Answer, from the engines' own declarations, which engine can mesh which flow from which form of geometry.
# Owns: the canonical geometry forms, the flow kinds, the one reading of a file as CAD or surface, and the words for both.
# Boundaries: it reads declarations (EngineSpec.accepts) and file names; it opens no file, meshes nothing and decides no run.
# Collaborates with: engines/base.py (FlowSupport, the admission rule), engines/registry.py, pipeline/geometry_admission.py,
#                    pipeline/engine_fallback.py, pipeline/engine_select.py and agents/intake/.
"""WHICH ENGINE CAN MESH WHAT - one answer, read from the engines' own declarations.

Every engine's spec declares, as data, the flows it is designed for and the canonical geometry
forms its staging can consume for each (EngineSpec.accepts). Everything that offers an engine -
the intake's proposal, the run's start-of-run choice, the fallback ladder and the offer a failed
run ends with - asks this module, so none of them keeps its own list of engines, and a new engine
is offered exactly where its spec says it fits.

Two forms, because the product turns any upload into one or both of them:
* "cad"     - a B-rep CAD solid (STEP, IGES, BREP): exact surfaces and faces;
* "surface" - a triangulated surface (STL and the other mesh formats, a volume mesh's skin).

On 2026-10-03 an aorta STL for internal flow was taken by snappyHexMesh, which refuses a surface
for internal flow by design; the user read it as a crash, was then offered VMTK - which cannot
take an STL either - and the identical run was tried again. Read from the declarations, neither
engine is offered for that file, the refusal happens before anything is built, and it says which
engines CAN do it, or what to change.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePath

#: The canonical forms an input can take. A file is one of them, or unknown ("").
FORM_CAD = "cad"
FORM_SURFACE = "surface"
GEOMETRY_FORMS: tuple[str, ...] = (FORM_CAD, FORM_SURFACE)

#: The flow kinds an engine can be designed for (Purpose.flow_kind names them).
FLOW_EXTERNAL = "external"
FLOW_INTERNAL = "internal"
FLOW_MULTI_REGION = "multi-region"
FLOW_STRUCTURAL = "structural"
FLOW_KINDS: tuple[str, ...] = (FLOW_EXTERNAL, FLOW_INTERNAL, FLOW_MULTI_REGION, FLOW_STRUCTURAL)

#: What a user reads for each form and flow.
FORM_WORDS: dict[str, str] = {
    FORM_CAD: "a CAD solid (STEP or IGES)",
    FORM_SURFACE: "a surface mesh (STL or a similar triangle file)",
}
_FORM_SHORT: dict[str, str] = {FORM_CAD: "a CAD solid", FORM_SURFACE: "a surface mesh"}
FLOW_WORDS: dict[str, str] = {
    FLOW_EXTERNAL: "external flow",
    FLOW_INTERNAL: "internal flow",
    FLOW_MULTI_REGION: "a multi-region (conjugate heat transfer) case",
    FLOW_STRUCTURAL: "a structural (FEA) part",
}

# #
# the one reading of a file as CAD or surface
# #

#: B-rep formats: exact geometry with faces, read through OpenCASCADE.
_CAD_SUFFIXES = frozenset({".step", ".stp", ".iges", ".igs", ".brep", ".brp"})
#: Triangle and mesh formats: whatever the reader makes of them, what reaches an engine is a
#: triangulated surface (a volume mesh contributes its skin).
_SURFACE_SUFFIXES = frozenset({".stl", ".obj", ".ply", ".off", ".3mf", ".gltf", ".glb", ".vtk",
                               ".vtp", ".vtu", ".msh", ".bdf", ".nas", ".inp"})


def _suffix_of(source: object) -> str:
    text = str(source or "").strip().lower()
    if not text:
        return ""
    if text.startswith(".") and "/" not in text and "\\" not in text and text.count(".") == 1:
        return text
    return PurePath(text.replace("\\", "/")).suffix


def geometry_form(source: object) -> str:
    """'cad' or 'surface' for a file path, file name or suffix; '' when it cannot be told.

    THE ONE PLACE the system decides whether an input is a CAD solid or a surface. The formats
    work (FORMATS) adds the reader-backed answer for every format it opens; this function is the
    seam to point at it - every caller below reads the form through here and nowhere else.
    Unknown answers '' and an unknown form is never a reason to refuse anything."""
    suffix = _suffix_of(source)
    if suffix in _CAD_SUFFIXES:
        return FORM_CAD
    if suffix in _SURFACE_SUFFIXES:
        return FORM_SURFACE
    return ""


def geometry_form_of_state(state: Mapping | None) -> str:
    """The form of the geometry a run carries: its materialised file, else the upload's own
    suffix and name - an API turn carries the reference alone, and the form is the same."""
    geo = (state or {}).get("geometry")
    if not isinstance(geo, Mapping):
        return ""
    ref = geo.get("ref") if isinstance(geo.get("ref"), Mapping) else {}
    for candidate in (geo.get("local_path"), (ref or {}).get("suffix_hint"),
                      (ref or {}).get("original_filename")):
        form = geometry_form(candidate)
        if form:
            return form
    return ""


# #
# the flow a purpose asks for
# #

def flow_of(purpose: object) -> str:
    """The flow kind a declared purpose asks for ('' for an unknown purpose)."""
    from meshpipeline.engines.purposes import flow_kind_of
    return flow_kind_of(str(purpose or ""))


# #
# reading the declarations
# #

def _catalog(specs: Mapping | None = None) -> dict:
    if specs is not None:
        return dict(specs)
    from meshpipeline.engines.registry import ENGINE_CATALOG
    return {n: s for n, s in ENGINE_CATALOG.items() if s.implemented}


def accepts(spec, flow: str, form: str) -> bool | None:
    """Whether this engine's staging consumes `form` for `flow`, by its declaration.

    None when the question has no answer: an unknown flow or form (nothing is claimed about
    it), or a flow the engine is not designed for at all (the purpose rule says so, not this)."""
    if not flow or not form:
        return None
    forms = spec.forms_for(flow)
    if forms is None:
        return None
    return form in forms


def designed_for(spec, flow: str) -> bool:
    return spec.forms_for(flow) is not None


def ladder_order(flow: str, specs: Mapping | None = None) -> tuple[str, ...]:
    """Every implemented engine designed for this flow, in its declared ladder order (most robust
    input handling first, then by name) - the order the fallback ladder walks and a start-of-run
    choice prefers. Read from EngineSpec.ladder_rank, never listed by hand."""
    cat = _catalog(specs)
    rows = [s for s in cat.values() if designed_for(s, flow)]
    return tuple(s.name for s in sorted(rows, key=lambda s: (s.ladder_rank, s.name)))


def engines_for(flow: str, form: str = "", specs: Mapping | None = None) -> list[str]:
    """The engines that can mesh this flow from this form, in ladder order. An unknown form
    narrows nothing: every engine designed for the flow is listed."""
    cat = _catalog(specs)
    return [n for n in ladder_order(flow, cat)
            if not form or accepts(cat[n], flow, form) is not False]


def capability_table(specs: Mapping | None = None) -> dict[tuple[str, str, str], bool]:
    """(engine, flow, form) -> can it - the whole declared matrix, for tests and reports."""
    cat = _catalog(specs)
    return {(n, flow, form): bool(accepts(s, flow, form))
            for n, s in sorted(cat.items()) for flow in FLOW_KINDS for form in GEOMETRY_FORMS}


# #
# the words
# #

def engine_words(name: str) -> str:
    from meshpipeline.contracts.display_names import display_name
    return display_name("mesh_engine", name)


def _join(items: list[str], last: str = "and") -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def form_words(form: str, *, short: bool = False) -> str:
    return (_FORM_SHORT if short else FORM_WORDS).get(form, form or "this kind of file")


def flow_words(flow: str) -> str:
    return FLOW_WORDS.get(flow, flow or "this case")


def refusal(spec, flow: str, form: str) -> str:
    """What one engine says when it cannot take this form for this flow - its own limit only."""
    forms = spec.forms_for(flow) or ()
    needs = _join([form_words(f) for f in forms], "or")
    return (f"{engine_words(spec.name)} cannot mesh {flow_words(flow)} from "
            f"{form_words(form)}: for {flow_words(flow)} it needs {needs}.")


def _able_and_takers(flow: str, form: str, able: Iterable[str] | None,
                     takers: Mapping[str, Iterable[str]] | None, exclude: Iterable[str],
                     specs: Mapping | None) -> tuple[list[str], dict[str, list[str]]]:
    """`able`: the engines that take THIS file; `takers`: {another form: the engines that would
    take the request in that form}. A caller that judged the whole request (the ladder: the
    boundaries, the geometry kind) passes both; otherwise the roster answers by form alone."""
    skip = set(exclude)
    can = (list(able) if able is not None else
           [n for n in engines_for(flow, form, specs) if n not in skip])
    other = (takers if takers is not None else
             {f: engines_for(flow, f, specs) for f in GEOMETRY_FORMS if f != form})
    return can, {f: list(v) for f, v in other.items() if list(v)}


def who_can(flow: str, form: str, *, able: Iterable[str] | None = None,
            takers: Mapping[str, Iterable[str]] | None = None, exclude: Iterable[str] = (),
            specs: Mapping | None = None, named: bool = True) -> str:
    """The roster's answer, in a sentence: which engines CAN mesh this from this file, or that
    none can yet and which form the ones that can take instead - always an out. With named=False
    no engine is named (the intake names no engine the user did not ask about)."""
    can, other = _able_and_takers(flow, form, able, takers, exclude, specs)
    if can:
        if not named:
            return (f"Other engines here can mesh {flow_words(flow)} from "
                    f"{form_words(form, short=True)}.")
        names = _join([engine_words(n) for n in can])
        return f"{names} can mesh {flow_words(flow)} from {form_words(form, short=True)}."
    # NO ENGINE takes this file for this request: name the form that works, and who takes it
    if not other:
        return f"No engine here can mesh {flow_words(flow)} as it is set up."
    alt = next(f for f in GEOMETRY_FORMS if f in other)
    names = _join([engine_words(n) for n in other[alt]])
    every = (f"{names} {'take' if len(other[alt]) > 1 else 'takes'}" if named else
             f"the engines that mesh {flow_words(flow)} take")
    return (f"No engine here can mesh {flow_words(flow)} from {form_words(form, short=True)} "
            f"yet - {every} {form_words(alt)}.")


def way_on(flow: str, form: str, *, able: Iterable[str] | None = None,
           takers: Mapping[str, Iterable[str]] | None = None, exclude: Iterable[str] = (),
           specs: Mapping | None = None) -> str:
    """The next step a user can take: name an engine that can, or the file to upload instead."""
    can, other = _able_and_takers(flow, form, able, takers, exclude, specs)
    if can:
        first = engine_words(can[0])
        return (f"Say \"use {first}\" to mesh it with {first}, or tell me what to change in "
                "this chat.")
    if FORM_CAD in other:
        what = ("the fluid region itself" if flow == FLOW_INTERNAL else "the part")
        return (f"Export {what} as a STEP or IGES solid from your CAD tool and upload it in "
                "this chat - that file can be meshed.")
    if other:
        alt = next(f for f in GEOMETRY_FORMS if f in other)
        return f"Upload {form_words(alt)} of the part in this chat - that file can be meshed."
    return "Tell me what to change in this chat."


def takes_line(spec) -> str:
    """One line a proposer reads: what this engine takes, per flow - derived from the declarations
    (EngineSpec.forms_for: `accepts`, and the input contract's internal_from_surface)."""
    parts = []
    for fs in spec.accepts:
        forms = _join([form_words(f, short=True) for f in (spec.forms_for(fs.flow) or fs.forms)],
                      "or")
        shaped = f" (built for {fs.designed_for})" if fs.designed_for else ""
        parts.append(f"{flow_words(fs.flow)}{shaped} from {forms}")
    return ("Takes: " + "; ".join(parts) + ".") if parts else ""


def delivers_line(spec) -> str:
    """One line a proposer reads: what the delivered mesh is - derived from `delivered_mesh`."""
    dm = spec.delivered_mesh
    if dm is None:
        return ""
    layers = ("near-wall prism layers" if dm.prism_layers else
              "no reliable near-wall prism layers")
    return f"Delivers: {dm.cells} cells, a {dm.walls} wall, {layers}."


__all__ = ["FLOW_EXTERNAL", "FLOW_INTERNAL", "FLOW_KINDS", "FLOW_MULTI_REGION",
           "FLOW_STRUCTURAL", "FORM_CAD", "FORM_SURFACE", "GEOMETRY_FORMS", "accepts",
           "capability_table", "delivers_line", "designed_for", "engine_words", "engines_for",
           "flow_of", "flow_words", "form_words", "geometry_form", "geometry_form_of_state",
           "ladder_order", "refusal", "takes_line", "way_on", "who_can"]
