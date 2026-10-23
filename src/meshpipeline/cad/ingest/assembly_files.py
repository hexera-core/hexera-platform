# Responsibility: Turn several CAD files uploaded together into ONE assembly file (STEP), each file's parts placed by their own coordinates and named from the file's own product names, or else from the file's name.
# Owns: the choice of how to combine (the files' own STEP text, joined, when they agree on schema and unit; else OpenCASCADE's reading of each, written as one STEP in millimetres), and the refusal a user reads for a file that cannot join an assembly.
# Boundaries: no scaling of a file's own geometry, no healing, no meshing: every part stays where its file puts it, in its file's own unit. The upload calls it once, before the combined file is stored as the session's one source.
# Collaborates with: api/v1/upload.py (several files in one upload), cad/step_units.py (each file's declared length unit), cad/fea_assembly.py (the part names it reads back: generic product names are replaced by the file's name here).
"""SEVERAL FILES, ONE ASSEMBLY.

An engineer often has an assembly as one file per part (a flange, its bolts, a gasket), each drawn
in the assembly's own coordinates. Uploaded together they become one STEP of several bodies - the
same thing as an assembly exported as one file - and everything after the upload treats it so.

* ALL STEP, one schema family and one length unit: the files' own text is joined (entity numbers
  shifted so none collide): nothing is re-read or re-written, every product name and unit is the
  file's own. A product named only generically ("SOLID", "Part1", a number) takes the file's name.
* ANYTHING ELSE (IGES, BREP, or STEP files in different units): each file is read by OpenCASCADE
  (which converts a STEP or IGES file's unit to millimetres; a BREP states none, so its numbers are
  taken as millimetres, as a BREP upload's are) and the whole is written as one STEP in millimetres,
  each file's shape named after the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: The most files one assembly upload takes.
MAX_FILES = 64
#: What each file may be: an exact CAD solid.
STEP_SUFFIXES = (".step", ".stp")
CAD_SUFFIXES = (*STEP_SUFFIXES, ".iges", ".igs", ".brep", ".brp")

NOT_CAD = ("Several files are joined into one assembly only when each is a CAD solid (STEP, IGES or BREP): "
           "{name} is not. Upload it on its own, or export the whole assembly as one STEP file.")
TOO_MANY = ("An assembly upload takes at most {n} files. Export the assembly as one STEP file and upload that.")

_DATA = re.compile(rb"\bDATA\s*;", re.IGNORECASE)
_ENDSEC = re.compile(rb"\bENDSEC\s*;", re.IGNORECASE)
_SCHEMA = re.compile(rb"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'", re.IGNORECASE)
#: A quoted string (with '' inside it), a comment, or an entity reference: only the last is renumbered.
_TOKEN = re.compile(rb"'(?:[^']|'')*'|/\*.*?\*/|#(\d+)", re.DOTALL)
_PRODUCT = re.compile(rb"(?<![A-Za-z0-9_])PRODUCT\s*\(\s*'((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'")


class AssemblyFilesError(ValueError):
    """Files that cannot be joined into one assembly; the message is a plain sentence for the user."""


@dataclass(frozen=True)
class Member:
    path: Path
    name: str          # the name the part takes when its file names it only generically


def _schema_family(text: bytes) -> str:
    m = _SCHEMA.search(text)
    if not m:
        return ""
    return m.group(1).decode("latin-1").split("{")[0].strip().upper()


def _unit(path: Path) -> tuple:
    from meshpipeline.cad.step_units import declared_lengths

    found = declared_lengths(path)
    return tuple(sorted(round(d.metres, 12) for d in found if d.metres is not None))


def can_join_as_text(members: list[Member]) -> bool:
    """Whether the files' own STEP text can simply be joined: all STEP, one schema family, one length
    unit declared by every file."""
    if not all(m.path.suffix.lower() in STEP_SUFFIXES for m in members):
        return False
    schemas = {_schema_family(m.path.read_bytes()[:20000]) for m in members}
    units = {_unit(m.path) for m in members}
    return len(schemas) == 1 and "" not in schemas and len(units) == 1 and all(len(u) == 1 for u in units)


def _sections(text: bytes) -> tuple[bytes, bytes]:
    """(the header, up to and including DATA;), (the data section's body)."""
    d = _DATA.search(text)
    if not d:
        raise AssemblyFilesError("A STEP file in the upload has no DATA section.")
    e = _ENDSEC.search(text, d.end())
    if not e:
        raise AssemblyFilesError("A STEP file in the upload has no end to its DATA section.")
    return text[:d.end()], text[d.end():e.start()]


def _renumbered(body: bytes, offset: int) -> tuple[bytes, int]:
    """The data section with every entity number shifted by `offset`, and the largest number in it."""
    top = 0

    def shift(m: re.Match) -> bytes:
        nonlocal top
        if m.group(1) is None:
            return m.group(0)
        v = int(m.group(1))
        top = max(top, v)
        return b"#" + str(v + offset).encode()

    return _TOKEN.sub(shift, body), top


def _named(body: bytes, name: str) -> bytes:
    """Every product the file names only generically, named `name` instead."""
    from meshpipeline.cad.fea_assembly import readable_body_name

    safe = name.replace("'", "''").encode("latin-1", "replace")

    def rename(m: re.Match) -> bytes:
        said = m.group(2).decode("latin-1", "replace").replace("''", "'")
        if readable_body_name(said):
            return m.group(0)
        return m.group(0)[:m.start(1) - m.start(0)] + safe + b"','" + safe + b"'"

    return _PRODUCT.sub(rename, body)


def join_text(members: list[Member], dest: Path) -> None:
    """The STEP files' own text, joined into one (see the module text)."""
    out: list[bytes] = []
    offset = 0
    head = None
    for m in members:
        text = m.path.read_bytes()
        h, body = _sections(text)
        if head is None:
            head = re.sub(rb"FILE_NAME\s*\(\s*'(?:[^']|'')*'", b"FILE_NAME('assembly.step'", h, count=1)
        shifted, top = _renumbered(body, offset)
        out.append(_named(shifted, m.name))
        offset += top
    assert head is not None
    dest.write_bytes(head + b"\n" + b"\n".join(part.strip(b"\r\n") for part in out)
                     + b"\nENDSEC;\nEND-ISO-10303-21;\n")


def _read(path: Path):
    suffix = path.suffix.lower()
    if suffix in (".brep", ".brp"):
        from meshpipeline.cad.ingest.cad import read_brep

        return read_brep(path)
    from meshpipeline.cad.scout import read_cad

    return read_cad(path)


def join_occ(members: list[Member], dest: Path) -> None:
    """Each file read by OpenCASCADE, all written as one STEP in millimetres, each file's shape named
    after the file."""
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("assembly"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    from meshpipeline.cad.fea_assembly import match_body_names, read_body_names, solids

    for m in members:
        shape = _read(m.path)
        if shape.IsNull():
            raise AssemblyFilesError(f"{m.path.name} holds no shape to join.")
        # EACH BODY UNDER ITS OWN NAME: the file's product names where it gives them, else the file's
        bodies = solids(shape)
        said: list[str] = []
        if m.path.suffix.lower() in STEP_SUFFIXES and bodies:
            try:
                said = match_body_names(read_body_names(m.path), bodies)
            except Exception:  # noqa: BLE001 - a courtesy: the file's name still names it
                said = []
        if len(bodies) <= 1:
            pieces = [(shape, (said[0] if said else "") or m.name)]
        else:
            pieces = [(b, (said[k] if k < len(said) else "") or f"{m.name} {k + 1}") for k, b in enumerate(bodies)]
        for piece, name in pieces:
            label = tool.AddShape(piece, False)
            TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    if not writer.Transfer(doc, STEPControl_AsIs) or writer.Write(str(dest)) != IFSelect_RetDone:
        raise AssemblyFilesError("The files could not be written as one assembly.")


#: THE GUARD on the OpenCASCADE join, which reads whole CAD models: it runs in a process of its own
#: (the API's never holds them), stopped past these.
JOIN_TIMEOUT_S = 300
JOIN_MEMORY_MB = 4096
TOO_HEAVY = ("The files could not be joined into one assembly here ({why}). Export the assembly as one "
             "STEP file and upload that.")


def join_occ_guarded(members: list[Member], dest: Path, *, timeout_s: float = JOIN_TIMEOUT_S,
                     memory_mb: int = JOIN_MEMORY_MB) -> None:
    """join_occ in a process of its own, killed past `timeout_s` or `memory_mb`: a hard model costs
    that process, never the caller's."""
    import json
    import subprocess
    import sys

    from meshpipeline.sandbox.safe_exec import run_guarded

    spec = json.dumps({"dest": str(dest), "memory_mb": int(memory_mb),
                       "members": [{"path": str(m.path), "name": m.name} for m in members]})
    try:
        proc = run_guarded([sys.executable, "-m", "meshpipeline.cad.ingest.assembly_files", spec],
                           timeout=timeout_s, capture_output=True, text=True)
    except subprocess.TimeoutExpired as exc:
        raise AssemblyFilesError(TOO_HEAVY.format(why=f"it took over {int(timeout_s)} s")) from exc
    if proc.returncode != 0 or not dest.exists():
        said = (proc.stderr or "").strip().splitlines()
        last = said[-1] if said else ""
        if last.startswith("AssemblyFilesError: "):
            raise AssemblyFilesError(last.split(": ", 1)[1])
        why = "it needed more memory than allowed" if "MemoryError" in last or proc.returncode < 0 else \
            "the CAD kernel could not read them"
        raise AssemblyFilesError(TOO_HEAVY.format(why=why))


def _main(argv: list[str]) -> int:
    """The guarded join's own process: the spec is JSON (dest, memory_mb, members)."""
    import json
    import resource
    import sys

    spec = json.loads(argv[0])
    cap = int(spec.get("memory_mb") or JOIN_MEMORY_MB) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    try:
        join_occ([Member(Path(m["path"]), str(m["name"])) for m in spec["members"]], Path(spec["dest"]))
    except AssemblyFilesError as exc:
        print(f"AssemblyFilesError: {exc}", file=sys.stderr)
        return 3
    return 0


def combine(members: list[Member], dest: Path | str, *, guarded: bool = True) -> dict:
    """Join `members` into the STEP file `dest`; returns how ({"files", "method"}). `guarded`: the
    OpenCASCADE join in its own process under a time and memory limit (the upload's way)."""
    dest = Path(dest)
    if len(members) > MAX_FILES:
        raise AssemblyFilesError(TOO_MANY.format(n=MAX_FILES))
    for m in members:
        if m.path.suffix.lower() not in CAD_SUFFIXES:
            raise AssemblyFilesError(NOT_CAD.format(name=m.name))
    if can_join_as_text(members):
        join_text(members, dest)
        method = "text"
    elif guarded:
        join_occ_guarded(members, dest)
        method = "occ"
    else:
        join_occ(members, dest)
        method = "occ"
    return {"files": len(members), "method": method}


__all__ = ["CAD_SUFFIXES", "JOIN_MEMORY_MB", "JOIN_TIMEOUT_S", "MAX_FILES", "NOT_CAD", "TOO_HEAVY", "TOO_MANY",
           "AssemblyFilesError", "Member", "can_join_as_text", "combine", "join_occ", "join_occ_guarded", "join_text"]


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:]))
