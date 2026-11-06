# Responsibility: Draw the pictures the FEA size reading is judged on: the whole model from three sides with every part in its own colour and its number on it, and sheets of each part alone with its number - so the model can tie "the brackets" to parts by shape when the CAD names say nothing.
# Boundaries: pyvista only, off screen. It reads the measured triangle surface (cad/fea_features.Surface) and the part ids; it decides nothing and calls no model.
# Collaborates with: engines/fea_part_sizes.py (asks the vision model with these), render/scout_snapshots.py (the same camera conventions).
from __future__ import annotations

import math
from pathlib import Path

WINDOW = (1024, 768)
#: part colours, distinct and muted, cycled past the last
PALETTE = ("#4E79A7", "#F28E2B", "#59A14F", "#E15759", "#B07AA1", "#76B7B2", "#EDC948", "#FF9DA7",
           "#9C755F", "#BAB0AC", "#2F4B7C", "#A05195", "#665191", "#D45087", "#F95D6A", "#FFA600",
           "#3D9970", "#85144B", "#0074D9", "#B10DC9")
#: the overviews: where each camera looks from (towards the model's centre)
VIEWS = {"parts_iso": (-1.0, -1.0, -0.8), "parts_top": (0.0, 0.0, -1.0), "parts_front": (0.0, 1.0, 0.0)}
#: parts per sheet of single parts, and the most sheets drawn
PER_SHEET = 20
MAX_SHEETS = 2


def _num(pid: str) -> str:
    return str(pid).rsplit("_", 1)[-1]


def render_parts(surface, part_ids: list[str], out_dir) -> list[Path]:
    """The overview pictures, then the sheets of single parts; returns their paths in that order."""
    import numpy as np
    import pyvista as pv

    pv.OFF_SCREEN = True
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    xyz = np.asarray(surface.xyz, dtype=float)
    tris = np.asarray(surface.tris, dtype=np.int64).reshape(-1, 3)
    names = list(surface.names)
    label = np.asarray(surface.label, dtype=np.int64)
    if not len(tris):
        return []

    def part_of(name: str) -> str:
        head, sep, _ = str(name).rpartition("_Face_")
        return head if sep else "Part_1"

    order = {p: k for k, p in enumerate(part_ids)}
    lab_part = np.array([order.get(part_of(n), -1) for n in names] + [-1])
    tri_part = lab_part[np.where((label >= 0) & (label < len(names)), label, len(names))]
    cells = np.hstack([np.full((len(tris), 1), 3), tris]).ravel()
    whole = pv.PolyData(xyz, cells)
    whole.cell_data["part"] = tri_part
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    centre = (lo + hi) / 2.0
    diag = float(np.linalg.norm(hi - lo)) or 1.0
    pieces = {}
    for p, k in order.items():
        sel = np.flatnonzero(tri_part == k)
        if len(sel):
            pieces[p] = whole.extract_cells(sel).extract_surface(algorithm="dataset_surface")  # type: ignore[arg-type]
    paths: list[Path] = []
    for name, d in VIEWS.items():
        pl = pv.Plotter(off_screen=True, window_size=list(WINDOW))
        pl.set_background("white")  # type: ignore[arg-type]
        pts, labels = [], []
        for p, mesh in pieces.items():
            pl.add_mesh(mesh, color=PALETTE[order[p] % len(PALETTE)], smooth_shading=True, specular=0.2)
            pts.append(np.asarray(mesh.center))
            labels.append(_num(p))
        if pts:
            pl.add_point_labels(np.asarray(pts), labels, font_size=22, bold=True, text_color="black",
                                shape="rounded_rect", shape_color="white", shape_opacity=0.85, point_size=1,
                                always_visible=True, show_points=False)
        dv = np.asarray(d, dtype=float) / np.linalg.norm(d)
        pl.camera.position = tuple(centre - dv * 2.2 * diag)
        pl.camera.focal_point = tuple(centre)
        pl.camera.up = (0.0, 0.0, 1.0) if abs(dv[2]) < 0.9 else (0.0, 1.0, 0.0)
        pl.reset_camera()  # type: ignore[call-arg]
        pl.add_text(f"whole model, {name.split('_')[1]} view: each part its own colour and number",
                    position="upper_left", font_size=11, color="black")
        path = out / f"{name}.png"
        pl.screenshot(str(path))
        pl.close()
        paths.append(path)
    ids = list(pieces)
    for s in range(min(MAX_SHEETS, math.ceil(len(ids) / PER_SHEET))):
        chunk = ids[s * PER_SHEET:(s + 1) * PER_SHEET]
        cols = min(5, len(chunk))
        rows = math.ceil(len(chunk) / cols)
        pl = pv.Plotter(off_screen=True, window_size=[260 * cols, 240 * rows], shape=(rows, cols), border=True)
        for i, p in enumerate(chunk):
            pl.subplot(i // cols, i % cols)
            pl.set_background("white")  # type: ignore[arg-type]
            pl.add_mesh(pieces[p], color=PALETTE[order[p] % len(PALETTE)], smooth_shading=True, specular=0.2)
            pl.camera.position = tuple(np.asarray(pieces[p].center) + np.array([-1.0, -1.0, 0.8]) * 3.0
                                       * max(float(pieces[p].length), 1e-9))
            pl.camera.focal_point = tuple(pieces[p].center)
            pl.camera.up = (0.0, 0.0, 1.0)
            pl.reset_camera()  # type: ignore[call-arg]
            pl.add_text(f"{_num(p)}  ({float(pieces[p].length):.3g} mm across)", position="upper_left",
                        font_size=10, color="black")
        path = out / f"parts_alone_{s + 1}.png"
        pl.screenshot(str(path))
        pl.close()
        paths.append(path)
    return paths


__all__ = ["render_parts"]
