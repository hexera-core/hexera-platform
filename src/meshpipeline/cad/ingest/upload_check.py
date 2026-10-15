# Responsibility: Decide, at upload, whether the bytes are a geometry file this product reads - by content, not by name.
# Owns: the content check both upload routes make, the format a mislabelled file is recorded as, and the refusal sentence.
# Boundaries: cheap and bounded - signatures, directories and headers only; the geometry itself is read later, on a worker.
# Collaborates with: api/v1/upload.py and api/v1/upload_direct.py (callers), cad/ingest/sniff.py, cad/ingest/limits.py.
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from meshpipeline.contracts.intake_formats import (
    NATIVE_UNKNOWN_TOOL,
    format_for_key,
    format_for_suffix,
    native_format_for_suffix,
    unsupported_message,
)

STEP_HEADER_REFUSAL = "File does not appear to be a valid STEP file (missing ISO-10303-21 header)."

#: Formats every writer marks unmistakably in their first bytes. A file NAMED as one of these whose
#: bytes say nothing is not that format, and is refused now rather than failing on a worker later.
#: (STL, OBJ, IGES, Abaqus and Nastran are marked weakly or not at all: their names are trusted
#: and the reader has the last word.)
_STRONGLY_MARKED = frozenset({"step", "brep", "vtp", "vtu", "vtk", "ply", "off", "3mf", "glb",
                              "gltf", "msh", "medit", "su2", "3dm", "ecxml"})
#: An ECXML file is read (XML only - no geometry is built) at upload below this size, so a file the
#: schema rules out is told why at once; a larger one is told the same thing by the geometry check.
_ECXML_CHECK_MAX_BYTES = 32 * 1024 * 1024

#: Reading a .3dm at upload to see whether it holds anything readable loads the whole model; above
#: this the check is left to the worker, which reports the same sentence on the geometry stage.
_3DM_CHECK_MAX_BYTES = 64 * 1024 * 1024
#: A .gltf is JSON, possibly with its buffers inlined as base64; read whole only below this.
_GLTF_CHECK_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class UploadVerdict:
    suffix: str          # the suffix the bytes really are: what the source is recorded under
    refusal: str = ""    # non-empty: refuse the upload with this sentence
    status: int = 422

    @property
    def ok(self) -> bool:
        return not self.refusal


def check_upload(path, suffix: str) -> UploadVerdict:
    """The verdict on staged upload bytes named `suffix` (already one the registry accepts)."""
    from meshpipeline.cad.ingest.sniff import native_suffix, sniff_format

    p = Path(path)
    declared = format_for_suffix(suffix)
    sniffed = sniff_format(p)
    native = native_suffix(sniffed)
    if native is not None:
        said = unsupported_message(native) if native_format_for_suffix(native) else \
            NATIVE_UNKNOWN_TOOL
        return UploadVerdict(suffix, said)
    fmt = format_for_key(sniffed) if sniffed else None
    if fmt is None:
        if declared is not None and declared.key == "step":
            # today's wording and status, unchanged
            return UploadVerdict(suffix, STEP_HEADER_REFUSAL, status=400)
        if declared is not None and declared.key in _STRONGLY_MARKED:
            return UploadVerdict(suffix, (
                f"This file is named {suffix} but its contents are not a {declared.label} file. "
                "Export it again from the program that made it, or upload the original."))
        return UploadVerdict(suffix)
    effective = suffix if fmt is declared else fmt.suffixes[0]
    refusal = _format_refusal(p, fmt.key)
    return UploadVerdict(effective, refusal)


def _format_refusal(path: Path, key: str) -> str:
    """The format-specific reasons a well-marked file still cannot be read; "" when none."""
    from meshpipeline.cad.ingest import limits

    if key == "3mf":
        try:
            with zipfile.ZipFile(path) as zf:
                said = limits.zip_refusal(zf)
        except (zipfile.BadZipFile, OSError) as exc:
            said = f"it is not a readable 3MF archive ({str(exc)[:80]})"
        return f"This 3MF file cannot be read: {said}." if said else ""
    if key in ("gltf", "glb"):
        size = path.stat().st_size
        if key == "gltf" and size > _GLTF_CHECK_MAX_BYTES:
            return ""        # read on the worker, which refuses the same things in the same words
        data = path.read_bytes() if key == "gltf" else _glb_head(path)
        return _sentence(limits.gltf_refusal(data))
    if key == "3dm" and path.stat().st_size <= _3DM_CHECK_MAX_BYTES:
        return _3dm_refusal(path)
    if key == "ecxml" and path.stat().st_size <= _ECXML_CHECK_MAX_BYTES:
        return _ecxml_refusal(path)
    return ""


def _ecxml_refusal(path: Path) -> str:
    from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml

    try:
        read_ecxml(path)
    except EcxmlError as exc:
        return _sentence(f"This ECXML file cannot be read: {exc}")
    return ""


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return (text[:1].upper() + text[1:].rstrip(".") + ".") if text else ""


def _glb_head(path: Path) -> bytes:
    """A GLB's header and JSON chunk, without reading its (possibly huge) binary chunk."""
    with path.open("rb") as fh:
        head = fh.read(20)
        if len(head) < 20:
            return head
        length = int.from_bytes(head[12:16], "little")
        return head + fh.read(min(length, 256 * 1024 * 1024))


def _3dm_refusal(path: Path) -> str:
    from meshpipeline.cad.ingest.readers import read_3dm
    from meshpipeline.cad.ingest.surface import SurfaceError

    try:
        read_3dm(path)
    except SurfaceError as exc:
        return _sentence(str(exc))
    except Exception as exc:  # noqa: BLE001 - an unreadable model is the user's to re-export
        return f"This Rhino file could not be read ({str(exc)[:80]})."
    return ""
