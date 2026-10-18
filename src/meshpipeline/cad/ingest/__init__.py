# Responsibility: The one ingestion layer: any accepted geometry file becomes a canonical CAD solid (STEP/IGES) or a canonical triangle surface (STL).
# Owns: nothing itself; it names the entry points of its modules.
# Boundaries: sniff (what a file is), upload_check (may it be uploaded), canonical (convert), readers/cad (how), limits (how much), units (what a format declares).
# Collaborates with: contracts/intake_formats.py, which declares the formats and answers geometry_kind() for everyone downstream.
from __future__ import annotations

from meshpipeline.cad.ingest.canonical import (
    CanonicalGeometry,
    IngestError,
    canonicalise,
    read_sidecar,
    resolve_format,
)
from meshpipeline.cad.ingest.sniff import sniff_format
from meshpipeline.cad.ingest.upload_check import UploadVerdict, check_upload

__all__ = ["CanonicalGeometry", "IngestError", "UploadVerdict", "canonicalise", "check_upload",
           "read_sidecar", "resolve_format", "sniff_format"]
