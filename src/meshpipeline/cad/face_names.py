# Responsibility: Read the names a STEP file gives its individual faces and tie each one to the face of the staged B-rep it describes.
# Owns: the per-face name read (OCC's XDE reader with sub-shape names), the geometric match, and the cad_face_names.json workspace record.
# Boundaries: it reads and matches; it meshes nothing and names no group. Best effort: a file without face names, or one it cannot read, gives no names and never stops a run.
# Collaborates with: engines/gmsh/gmsh_runner.py (writes the record while staging geometry.step), engines/gmsh/driver.py (reads it back for the deck), engines/gmsh/abaqus_deck.py (writes the names).
"""READABLE CAD FACE NAMES.

Some CAD systems export a name per face ("Mounting face", "Bore A") on the STEP ADVANCED_FACE.
Every face reaches the FEA deck as Face_k; where the file named a face, its name is written
beside it. The staged geometry.step is rewritten by gmsh, which drops those names, so they are
read from the uploaded file here and recorded by WHERE the face is (its centroid and area in
metres): the driver ties each record to its own face by the same two numbers, whatever tag
numbering the re-read gives.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: The workspace record: [{"name", "centroid" [x, y, z] m, "area" m^2}, ...]
RECORD = "cad_face_names.json"
#: A face matches a record when its centroid lies within this fraction of the part's size and
#: its area within this fraction of the record's.
_CENTROID_TOL = 1e-4
_AREA_TOL = 1e-3
#: A STEP face entity with a non-empty name: ADVANCED_FACE('Bore A', ...)
_NAMED_FACE = re.compile(rb"ADVANCED_FACE\s*\(\s*'[^']")

#: (centre, size) of a set of faces - see frame_of
Frame = tuple[np.ndarray, float]
_NO_FRAME: Frame = (np.zeros(3), 0.0)


def frame_of(faces) -> Frame:
    """Where a set of (.., centroid, area) faces sits and how big it is: the area-weighted centre
    of all of them and the square root of their total area. Intrinsic to the shape, unlike an OCC
    bounding box (loose on curved faces), so two readings in different units share it."""
    if not faces:
        return _NO_FRAME
    c = np.array([f[1] for f in faces], dtype=float)
    a = np.array([f[2] for f in faces], dtype=float)
    total = float(a.sum())
    if total <= 0:
        return _NO_FRAME
    return (a[:, None] * c).sum(axis=0) / total, total ** 0.5


def read_named_faces(path: Path | str) -> tuple[list[tuple[str, np.ndarray, float]], Frame]:
    """(name, centroid, area) of every face the STEP file names, in the reader's own length unit,
    and the frame (frame_of) of ALL its faces. Empty for any other file."""
    p = Path(path)
    if p.suffix.lower() not in (".step", ".stp"):
        return [], _NO_FRAME
    # most files name no face: a byte scan says so in well under a second, where a second full
    # XDE read of a large assembly would cost a minute of staging
    if not _NAMED_FACE.search(p.read_bytes()):
        return [], _NO_FRAME
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("cad"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    # set AFTER the reader exists: constructing it resets the STEP parameters to their defaults
    Interface_Static.SetIVal_s("read.stepcaf.subshapes.name", 1)
    if reader.ReadFile(str(p)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        return [], _NO_FRAME
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def named(label) -> str:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            return str(attr.Get().ToExtString())
        return ""

    def measured(shape) -> tuple[np.ndarray, float]:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, props)
        c = props.CentreOfMass()
        return np.array([c.X(), c.Y(), c.Z()]), float(props.Mass())

    every: list[tuple[str, np.ndarray, float]] = []
    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        explorer = TopExp_Explorer(tool.GetShape_s(roots.Value(i)), TopAbs_FACE)
        while explorer.More():
            every.append(("", *measured(explorer.Current())))
            explorer.Next()
    labels = TDF_LabelSequence()
    tool.GetShapes(labels)
    out: list[tuple[str, np.ndarray, float]] = []
    for i in range(1, labels.Length() + 1):
        subs = TDF_LabelSequence()
        tool.GetSubShapes_s(labels.Value(i), subs)
        for j in range(1, subs.Length() + 1):
            sub = subs.Value(j)
            shape = tool.GetShape_s(sub)
            name = named(sub).strip()
            if not name or shape.IsNull() or shape.ShapeType() != TopAbs_FACE:
                continue
            out.append((name, *measured(shape)))
    return out, frame_of(every)


def match_faces(named: list[tuple[str, np.ndarray, float]], named_frame: Frame,
                faces: list[tuple[int, np.ndarray, float]], faces_frame: Frame) -> dict[int, str]:
    """Tie named faces to ``faces`` (tag, centroid, area) by position and area, each read in its
    own unit: both are taken relative to their own frame (frame_of), so a file read in
    millimetres matches a model staged in metres. A face two different names claim, or a record
    that fits no face, is left unnamed."""
    if not named or not faces or named_frame[1] <= 0 or faces_frame[1] <= 0:
        return {}
    from scipy.spatial import cKDTree

    lo_n, d_n = named_frame
    lo_f, d_f = faces_frame
    fc = np.array([(c - lo_f) / d_f for _, c, _ in faces])
    fa = np.array([a / d_f ** 2 for _, _, a in faces])
    tree = cKDTree(fc)
    claims: dict[int, list[str]] = {}
    for name, c, a in named:
        an = a / d_n ** 2
        for k in tree.query_ball_point((np.asarray(c) - lo_n) / d_n, _CENTROID_TOL):
            if abs(fa[k] - an) <= _AREA_TOL * max(an, 1e-300):
                claims.setdefault(int(faces[k][0]), []).append(name)
    return {tag: names[0] for tag, names in claims.items() if len(set(names)) == 1}


def gmsh_faces(gmsh) -> tuple[list[tuple[int, np.ndarray, float]], Frame]:
    """(tag, centroid, area) of every face of the current gmsh OCC model, and their frame."""
    out = [(int(t), np.asarray(gmsh.model.occ.getCenterOfMass(2, t), dtype=float),
            float(gmsh.model.occ.getMass(2, t))) for _, t in gmsh.model.getEntities(2)]
    return out, frame_of(out)


def record_for_staging(gmsh, source: Path | str, workspace: Path | str) -> int:
    """Write RECORD for the B-rep just staged from ``source`` (the current gmsh model, in
    metres): one entry per face the file names. Returns how many; 0 writes nothing."""
    target = Path(workspace) / RECORD
    target.unlink(missing_ok=True)
    try:
        named, named_frame = read_named_faces(source)
        if not named:
            return 0
        faces, frame = gmsh_faces(gmsh)
        tags = match_faces(named, named_frame, faces, frame)
    except Exception as exc:  # noqa: BLE001 - names are a courtesy; Face_k still names every face
        logger.info("cad face names: %s not read (%s)", Path(source).name, exc)
        return 0
    by_tag = {t: (c, a) for t, c, a in faces}
    rows = [{"name": n, "centroid": [float(v) for v in by_tag[t][0]], "area": by_tag[t][1]}
            for t, n in sorted(tags.items())]
    if rows:
        target.write_text(json.dumps(rows, indent=1))
    logger.info("cad face names: %d of %d named face(s) tied to the staged B-rep",
                len(rows), len(named))
    return len(rows)


def names_for_model(gmsh, workspace: Path | str) -> dict[int, str]:
    """{face tag: name} for the current gmsh model from the workspace RECORD (both in metres)."""
    try:
        rows = json.loads((Path(workspace) / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, list) or not rows:
        return {}
    faces, frame = gmsh_faces(gmsh)
    named = [(str(r["name"]), np.asarray(r["centroid"], dtype=float), float(r["area"]))
             for r in rows if isinstance(r, dict) and r.get("name")]
    # both sides are this model in metres: one frame for both
    return match_faces(named, frame, faces, frame)
