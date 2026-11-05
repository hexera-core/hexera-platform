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
        if f.stat().st_size > _MAX_BYTES:
            return f"{rel} is larger than the {_MAX_BYTES} byte scan cap - refusing"
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
