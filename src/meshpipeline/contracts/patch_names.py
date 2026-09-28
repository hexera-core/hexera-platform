# Responsibility: The one rule for what a boundary patch may be called and what kind of boundary it is, and how what a person typed becomes both.
# Owns: the mesh-safe name pattern, the names the meshers keep for themselves, the words people use for each boundary role, and the normaliser; every layer that names a patch uses these.
"""Mesh-safe patch names and boundary roles.

A person names a boundary however they like: "car wall", "Inlet-1", "2nd outlet", "Einlass". The
meshers do not: an OpenFOAM patch is a word (letters, digits, underscores, no spaces), gmsh physical
names follow the same rule in practice, and a name the mesher cannot write is silently replaced by
one it can. The manifest check then looks for the name the user approved, finds zero faces, and a
good mesh fails as "did not meet the required quality checks" (job ac1daa3e, "car wall", 26 minutes).

The same is true of the KIND of boundary. People say "velocity inlet", "pressure outlet", "no-slip
wall", "far field", "symmetry plane", "fixed support"; the system routes on inlet, outlet, wall,
farfield, symmetry, fixed. A word the system does not route on either stops the conversation with a
list of valid words, or - worse - reaches a builder that guesses.

And some names are already taken: every mesher writes boundaries and geometry entries of its own
("outer" around an internal carve, "fixedWalls" for cfMesh's leftovers, "solid" for gmsh's volume,
"FoamFile" at the head of every OpenFOAM file). A user patch under one of those names collides with
the mesher's own and one of the two disappears.

So a declaration is normalised ONCE, where it enters - before the admission preview, the preview
token, the approved snapshot, the builder payload and the manifest check ever see it - and every
one of those reads the same spelling and the same role. The person is told what changed once;
nobody is asked to retype anything. A word that has no one obvious meaning ("opening" inside a duct
is an inlet or an outlet) is left alone, and the validator asks the one question that settles it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A mesh-safe patch name: starts with a letter, then letters, digits and underscores.
MESH_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
MAX_NAME_LENGTH = 64
_NOT_A_WORD_CHAR = re.compile(r"[^A-Za-z0-9_]+")

#: Names a mesher gives its OWN boundaries, geometry entries or element sets, so a user patch may
#: not carry them (compared without regard to capitals - gmsh's Abaqus deck ignores case, and a
#: case-insensitive file system holds one triSurface file per spelling):
#:   FoamFile          the header entry of every OpenFOAM file; the boundary readers skip it
#:   outer             the background box snappyHexMesh carves an internal passage out of
#:   seedZone          the refinement sphere around snappyHexMesh's internal seed point
#:   defaultFaces      blockMesh's patch for every face no patch claims (the multi-region box)
#:   fixedWalls        cfMesh's name for every surface patch renameBoundary does not list
#:   bottomEmptyFaces  cfMesh's 2D front plane, before it is merged into the declared empty patch
#:   topEmptyFaces     cfMesh's 2D back plane, likewise
#:   solid             gmsh's physical group for the meshed volume itself
RESERVED_PATCH_NAMES: frozenset[str] = frozenset({
    "FoamFile", "outer", "seedZone", "defaultFaces", "fixedWalls", "bottomEmptyFaces",
    "topEmptyFaces", "solid"})
#: ...and the numbered families: snappyHexMesh's thin-feature refinement boxes, and the processor
#: boundaries a parallel run writes between subdomains.
_RESERVED_PATTERN = re.compile(r"^(thinzone\d+|procboundary\d+to\d+|processor\d+)$")
_RESERVED_FOLDED = frozenset(n.casefold() for n in RESERVED_PATCH_NAMES)
#: What a reserved name becomes: the user's word, marked as theirs.
RESERVED_SUFFIX = "_patch"


def is_mesh_safe(name: object) -> bool:
    return (isinstance(name, str) and len(name) <= MAX_NAME_LENGTH
            and MESH_SAFE_NAME.match(name) is not None)


def is_reserved(name: object) -> bool:
    """Whether a mesher already uses this name for a boundary or entry of its own."""
    folded = str(name if name is not None else "").strip().casefold()
    return folded in _RESERVED_FOLDED or _RESERVED_PATTERN.match(folded) is not None


def mesh_safe(name: object, *, fallback: str = "patch") -> str:
    """The mesh-safe spelling of `name`: runs of anything but letters, digits and underscores
    become one underscore ("car wall" -> "car_wall", "Inlet-1" -> "Inlet_1"), a leading digit gets
    a "p_" prefix ("2nd outlet" -> "p_2nd_outlet"), and a name with nothing usable in it becomes
    `fallback`. A name that is already safe comes back unchanged."""
    text = str(name if name is not None else "").strip()
    if is_mesh_safe(text):
        return text
    safe = _NOT_A_WORD_CHAR.sub("_", text).strip("_")
    safe = re.sub(r"_+", "_", safe)
    if not safe:
        safe = fallback
    if not safe[0].isalpha():
        safe = f"p_{safe}"
    return safe[:MAX_NAME_LENGTH].rstrip("_") or fallback


def unreserved(name: str) -> str:
    """A mesh-safe name that no mesher keeps for itself: a reserved one gets RESERVED_SUFFIX."""
    if not is_reserved(name):
        return name
    return f"{name[:MAX_NAME_LENGTH - len(RESERVED_SUFFIX)]}{RESERVED_SUFFIX}"


def same_patch_name(a: object, b: object) -> bool:
    """Whether two names would reach a mesher as one name: equal once made mesh-safe, without
    regard to capitals. The comparison the region rule and the engines' region mapping share."""
    return mesh_safe(a).casefold() == mesh_safe(b).casefold()


