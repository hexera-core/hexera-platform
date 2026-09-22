# Responsibility: Draw the pictures the geometry check is judged on - the part from enough angles
# that every proposed opening is seen, each opening wearing a numbered sticker, and one labelled
# overview for the user.
# Boundaries: pyvista only. It reads a skin (STL) and a list of openings; it decides nothing about
# them and calls no model.
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

WINDOW = (1024, 768)
BODY = "#B8C4D6"
STICKER = "#FFD400"
#: Past this many openings the per-opening close-ups stop; the global views and the overview
#: still show every sticker, and the count is stated to the model and the user.
MAX_CLOSEUPS = 8


@dataclass(frozen=True)
class Snapshot:
    name: str                 # "overview", "iso", "top", "front", "side", "opening-3"
    path: Path
    direction: tuple[float, float, float]   # where the camera looks (unit vector)
    facing: list[int]         # opening ids whose mouth faces this camera


def _unit(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def render_snapshots(skin_stl, openings: list[dict], out_dir, *, part_label: str = "") -> list[Snapshot]:
    """Pictures of the skin with a numbered sticker on every opening.

    `openings` are the scout's dicts (id, name, centroid_m, normal). Global views first (an
    overview with names, then iso/top/front/side with numbers only), then one close-up per
    opening looking straight into its mouth, up to MAX_CLOSEUPS."""
    import numpy as np
    import pyvista as pv

    pv.OFF_SCREEN = True
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    skin = pv.read(str(skin_stl))
    if skin.n_points == 0:
        raise ValueError("the part's skin has no surface to draw")
    bounds = skin.bounds
    diag = math.sqrt((bounds[1] - bounds[0]) ** 2 + (bounds[3] - bounds[2]) ** 2 + (bounds[5] - bounds[4]) ** 2) or 1.0
    centre = skin.center
    pts = np.asarray([o["centroid_m"] for o in openings], dtype=float).reshape(-1, 3)
    ids = [int(o["id"]) for o in openings]
    normals = [_unit(o["normal"]) for o in openings]

    def facing(direction) -> list[int]:
        # a mouth faces the camera when its outward normal points back at it
        return [i for i, n in zip(ids, normals) if sum(n[k] * direction[k] for k in range(3)) < -0.2]

    def shoot(name: str, position, direction, labels: list[str], *, translucent: bool = False,
              zoom: float = 1.0, focus=None) -> Snapshot:
        pl = pv.Plotter(off_screen=True, window_size=WINDOW)
        pl.set_background("white")
        pl.add_mesh(skin, color=BODY, smooth_shading=True, opacity=0.35 if translucent else 1.0,
                    specular=0.2)
        for p in pts:
            pl.add_mesh(pv.Sphere(radius=0.012 * diag, center=p), color=STICKER, smooth_shading=True)
        if len(pts):
            # ALWAYS visible: a sticker behind the part still names where its opening is; which
            # stickers actually face the camera is stated per picture (`facing`) instead.
            pl.add_point_labels(pts, labels, font_size=26, bold=True, text_color="black",
                                shape="rounded_rect", shape_color="white", shape_opacity=0.9,
                                point_size=1, always_visible=True, show_points=False)
        pl.camera.position = position
        pl.camera.focal_point = focus if focus is not None else centre
        up = (0.0, 0.0, 1.0) if abs(direction[2]) < 0.9 else (0.0, 1.0, 0.0)
        pl.camera.up = up
        pl.reset_camera()
        pl.camera.zoom(zoom)
        if part_label:
            pl.add_text(part_label, position="upper_left", font_size=12, color="black")
        path = out_dir / f"{name}.png"
        pl.screenshot(str(path))
        pl.close()
        return Snapshot(name=name, path=path, direction=_unit(direction), facing=facing(direction))

    numbers = [str(i) for i in ids]
    named = [f"{i}  {o.get('name') or ''}".strip() for i, o in zip(ids, openings)]
    shots: list[Snapshot] = []
    iso = _unit((-1.0, -1.0, -0.8))
    far = 2.2 * diag
    # THE OVERVIEW: the user's picture - translucent so every sticker shows, names on the stickers
    shots.append(shoot("overview", tuple(centre[k] - iso[k] * far for k in range(3)), iso, named,
                       translucent=True))
    for name, direction in (("iso", iso), ("top", (0.0, 0.0, -1.0)), ("front", (0.0, 1.0, 0.0)),
                            ("side", (-1.0, 0.0, 0.0))):
        position = tuple(centre[k] - direction[k] * far for k in range(3))
        shots.append(shoot(name, position, direction, numbers))
    # CLOSE-UPS: straight into each mouth from outside, framed on the mouth
    for o, n, p in list(zip(openings, normals, pts))[:MAX_CLOSEUPS]:
        direction = tuple(-c for c in n)
        position = tuple(p[k] + n[k] * 0.9 * diag for k in range(3))
        shots.append(shoot(f"opening-{int(o['id'])}", position, direction, numbers, zoom=1.6,
                           focus=tuple(p)))
    return shots
