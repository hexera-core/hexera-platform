# Responsibility: Hold the mesh-quality facts that belong to OpenFOAM rather than to any one mesher.
# Boundaries: shared constants only - the two OpenFOAM-family engines read them so a solver limit is stated once.
from __future__ import annotations

from meshpipeline.engines.review_types import Criterion

#: Verified citations, curated by hand.
OF_MESH_VALIDITY = "https://doc.cfd.direct/openfoam/user-guide-v12/mesh-description"
OF_SNAPPY_GUIDE = "https://doc.cfd.direct/openfoam/user-guide-v12/snappyhexmesh"

FATAL_TOPOLOGY = Criterion(
    key="fatal", label="No fatal topology defects", op="empty", threshold=None, gating=True,
    rationale=(
        "checkMesh's fatal classes - negative-volume cells, open cells, incorrectly "
        "oriented faces - violate the mesh validity constraints every OpenFOAM mesh "
        "must satisfy (cells geometrically and topologically closed, face area "
        "vectors pointing outward). No solver or scheme can integrate over them."),
    evidence_url=OF_MESH_VALIDITY,
)

MAX_NON_ORTHO = Criterion(
    key="max_non_ortho", label="Max face non-orthogonality within solver tolerance",
    op="<=", threshold=65.0, gating=False,
    rationale=(
        "65° is the standard meshQualityControls bar (maxNonOrtho 65): above it, "
        "gradient/laplacian discretisation degrades and needs non-orthogonal "
        "correctors. Advisory - the FV solvability gate is the hard check."),
    evidence_url=OF_SNAPPY_GUIDE,
)

__all__ = ["FATAL_TOPOLOGY", "MAX_NON_ORTHO", "OF_MESH_VALIDITY", "OF_SNAPPY_GUIDE"]
