# Responsibility: Refuse an OpenFOAM case that would execute code while being parsed.
# Boundaries: a scan before any mesher starts: a dictionary is data, and parse-time directives make it a program.
from __future__ import annotations

import re
from pathlib import Path

# rc the runners return when this guard refuses a case - a deterministic SAFE failure, never a mesh
# result and never native success (adapters/runners map it to a rejection; no subprocess is run).
FOAM_GUARD_REJECT_RC = -2

# Files OpenFOAM does NOT dictionary-parse: geometry + mesh DATA (often binary). Scanning these for
# text directives is meaningless and failing closed on their (legitimate) binary content would break
# every real case, so they are skipped. The `#include` vector lives only in DICTIONARIES.
_DATA_SUFFIXES = frozenset({".stl", ".obj", ".eMesh", ".fms", ".ftr", ".nas", ".vtk", ".vtp", ".vtu",
                            ".gz", ".png", ".jpg"})
_DATA_DIRS = frozenset({"polyMesh", "extendedFeatureEdgeMesh"})

# active-directive lexical patterns (applied AFTER comment stripping).
#   any preprocessor directive: '#', optional whitespace, an identifier.
_HASH_DIRECTIVE = re.compile(r"#\s*[A-Za-z]")
#   library / dynamic-code loading keywords, as whole dictionary keywords.
_LIB_DIRECTIVE = re.compile(r"(?<![A-Za-z0-9_])(libs|dynamicCode|codedFixedValue|codedSource)(?![A-Za-z0-9_])")

_MAX_BYTES = 8 * 1024 * 1024   # bounded read - an authored dict far larger than this is itself suspect

#: Above _MAX_BYTES the file is scanned in chunks instead of being refused, with a hard ceiling so
#: the read stays bounded whatever is on disk. 4 GiB is a cellToRegion for roughly four hundred
#: million cells, which is past anything this platform will mesh.
_HUGE_BYTES = 4 * 1024 ** 3
#: Bytes carried from one chunk to the next, so a directive straddling a boundary is still seen.
#: A directive is a few characters; a kilobyte is four orders of magnitude of margin.
_OVERLAP = 1024


#: FoamFile classes that hold DATA rather than a dictionary: one entry per cell, point or face.
#: `cellToRegion` is a labelList, which is the one that stopped a 5.7M-cell build.
_FIELD_CLASS = re.compile(r"\bclass\s+[A-Za-z]*(?:List|Field)\s*;")
#: How much of a file is read to find its FoamFile header. The header is the first thing in it.
_HEADER_BYTES = 4096


def _is_field_data(f: Path) -> bool:
    """Whether the file says, in its own header, that it holds data rather than a dictionary.

    READ FROM THE FILE, NOT FROM ITS NAME. A name list would have to be kept in step with every
    OpenFOAM tool that writes into constant/; the FoamFile header is written by the tool itself and
    says `class labelList` where a dictionary says `class dictionary`.
    """
    try:
        with f.open("rb") as fh:
            head = fh.read(_HEADER_BYTES)
    except OSError:
        return False
    return bool(_FIELD_CLASS.search(head.decode("utf-8", "replace")))