# THE WORDS PEOPLE USE FOR EACH BOUNDARY ROLE. Keyed by the word as it is compared (lower case,
# separators as single spaces - see _word), valued by the role(s) it can mean. A word with one
# candidate the purpose allows becomes that role; a word with several allowed candidates is
# ambiguous there and stays as typed, so the validator asks. The roles themselves are the purposes'
# vocabulary (engines/purposes.py); this table only says which word means which of them.
_ROLE_WORDS: dict[str, tuple[str, ...]] = {}


def _add(roles: tuple[str, ...], *words: str) -> None:
    for w in words:
        _ROLE_WORDS[w] = roles


_add(("wall",), "wall", "walls", "no slip", "no slip wall", "noslip", "noslip wall",
     "solid wall", "wall boundary", "viscous wall", "adiabatic wall", "isothermal wall",
     "heated wall", "stationary wall", "moving wall", "body", "body wall", "body surface")
_add(("inlet",), "inlet", "inlets", "inflow", "velocity inlet", "velocity inflow",
     "mass flow inlet", "mass flow rate inlet", "mass flow", "total pressure inlet",
     "pressure inlet", "stagnation inlet", "intake", "inlet boundary", "supply")
_add(("outlet",), "outlet", "outlets", "outflow", "pressure outlet", "static pressure outlet",
     "exit", "exhaust", "outlet boundary", "return")
_add(("farfield",), "farfield", "far field", "far field boundary", "freestream",
     "free stream", "free stream boundary", "outer boundary", "outer domain", "domain boundary",
     "pressure far field", "farfield boundary", "far field wall", "ambient")
_add(("symmetry",), "symmetry", "symmetry plane", "symmetryplane", "symmetry boundary",
     "plane of symmetry", "mirror plane", "mirror", "symmetric", "symmetric plane")
_add(("empty",), "empty", "front and back", "frontandback", "front back", "empty 2d",
     "2d empty")
_add(("fixed",), "fixed", "fixed support", "fixed supports", "fixed constraint", "fixed face",
     "fixed boundary", "encastre", "encastré", "clamped", "clamp", "built in", "support",
     "constraint", "anchor", "anchored", "fixture")
_add(("load",), "load", "loads", "load face", "load surface", "applied load", "force",
     "traction", "pressure load", "bolt load", "bearing load", "moment", "torque",
     "remote load")
_add(("contact",), "contact", "contact surface", "contact face", "bonded", "bonded contact",
     "frictional contact")
_add(("free",), "free", "free surface", "free face", "traction free", "unconstrained",
     "unloaded")
