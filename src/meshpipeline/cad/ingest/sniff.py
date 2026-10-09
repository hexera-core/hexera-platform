# Responsibility: Say what a geometry file really is from its bytes, whatever its name claims.
# Owns: the content signatures of every accepted format and of the native CAD formats we can recognise but not read.
# Boundaries: reads the head of the file (and a zip's directory); it parses no geometry and trusts no suffix.
# Collaborates with: contracts/intake_formats.py (the keys it answers in), cad/ingest/upload_check.py and canonical.py.
from __future__ import annotations

import re
import struct
import zipfile
from pathlib import Path

from meshpipeline.cad.ingest.ecxml import looks_like_ecxml

#: How much of a file the signatures look at. Every text format names itself in its first lines.
HEAD_BYTES = 64 * 1024

#: The prefix a native-CAD answer carries: "native:.sldprt" is a SolidWorks file, whatever its name.
NATIVE = "native:"

_UTF8_BOM = b"\xef\xbb\xbf"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_OFF_HEADER = re.compile(rb"^(?:ST)?C?N?4?n?OFF\b")
_FLUENT = re.compile(rb"^\(\s*(?:0|1|2|4|10|12|13|18|39|45|2010|2012|2013)\s")
_ABAQUS = re.compile(rb"(?im)^\s*\*\s*(?:heading|node|element|part|assembly|include|preprint|"
                     rb"instance|nset|elset|material)\b")
_NASTRAN = re.compile(rb"(?im)^(?:GRID[ *,]|BEGIN\s+BULK|CTRIA3[ *,]|CQUAD4[ *,]|CTETRA[ *,]|"
                      rb"CHEXA[ *,]|CPENTA[ *,]|CEND\b|SOL\s+\d)")
_OBJ_VERTEX = re.compile(rb"(?m)^v[ \t]+[-+0-9.]")
_OBJ_FACE = re.compile(rb"(?m)^f[ \t]+[-0-9]")
_STL_FACET = re.compile(rb"(?i)\bfacet\b")
_ACIS_TEXT = re.compile(rb"^\d{3,5} \d+ \d+ \d+\s")


def _binary_stl_count(head: bytes, size: int) -> int | None:
    if size < 84 or len(head) < 84:
        return None
    n = struct.unpack("<I", head[80:84])[0]
    return n if n > 0 and 84 + 50 * n == size else None