def _scan_big(f: Path, rel: Path) -> str | None:
    """A file too large to hold in memory, scanned in chunks for the same two directives.

    WHY THIS EXISTS RATHER THAN A BIGGER CAP. The cap is right about AUTHORED dictionaries: one
    larger than 8 MiB is itself suspect, and refusing it is the correct answer. But OpenFOAM's own
    tools write FIELD data into constant/ that is one number per cell and carries no suffix and no
    polyMesh/ parent - `cellToRegion`, which checkMesh and splitMeshRegions both write - so it fell
    to the authored-dictionary rule and the whole case was refused for being big. Measured on
    mesh-bench 2026-10-28: a 5,686,631-cell conjugate build died on
    "constant/fluid/cellToRegion is larger than the 8388608 byte scan cap - refusing", the solid
    region came back unreadable, and the run reported "solid: no cells". Any mesh past about a
    million cells hit this, on every engine, not just this one.

    NO COMMENT STRIPPING HERE, deliberately. Across a chunk boundary a block comment cannot be
    tracked without carrying parser state, and getting that wrong HIDES a directive rather than
    reporting a spurious one. So the raw bytes are searched: a directive written inside a comment
    is reported, which is a false alarm in the safe direction and has no cost on field data, which
    carries no comments at all.
    """
    try:
        with f.open("rb") as fh:
            carry = b""
            while True:
                chunk = fh.read(_MAX_BYTES)
                if not chunk:
                    return None
                if b"\x00" in chunk:
                    return f"{rel} is binary where a dictionary is expected - refusing"
                window = (carry + chunk).decode("utf-8", "replace")
                if _HASH_DIRECTIVE.search(window):
                    return (f"{rel} contains an active OpenFOAM preprocessor directive (#...) - "
                            "code/include directives are not allowed in authored case dictionaries")
                m = _LIB_DIRECTIVE.search(window)
                if m:
                    return (f"{rel} contains an active '{m.group(1)}' directive - dynamic "
                            "library/code loading is not allowed in authored case dictionaries")
                carry = chunk[-_OVERLAP:]
    except OSError:
        return f"{rel} could not be read - refusing"


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _is_data_file(rel: Path) -> bool:
    if rel.suffix in _DATA_SUFFIXES:
        return True
    return any(part in _DATA_DIRS for part in rel.parts)


def _scan_one(f: Path, ws: Path) -> str | None:
    rel = f.relative_to(ws)
    # symlink escape: a link in a dict tree that resolves outside the workspace could redirect a
    # native read (or an #include target) outside our confinement - refuse it.
    try:
        real = f.resolve()
        if not real.is_relative_to(ws.resolve()):
            return f"{rel} resolves outside the workspace - refusing (possible symlink escape)"
    except (OSError, RuntimeError):
        return f"{rel} could not be resolved safely - refusing"
    if _is_data_file(rel):
        return None                      # geometry/mesh DATA, not a dictionary - not an injection vector
    try:
        size = f.stat().st_size
        if size > _HUGE_BYTES:
            return (f"{rel} is {size} bytes, past the {_HUGE_BYTES} byte ceiling this guard will "
                    "read - refusing")
        if size > _MAX_BYTES:
            # AN AUTHORED DICTIONARY THIS BIG IS STILL REFUSED. Only a file whose own FoamFile
            # header declares a data CLASS is read in chunks instead - that header is what tells a
            # list of numbers from a dictionary, and it is the file's own word rather than a guess
            # from its name. See _is_field_data and _scan_big.
            if not _is_field_data(f):
                return f"{rel} is larger than the {_MAX_BYTES} byte scan cap - refusing"
            return _scan_big(f, rel)
        raw = f.read_bytes()
    except OSError:
        return f"{rel} could not be read - refusing"
    # fail CLOSED: an authored dictionary that is not valid UTF-8 text (embedded NULs / binary) is
    # not something a renderer produces, and we will not hand it unscanned to the parser.
    if b"\x00" in raw:
        return f"{rel} is binary where a dictionary is expected - refusing"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"{rel} is not decodable text where a dictionary is expected - refusing"
    body = _strip_comments(text)
    if _HASH_DIRECTIVE.search(body):
        return (f"{rel} contains an active OpenFOAM preprocessor directive (#…) - code/include "
                "directives are not allowed in authored case dictionaries")
    m = _LIB_DIRECTIVE.search(body)
    if m:
        return (f"{rel} contains an active '{m.group(1)}' directive - dynamic library/code loading "
                "is not allowed in authored case dictionaries")
    return None


def scan_case_dicts(workspace) -> str | None:
    ws = Path(workspace)
    for root in ("system", "constant"):
        d = ws / root
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            # a symlinked DIRECTORY that escapes is caught when we resolve its files; skip plain dirs
            if f.is_dir() and not f.is_symlink():
                continue
            if f.is_dir():
                # symlinked directory - check it does not escape
                try:
                    if not f.resolve().is_relative_to(ws.resolve()):
                        return f"{f.relative_to(ws)} is a symlinked directory resolving outside the workspace - refusing"
                except (OSError, RuntimeError):
                    return f"{f.relative_to(ws)} could not be resolved safely - refusing"
                continue
            reason = _scan_one(f, ws)
            if reason:
                return reason
    return None