_add(("external",), "external")
# Words with more than one meaning: which one depends on the purpose.
#   "opening" - the far field around a body, or an inlet or outlet of a duct;
#   "pressure" - a load on a structure, or a pressure inlet or outlet of a flow.
_add(("farfield", "inlet", "outlet"), "opening", "open boundary", "opening boundary")
_add(("load", "inlet", "outlet"), "pressure", "pressure boundary")

#: Words for a GROUND PLANE, as a type or as a name. As a type they mean a wall; in an external flow
#: a wall so named is the floor of the far-field box (engines/ground_plane.py), whatever it is called.
GROUND_WORDS: frozenset[str] = frozenset({
    "ground", "ground plane", "groundplane", "floor", "floor plane", "road", "road surface",
    "ground surface", "rolling road", "moving ground", "moving floor", "ground wall", "the ground",
    "ground floor"})
_add(("wall",), *GROUND_WORDS)


def _word(text: object) -> str:
    """A typed word as the tables compare it: lower case, '-', '_' and '/' as spaces, no
    brackets, single spaces. "No-Slip Wall" -> "no slip wall", "Velocity_Inlet" -> "velocity inlet"."""
    s = str(text if text is not None else "").casefold()
    s = re.sub(r"[-_/.,;:()\[\]{}]+", " ", s)
    return " ".join(s.split())


def role_candidates(word: object) -> tuple[str, ...]:
    """Every role a typed boundary word can mean, or () for a word the table does not know."""
    return _ROLE_WORDS.get(_word(word), ())


def canonical_role(word: object, allowed=None) -> str | None:
    """The one role a typed word means among `allowed` (every role when None), or None when it
    means none of them - or more than one, which the validator turns into a question."""
    cands = role_candidates(word)
    if allowed is not None:
        cands = tuple(c for c in cands if c in set(allowed))
    return cands[0] if len(cands) == 1 else None


def role_hint(word: object, allowed=None) -> str:
    """What to ask when a typed word has no single meaning here: '' when it simply is not a
    boundary word, or the choice it leaves open ("'opening' could be an inlet or an outlet")."""
    cands = role_candidates(word)
    if allowed is not None:
        cands = tuple(c for c in cands if c in set(allowed))
    if len(cands) > 1:
        return (f"{str(word).strip()!r} could be {' or '.join(repr(c) for c in cands)} here - ask "
                "the user which")
    return ""


def is_ground_word(text: object) -> bool:
    return _word(text) in GROUND_WORDS


@dataclass(frozen=True)
class GroundRule:
    """How an external flow names its ground plane: the one name the domain's floor carries, and
    that a wall called any ground word is that floor. Supplied by the caller (engines own the
    ground plane); this module only applies it."""

    name: str = "ground"


@dataclass(frozen=True)
class Rename:
    typed: str
    name: str
    #: why: "spelling" (not mesh-safe), "reserved" (the mesher's own name), "duplicate" (another
    #: patch already has it, in any capitals), "ground" (the ground plane's one name)
    reason: str


@dataclass(frozen=True)
class Retype:
    name: str
    typed: str
    role: str