def _zip_entry_count(path: Path) -> int | None:
    """How many members the archive's end record declares, read from its last 64 KiB - before
    zipfile builds a Python object for every one of them."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - 65_557))
        tail = fh.read()
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or at + 22 > len(tail):
        return None
    return struct.unpack("<H", tail[at + 10:at + 12])[0]


def _zip_kind(path: Path | None) -> str | None:
    if path is None:
        return None
    from meshpipeline.cad.ingest.limits import MAX_ZIP_ENTRIES

    try:
        entries = _zip_entry_count(path)
    except OSError:
        return None
    # a ZIP64 directory says 0xFFFF here; zipfile reads the real count, still bounded below
    if entries is not None and MAX_ZIP_ENTRIES < entries < 0xFFFF:
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            if len(zf.infolist()) > MAX_ZIP_ENTRIES:
                return None
            names = [n.lower() for n in zf.namelist()]
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    if any(n.endswith("3dmodel.model") for n in names) or "[content_types].xml" in names \
            and any(n.startswith("3d/") for n in names):
        return "3mf"
    if "document.xml" in names and any(n.endswith(".brp") for n in names):
        return NATIVE + ".fcstd"
    return None


def _iges_line(text: bytes) -> bool:
    first = text.split(b"\n", 1)[0].rstrip(b"\r")
    return len(first) >= 73 and first[72:73] in (b"S", b"G")


def sniff_bytes(head: bytes, size: int, *, path: Path | None = None) -> str | None:
    """The format key (contracts/intake_formats) the bytes are, a native-CAD answer, or None.

    The binary signatures come first: they are exact. Text formats are recognised by the keyword
    every writer puts first. None means "nothing here says what this is" - the caller decides
    whether the name alone is enough (a weakly marked format such as OBJ or Abaqus) or not."""
    if head[:4] == b"glTF":
        return "glb"
    if head[:4] == b"PK\x03\x04":
        return _zip_kind(path)
    if head[:8] == _OLE_MAGIC:
        # SolidWorks (before 2015), Inventor and Solid Edge store their parts in OLE containers.
        return NATIVE + ".ole"
    if head.startswith(b"3D Geometry File Format "):
        return "3dm"
    if head.startswith(b"ACIS BinaryFile"):
        return NATIVE + ".sab"
    if head.startswith(b"V5_CFV2"):
        return NATIVE + ".catpart"
    if head.startswith(b"#UGC:"):
        return NATIVE + ".prt"
    if _binary_stl_count(head, size) is not None:
        return "stl"
    text = head.lstrip(_UTF8_BOM).lstrip(b" \t\r\n")
    if text.startswith(b"ISO-10303-21"):
        return "step"
    if b"DBRep_DrawableShape" in text[:2048] or text.startswith(b"CASCADE Topology") \
            or text.startswith(b"Open CASCADE Topology"):
        return "brep"
    if text.startswith((b"$MeshFormat", b"$NOD", b"$PhysicalNames")):
        return "msh"
    if text.startswith(b"# vtk DataFile"):
        return "vtk"
    if looks_like_ecxml(head):
        return "ecxml"
    if b"<VTKFile" in text[:2048]:
        tag = text[text.find(b"<VTKFile"):][:400]
        if b'type="PolyData"' in tag:
            return "vtp"
        if b'type="UnstructuredGrid"' in tag:
            return "vtu"
        return None
    if re.match(rb"ply\r?\n", text):
        return "ply"
    if _OFF_HEADER.match(_first_content_line(text)):
        return "off"
    if text.startswith(b"MeshVersionFormatted"):
        return "medit"
    if re.match(rb"(?i)%?\s*NDIME\s*=", _first_content_line(text, comment=b"%")):
        return "su2"
    if text.startswith(b"{") and b'"asset"' in text:
        return "gltf"
    if text.startswith(b"**ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        return NATIVE + ".x_t"
    if re.match(rb"Version \d+(?:\.\d+)? JT", text):
        return NATIVE + ".jt"
    if _FLUENT.match(text):
        return "msh"
    if text[:5].lower() == b"solid" and _STL_FACET.search(text[:4096]):
        return "stl"
    # IGES is fixed-column: its first line is 72 columns (often all blank) then the section letter,
    # so it is read before any whitespace is stripped
    if _iges_line(head.lstrip(_UTF8_BOM)):
        return "iges"
    if _ACIS_TEXT.match(text) and b"ACIS" in text[:512]:
        return NATIVE + ".sat"
    if _ABAQUS.search(text):
        return "abaqus"
    if _NASTRAN.search(text):
        return "nastran"
    if _OBJ_VERTEX.search(text) and (_OBJ_FACE.search(text) or text.count(b"\nv ") >= 3):
        return "obj"
    return None


def _first_content_line(text: bytes, comment: bytes = b"#") -> bytes:
    for line in text.splitlines()[:50]:
        s = line.strip()
        if s and not s.startswith(comment):
            return s
    return b""


def sniff_format(path) -> str | None:
    p = Path(path)
    size = p.stat().st_size
    with p.open("rb") as fh:
        head = fh.read(HEAD_BYTES)
    return sniff_bytes(head, size, path=p)


def native_suffix(sniffed: str | None) -> str | None:
    """The suffix a native-CAD answer stands for, or None for anything else."""
    if sniffed and sniffed.startswith(NATIVE):
        return sniffed[len(NATIVE):]
    return None
