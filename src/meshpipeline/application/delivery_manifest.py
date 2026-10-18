# Responsibility: State what a job delivered, what it was built from, and what the customer is
#                 being told about it - from durable rows, not from a live run's memory.
# Boundaries: it reads artifact rows and the terminal record and composes. It uploads nothing,
#             decides no status, and makes no claim the rows do not support.
# Collaborates with: artifact_policy (what counts as delivery), final_result (the verdict and
#                    lineage), persistence artifact rows (what actually exists).
from __future__ import annotations

from collections.abc import Mapping

#: Bump when the manifest's SHAPE changes in a way a reader has to know about. It is a separate
#: version from FINAL_RESULT_SCHEMA_VERSION because this is a different document with a different
#: audience: the final result is the run's verdict, this is the parcel's contents.
DELIVERY_MANIFEST_VERSION = 1

#: Classes a customer may download. `viewer_data` is deliberately absent - it is what the viewer
#: consumes, not a file anybody asked for, and listing it would put an internal JSON blob in the
#: deliverables panel. This is the same judgement the job route makes when it lists artifacts, in
#: one place so the two cannot drift.
_CUSTOMER_VISIBLE = ("mesh_bundle", "mesh", "repaired_cad", "repair_report")


def _mapping(value) -> dict:
    """A dict from whatever a stored record holds there - {} for anything that is not a mapping.

    An older or hand-edited terminal record must never break a read of what a customer was given.
    """
    return dict(value) if isinstance(value, Mapping) else {}


def _artifact_entry(row) -> dict:
    artifact_class = getattr(row.artifact_type, "value", str(row.artifact_type))
    return {
        "class": artifact_class,
        "logical_key": row.logical_key,
        "storage_key": row.storage_key,
        # THE INTEGRITY CLAIM, carried so a customer or a support engineer can prove the file they
        # hold is the file we delivered. It is the store's own checksum, not a digest we computed
        # over something else - naming it `checksum` rather than `sha256` keeps that honest,
        # because a multipart ETag is not a hash of the content.
        "checksum": row.checksum,
        "size_bytes": int(row.size_bytes or 0),
        "customer_visible": artifact_class in _CUSTOMER_VISIBLE,
        "delivered_at": row.created_at.isoformat() if row.created_at else None,
        "attempt": int(row.delivery_attempt or 0),
        "execution_generation": int(row.execution_generation or 0),
    }


def build(*, job_id: str, artifacts, final_result: Mapping | None = None) -> dict:
    """The manifest for one job: every artifact row, the verdict, and the lineage behind it.

    BUILT FROM ROWS, NOT FROM A RUN. By the time a customer asks what they were given, the
    workspace is long gone and the run's state with it - so a manifest derived from anything but
    the durable rows would be a claim nobody can check. Everything here is readable again a year
    later from the same two sources.
    """
    fr = dict(final_result or {})
    entries = sorted((_artifact_entry(a) for a in (artifacts or [])),
                     key=lambda e: (e["class"], e["logical_key"]))

    _raw_lineage = fr.get("repair_lineage")
    lineage: dict = dict(_raw_lineage) if isinstance(_raw_lineage, Mapping) else {}
    repaired = bool(lineage)

    return {
        "manifest_version": DELIVERY_MANIFEST_VERSION,
        "job_id": str(job_id),
        "artifacts": entries,
        "customer_visible": [e for e in entries if e["customer_visible"]],
        # WHAT WAS PROMISED AND WHETHER IT ARRIVED, taken from the terminal record rather than
        # re-derived here: two authorities on whether a job delivered is one too many.
        "required_ready": bool(fr.get("required_ready", False)),
        "delivered_classes": sorted({e["class"] for e in entries}),
        "missing_outputs": list(fr.get("missing_outputs") or []),
        "status": fr.get("status", ""),
        "outcome_code": fr.get("outcome_code", ""),
        # EVERY CAVEAT THE RESULT CARRIES, in the manifest too. A customer reading what they were
        # given must not have to find the caveats somewhere else: a mesh delivered with a stated
        # near-miss is a different thing from one delivered without, and the parcel should say so.
        "requirement_caveats": list(fr.get("requirement_caveats") or []),
        "optional_warnings": list(fr.get("optional_warnings") or []),
        # WHICH BYTES THIS WAS BUILT FROM. "We fixed your file and meshed the fix" is a different
        # claim from "we meshed your file", and the manifest is where a customer sees which.
        "built_from_repaired_geometry": repaired,
        "repair_lineage": dict(lineage),
        "source_identity": _mapping(lineage.get("original")) if repaired else {},
    }


def evidence_gaps(parcel: Mapping) -> list[str]:
    """What is missing before this parcel should be handed over. Empty means nothing is.

    SEPARATE FROM `required_ready`, which answers "did the mesh arrive". This answers "can we
    stand behind it": a delivery whose repaired geometry is not downloadable, or whose repair left
    no report to explain itself, is incomplete as a SERVICE even when the mesh is present.
    """
    # The parameter is `parcel`, not `manifest`, on purpose. A key read off a variable called
    # `manifest` is how the MESH manifest is read, and the data-contract suite checks every one of
    # those against that document's registered vocabulary. This is a different document with
    # different keys, so it is named differently rather than quietly borrowing a name whose keys
    # mean something else.
    gaps: list[str] = []
    classes = set(parcel.get("delivered_classes") or [])
    if not parcel.get("required_ready"):
        gaps.append("the required mesh deliverables are not all ready")
    if parcel.get("built_from_repaired_geometry"):
        if "repaired_cad" not in classes:
            gaps.append("this mesh was built from repaired geometry the customer cannot download")
        if "repair_report" not in classes:
            gaps.append("geometry was repaired but no report explains what changed")
        if not parcel.get("source_identity"):
            gaps.append("the repaired delivery does not identify the original upload")
    return gaps
