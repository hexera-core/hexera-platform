# Responsibility: Carry a returned STEP's mesh-size hints (crs-mesh-hints/1) into OUR gmsh mesh: stage them
#                 into the workspace in the staged solid's unit when - and only when - the sidecar names the
#                 exact bytes being meshed, and apply them in the mesher as local size fields.
# Owns: the staged hints file (`mesh_hints.json` in the workspace) and the record of what was applied.
# Boundaries: no sidecar, a sidecar for other bytes, or an unreadable one stages NOTHING, and with nothing staged
#             `apply` touches no gmsh state at all - the mesh is byte for byte what it was. The matching and the
#             size fields are cad/repair/mesh_hints.apply_in_gmsh's (fingerprint mapping, IncludeBoundary=1),
#             the code the meshability eval measured; nothing here re-implements them. No OCC call: the host side
#             only reads JSON and hashes bytes, and the mesher side runs inside gmsh's own mesher process.
# Collaborates with: agents/builder/attempt.py (stages), engines/gmsh/fea_adapter.py and engines/gmsh/driver.py
#                    (apply), pipeline/repair_return.py (where the sidecar is), engines/fea_driver.PAYLOAD (ships it).
"""HINTS IN THE PRODUCTION MESH.

THE UNIT. A sidecar's fingerprints and sizes are in the millimetres OCC reads the delivered STEP in. The
mesher never reads that STEP: it reads the staged solid (geometry.brep / geometry.step), which staging wrote
at `to_metres` times those coordinates (engines/gmsh/gmsh_runner._stage_here, OCCScaling). So the staged copy
carries every length times `to_metres` (areas times its square), and a mesher that reads the staged solid at
another scale again (MESH-4: the CAD's own millimetres) multiplies by that scale too. `mesh_hints.same_face`
judges a match relative to the part's diagonal, so the scaled hints match exactly as the unscaled ones did.

THE SHA. Checked on the HOST against the bytes the run meshes (the materialised geometry): a hint is keyed to a
face index of one exact file, so a sidecar for any other bytes is ignored, never guessed onto this one. A
refusal's passthrough is byte-identical to the upload, so a run that meshes the original after a refusal
matches its passthrough's sidecar.

THE BACKGROUND. `apply_in_gmsh` sets a Min of its own Constant fields as the background mesh. A mesh whose plan
already has a background field (the FEA plan's zones) keeps it: both go under one more Min, so a hint only ever
makes elements SMALLER where it names a face, and everywhere else the plan's sizes stand.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The staged copy, in the workspace (and in the FEA payload: engines/fea_driver.PAYLOAD).
HINTS_FILE = "mesh_hints.json"
#: What the staged copy says it is.
STAGED_SCHEMA = "hexera-staged-mesh-hints/1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _scaled_faces(faces: list, k: float) -> list[dict]:
    out: list[dict] = []
    for row in faces or []:
        try:
            fp = row["fingerprint"]
            c = fp["centroid"]
            out.append({"face": int(row["face"]), "size_mm": float(row["size_mm"]) * k,
                        "fingerprint": {"area_mm2": float(fp["area_mm2"]) * k * k,
                                        "centroid": [float(c[0]) * k, float(c[1]) * k, float(c[2]) * k]}})
        except (KeyError, TypeError, ValueError, IndexError):
            continue   # a malformed row is not staged; apply_in_gmsh would report it unmapped anyway
    return out


def stage(workspace, *, geometry_path, hints_path, to_metres: float) -> dict:
    """Stage the sidecar at `hints_path` for the mesh of `geometry_path`, or remove a stale staged copy.
    Returns what was decided: `{"staged": bool, "why": str, "faces": int}`. NEVER RAISES - hints are advice
    beside a mesh, never a reason to lose one."""
    ws = Path(workspace)
    target = ws / HINTS_FILE
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass
    if not hints_path:
        return {"staged": False, "why": "no sidecar", "faces": 0}
    try:
        from meshpipeline.cad.repair.mesh_hints import read_hints

        doc = read_hints(hints_path)
        named = str((doc.get("step") or {}).get("sha256") or "")
        meshed = _sha256(Path(geometry_path))
        if not named or named != meshed:
            return {"staged": False, "why": "sidecar is for different STEP bytes", "faces": 0}
        k = float(to_metres)
        faces = _scaled_faces(doc.get("faces") or [], k)
        diagonal = float(doc.get("diagonal_mm") or 0.0) * k
        if not faces or diagonal <= 0:
            return {"staged": False, "why": "the sidecar holds no usable hint", "faces": 0}
        staged = {"schema": STAGED_SCHEMA, "source_schema": doc.get("schema"),
                  "source_sha256": meshed, "source_status": doc.get("status"),
                  "to_metres": k, "diagonal": diagonal,
                  # the keys apply_in_gmsh reads; the numbers are in the staged solid's unit
                  "faces": faces}
        target.write_text(json.dumps(staged, indent=1))
        return {"staged": True, "why": "", "faces": len(faces)}
    except Exception as exc:  # noqa: BLE001 - an unreadable sidecar is no hint, never a failed attempt
        logger.warning("staged_hints: the sidecar was not staged (%s: %s)", type(exc).__name__, exc)
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return {"staged": False, "why": f"sidecar unreadable: {type(exc).__name__}", "faces": 0}


def read_staged(workspace) -> dict | None:
    """The staged copy, or None when there is none (or it is not ours)."""
    try:
        doc = json.loads((Path(workspace) / HINTS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == STAGED_SCHEMA else None


def apply(gmsh, workspace, *, scale: float = 1.0, background: int | None = None,
          log_prefix: str = "[GMSH]") -> tuple[int | None, dict | None]:
    """Apply the staged hints to the model gmsh holds now (read at `scale` times the staged unit).

    Returns `(field, record)`: the field to set as the background mesh (the plan's `background` and the
    hints under one Min; `background` itself when no hint mapped) and what was applied - or `(background,
    None)` when nothing is staged, having touched nothing. The caller sets the background as it always did.
    """
    doc = read_staged(workspace)
    if doc is None:
        return background, None
    from meshpipeline.cad.repair.mesh_hints import apply_in_gmsh

    k = float(scale)
    faces = _scaled_faces(doc.get("faces") or [], k)
    diagonal = float(doc.get("diagonal") or 0.0) * k
    record: dict[str, Any] = {"faces": len(faces), "applied": 0, "unmapped": [], "scale": k,
                              "source_sha256": doc.get("source_sha256")}
    try:
        before = set(gmsh.model.mesh.field.list())
        applied, unmapped = apply_in_gmsh(gmsh, faces, diagonal_mm=diagonal)
        record.update(applied=int(applied), unmapped=[int(u) for u in unmapped])
        field = background
        if applied:
            added = sorted(set(gmsh.model.mesh.field.list()) - before)
            hinted = added[-1]          # apply_in_gmsh's Min over its Constant fields: the last it added
            if background is not None:
                field = gmsh.model.mesh.field.add("Min")
                gmsh.model.mesh.field.setNumbers(field, "FieldsList", [int(background), int(hinted)])
            else:
                field = hinted
    except Exception as exc:  # noqa: BLE001 - a hint that cannot be applied never costs the mesh its plan
        record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        field = background
    print(f"{log_prefix} mesh hints: {record['applied']} of {record['faces']} applied, "
          f"{len(record['unmapped'])} unmapped (crs-mesh-hints/1, scale {k:g})"
          + (f" - {record['error']}" if record.get("error") else ""), file=sys.stderr)
    return field, record
