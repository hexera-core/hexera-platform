# Responsibility: The one rule for what a boundary patch may be called, and how a name a person typed becomes one.
# Owns: the mesh-safe name pattern and the normaliser; every layer that names a patch uses these.
"""Mesh-safe patch names.

A person names a boundary however they like: "car wall", "Inlet-1", "2nd outlet", "Einlass". The
meshers do not: an OpenFOAM patch is a word (letters, digits, underscores, no spaces), gmsh physical
names follow the same rule in practice, and a name the mesher cannot write is silently replaced by
one it can. The manifest check then looks for the name the user approved, finds zero faces, and a
good mesh fails as "did not meet the required quality checks" (job ac1daa3e, "car wall", 26 minutes).

So a name is made mesh-safe ONCE, where it enters - before the admission preview, the preview
token, the approved snapshot, the builder payload and the manifest check ever see it - and every
one of those reads the same spelling. The person is told the new spelling once; nobody is asked
to retype a name.
"""
from __future__ import annotations

import re

#: A mesh-safe patch name: starts with a letter, then letters, digits and underscores.
MESH_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
MAX_NAME_LENGTH = 64
_NOT_A_WORD_CHAR = re.compile(r"[^A-Za-z0-9_]+")


def is_mesh_safe(name: object) -> bool:
    # fullmatch, not match: `$` also matches before a final newline, so "car\n" would pass.
    return (isinstance(name, str) and len(name) <= MAX_NAME_LENGTH
            and MESH_SAFE_NAME.fullmatch(name) is not None)


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


def normalize_patches(patches: object) -> tuple[object, dict[str, str]]:
    """Make every patch name in a declared patch list mesh-safe and unique.

    Returns the new list and the renames made ({typed: safe}). Two DIFFERENT typed names that
    become the same spelling are told apart with a number ("car wall" and "car-wall" -> car_wall,
    car_wall_2). The SAME typed name twice stays the same name twice, so the duplicate-name check
    downstream still refuses it - cleaning a spelling must never turn a duplicate into a second,
    new boundary. References to other patches (interchangeable_with) follow their renames.
    Anything that is not a list of dicts is returned untouched: the validators report malformed
    input, not this."""
    if not isinstance(patches, list):
        return patches, {}
    renames: dict[str, str] = {}
    spelled: dict[str, str] = {}          # typed name -> the safe spelling it was given
    owner: dict[str, str] = {}            # safe spelling -> the typed name that owns it
    out: list = []
    for p in patches:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not p["name"].strip():
            out.append(p)
            continue
        typed = p["name"].strip()
        if typed in spelled:              # an exact duplicate: keep it one, for the validator
            out.append({**p, "name": spelled[typed]})
            continue
        safe = mesh_safe(typed)
        base, n = safe, 2
        while safe in owner:
            suffix = f"_{n}"
            safe = f"{base[:MAX_NAME_LENGTH - len(suffix)]}{suffix}"
            n += 1
        owner[safe] = typed
        spelled[typed] = safe
        if safe != typed:
            renames[typed] = safe
        out.append({**p, "name": safe})
    if renames:
        for i, p in enumerate(out):
            if isinstance(p, dict) and isinstance(p.get("interchangeable_with"), list):
                out[i] = {**p, "interchangeable_with": [
                    renames.get(str(o).strip(), mesh_safe(o)) for o in p["interchangeable_with"]]}
    return out, renames


def rename_note(renames: dict[str, str]) -> str:
    """One plain sentence for the person, naming each change."""
    if not renames:
        return ""
    pairs = ", ".join(f"'{typed}' is now {safe}" for typed, safe in renames.items())
    return (f"Patch names were made mesh-safe ({pairs}): a mesh cannot have spaces or symbols "
            "in a boundary name. Use these spellings from now on and tell the user once, in "
            "passing - do not ask them to retype anything.")


__all__ = ["MAX_NAME_LENGTH", "MESH_SAFE_NAME", "is_mesh_safe", "mesh_safe", "normalize_patches",
           "rename_note"]
