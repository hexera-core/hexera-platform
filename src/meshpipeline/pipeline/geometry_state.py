# Responsibility: Read the geometry facts a run's state already carries.
# Boundaries: accessors over declared state; it materialises nothing.
from __future__ import annotations

from meshpipeline.contracts.geometry_source import MaterializedGeometry

# ONE way to read the verified geometry out of pipeline state. Consumers that only need bytes call
# `geometry_path(state)` and never learn about object storage, checksums or the catalog; the full
# reference stays reachable for the few places that genuinely own provenance.


def materialized(state) -> MaterializedGeometry | None:
    return MaterializedGeometry.from_state((state or {}).get("geometry"))


def geometry_path(state) -> str:
    mg = materialized(state)
    return mg.path if mg else ""


def geometry_ref(state):
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    payload = ((state or {}).get("geometry") or {}).get("ref")
    return GeometrySourceRef.from_payload(payload) if payload else None