@dataclass(frozen=True)
class DeclarationChanges:
    patches: object
    renames: tuple[Rename, ...] = ()
    retypes: tuple[Retype, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return bool(self.renames or self.retypes or self.notes)


def _suffixed(base: str, n: int) -> str:
    suffix = f"_{n}"
    return f"{base[:MAX_NAME_LENGTH - len(suffix)]}{suffix}"


#: Every role any purpose routes on - a typed type already in this set is never re-read. Kept in
#: step with engines/purposes.py by tests/unit/contracts/test_approved_equals_delivered.py.
ALL_ROLES: frozenset[str] = frozenset({"wall", "inlet", "outlet", "farfield", "symmetry",
                                       "empty", "fixed", "load", "contact", "free", "external"})


def _settle_roles(patches: list, allowed) -> tuple[list, list[Retype]]:
    """Each patch's TYPE as a role the purpose routes on. A `role` given instead of `type`
    becomes the type; a boundary word with one meaning among `allowed` becomes that role."""
    retypes: list[Retype] = []
    out: list = []
    for p in patches:
        if not isinstance(p, dict):
            out.append(p)
            continue
        q = dict(p)
        typed_type = q.get("type")
        role_word = q.get("role")
        if (not isinstance(typed_type, str) or not typed_type.strip()) and \
                isinstance(role_word, str) and role_word.strip():
            q["type"] = q.pop("role")
            typed_type = q["type"]
        elif isinstance(role_word, str) and isinstance(typed_type, str) and \
                role_word.strip() == typed_type.strip():
            q.pop("role")                        # the same word twice is one statement
        if isinstance(typed_type, str) and typed_type.strip():
            word = typed_type.strip()
            known = allowed if allowed is not None else ALL_ROLES
            role = None if word in known else canonical_role(word, allowed)
            if role is not None:
                q["type"] = role
                if _word(word) != role:          # a change of capitals is not worth a sentence
                    retypes.append(Retype(name=str(q.get("name") or "").strip(), typed=word,
                                          role=role))
            elif word != typed_type:
                q["type"] = word
        out.append(q)
    return out, retypes


def _named(q) -> bool:
    return isinstance(q, dict) and isinstance(q.get("name"), str) and bool(q["name"].strip())


def _settle_names(staged: list, typed_types: list[str],
                  ground: GroundRule | None) -> tuple[list, list[Rename]]:
    """Each patch's NAME: mesh-safe, unreserved, the ground plane's one name for a ground (with a
    GroundRule), and unique in any capitals. A name typed correctly is claimed first, so it is
    never the one that moves. References (interchangeable_with) follow their renames."""
    taken: set[str] = set()
    final: dict[int, str] = {}
    reasons: dict[int, str] = {}

    def _is_ground(i: int, name: str) -> bool:
        q = staged[i]
        return (str(q.get("type") or "") == "wall"
                and (is_ground_word(name) or is_ground_word(typed_types[i])))

    ground_taken = ground is not None and any(
        _named(q) and q["name"].strip().casefold() == ground.name.casefold() for q in staged)
    for i, q in enumerate(staged):
        if not _named(q):
            continue
        typed = q["name"].strip()
        if (is_mesh_safe(typed) and not is_reserved(typed) and typed.casefold() not in taken
                and not (ground is not None and not ground_taken and _is_ground(i, typed))):
            final[i] = typed
            taken.add(typed.casefold())
    for i, q in enumerate(staged):
        if not _named(q) or i in final:
            continue
        typed = q["name"].strip()
        safe = mesh_safe(typed)
        reason = "spelling" if safe != typed else ""
        if ground is not None and not ground_taken and _is_ground(i, typed):
            safe, reason, ground_taken = ground.name, "ground", True
        if is_reserved(safe):
            safe, reason = unreserved(safe), "reserved"
        base, n = safe, 2
        while safe.casefold() in taken:
            safe = _suffixed(base, n)
            n += 1
            reason = reason if reason in ("spelling", "reserved", "ground") else "duplicate"
        taken.add(safe.casefold())
        final[i] = safe
        if safe != typed:
            reasons[i] = reason or "duplicate"

    out: list = []
    renames: list[Rename] = []
    by_typed: dict[str, str] = {}
    for i, q in enumerate(staged):
        if i not in final:
            out.append(q)
            continue
        typed = q["name"].strip()
        out.append({**q, "name": final[i]})
        if final[i] != typed:
            renames.append(Rename(typed=typed, name=final[i], reason=reasons.get(i, "spelling")))
            by_typed.setdefault(typed, final[i])
    if renames:
        for i, q in enumerate(out):
            if isinstance(q, dict) and isinstance(q.get("interchangeable_with"), list):
                out[i] = {**q, "interchangeable_with": [
                    by_typed.get(str(o).strip(), mesh_safe(o)) for o in q["interchangeable_with"]]}
    return out, renames


def normalize_declaration(patches: object, *, allowed_roles=None,
                          ground: GroundRule | None = None) -> DeclarationChanges:
    """Make a declared patch list one every layer reads the same way.

    Types first: a `role` given instead of `type` becomes the type, and a boundary word with one
    meaning among `allowed_roles` becomes that role ("velocity inlet" -> inlet). Then names: each
    becomes mesh-safe; a name a mesher keeps for itself gets RESERVED_SUFFIX; with a GroundRule
    (an external flow) the first wall named or typed as a ground word becomes the ground plane's
    one name, unless a patch already carries it; and a name another patch already has, in any
    capitals, is told apart with a number. Anything that is not a list is returned untouched: the
    validators report malformed input, not this."""
    if not isinstance(patches, list):
        return DeclarationChanges(patches=patches)
    allowed = set(allowed_roles) if allowed_roles is not None else None
    staged, retypes = _settle_roles(patches, allowed)
    typed_types = [str(p.get("type") or p.get("role") or "") if isinstance(p, dict) else ""
                   for p in patches]
    out, renames = _settle_names(staged, typed_types, ground)
    if renames:
        moved = {r.typed: r.name for r in renames}
        retypes = [Retype(name=moved.get(r.name, r.name), typed=r.typed, role=r.role)
                   for r in retypes]
    return DeclarationChanges(patches=out, renames=tuple(renames), retypes=tuple(retypes))


def normalize_patches(patches: object) -> tuple[object, dict[str, str]]:
    """Names only: every patch name mesh-safe, unreserved and unique in any capitals (see
    normalize_declaration). Returns the new list and the renames made ({typed: new})."""
    if not isinstance(patches, list):
        return patches, {}
    out, renames = _settle_names(list(patches), [""] * len(patches), None)
    return out, {r.typed: r.name for r in renames}


def rename_note(renames: dict[str, str]) -> str:
    """One plain sentence for the person, naming each change."""
    if not renames:
        return ""
    pairs = ", ".join(f"'{typed}' is now {safe}" for typed, safe in renames.items())
    return (f"Patch names were made mesh-safe ({pairs}): a mesh cannot have spaces or symbols "
            "in a boundary name. Use these spellings from now on and tell the user once, in "
            "passing - do not ask them to retype anything.")


_REASON_TEXT = {
    "spelling": "a mesh cannot have spaces or symbols in a boundary name",
    "reserved": "the mesher already uses that name for a boundary of its own",
    "duplicate": "two boundaries cannot share a name, whatever the capitals",
    "ground": ("the ground under a body in an external flow is the floor of the domain, and that "
               "floor is always called ground"),
}


def declaration_note(changes: DeclarationChanges) -> str:
    """Plain sentences for the model to pass on once: each rename with its reason, and each type
    read as the role it means. Empty when nothing changed."""
    parts: list[str] = []
    by_reason: dict[str, list[Rename]] = {}
    for r in changes.renames:
        by_reason.setdefault(r.reason, []).append(r)
    spelled = by_reason.pop("spelling", [])
    if spelled:
        parts.append(rename_note({r.typed: r.name for r in spelled}))
    for reason, items in by_reason.items():
        pairs = ", ".join(f"'{r.typed}' is now {r.name}" for r in items)
        parts.append(f"Patch names changed ({pairs}): {_REASON_TEXT.get(reason, reason)}. Use "
                     "these spellings from now on and tell the user once, in passing.")
    if changes.retypes:
        pairs = ", ".join(f"{r.name or 'a patch'} typed '{r.typed}' is a {r.role}"
                          for r in changes.retypes)
        parts.append(f"Boundary types were read as the kinds the mesher knows ({pairs}). Use "
                     "these types from now on; tell the user only if a reading could surprise "
                     "them.")
    parts.extend(changes.notes)
    return "\n".join(parts)


__all__ = ["ALL_ROLES", "GROUND_WORDS", "MAX_NAME_LENGTH", "MESH_SAFE_NAME", "RESERVED_PATCH_NAMES",
           "RESERVED_SUFFIX", "DeclarationChanges", "GroundRule", "Rename", "Retype",
           "canonical_role", "declaration_note", "is_ground_word", "is_mesh_safe", "is_reserved",
           "mesh_safe", "normalize_declaration", "normalize_patches", "rename_note",
           "role_candidates", "role_hint", "same_patch_name", "unreserved"]
