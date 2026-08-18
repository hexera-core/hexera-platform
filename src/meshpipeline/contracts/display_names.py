# Responsibility: Hold every internal identifier that has a user-facing name, and that name.
# Owns: the words themselves, grouped by the vocabulary they belong to.
# Boundaries: DATA ONLY - no imports, no logic, no formatting.
from __future__ import annotations

from typing import Final

#: Mesh engines. Keys are the registry names (the folder under `engines/`).
MESH_ENGINES: Final[dict[str, str]] = {
    "cfmesh":             "cfMesh",
    "gmsh":               "Gmsh",
    "snappy":             "snappyHexMesh",
    "snappy_multiregion": "snappyHexMesh multi-region",
    "vmtk":               "VMTK",
}

#: Simulation purposes - what the user is trying to find out.
PURPOSES: Final[dict[str, str]] = {
    "structural":              "Structural / FEA",
    "external_cfd":            "External CFD",
    "internal_cfd":            "Internal CFD",
    "conjugate_heat_transfer": "Conjugate heat transfer",
}

#: Input kinds - what the submitted geometry represents.
INPUT_KINDS: Final[dict[str, str]] = {
    "solid-body":     "Solid body",
    "fluid-domain":   "Fluid domain",
    "body-surface":   "Body surface",
    "solid-assembly": "Solid assembly",
    "planar-domain":  "Planar domain",
}

#: Every vocabulary above, by name. A guard walks this to check coverage, so a new vocabulary is
#: regulated by being added here rather than by anyone remembering to test it.
VOCABULARIES: Final[dict[str, dict[str, str]]] = {
    "mesh_engine": MESH_ENGINES,
    "purpose":     PURPOSES,
    "input_kind":  INPUT_KINDS,
}


def display_name(vocabulary: str, key: str) -> str:
    k = str(key or "").strip()
    return VOCABULARIES.get(vocabulary, {}).get(k, k)
