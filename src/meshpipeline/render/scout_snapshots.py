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
    up: tuple[float, float, float] = (0.0, 0.0, 1.0)   # which way is up on the picture


def _unit(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def screen_axes(direction, up):
    """(right, up) as world vectors for a camera looking along `direction`: what "the left of the
    picture" and "the top of the picture" mean in the part's own axes."""
    d, u = _unit(direction), _unit(up)
    right = (d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0])
    return _unit(right), u


def signed_axis(v) -> str:
    """The dominant world axis of a vector, as "+x" .. "-z"."""
    k = max(range(3), key=lambda i: abs(v[i]))
    return ("+" if v[k] >= 0 else "-") + "xyz"[k]


def camera_words(direction) -> str:
    """Where a picture looks, in axis words the model can hold against the marker: "-Z (from
    above)" for a top view, "(-0.6, -0.6, -0.5)" for an oblique one."""
    d = _unit(direction)
    k = max(range(3), key=lambda i: abs(d[i]))
    if abs(d[k]) >= 0.9:
        sign = "-" if d[k] < 0 else "+"
        side = {(2, "-"): "from above", (2, "+"): "from below", (0, "-"): "from +X", (0, "+"): "from -X",
                (1, "-"): "from +Y", (1, "+"): "from -Y"}[(k, sign)]
        return f"{sign}{'XYZ'[k]} ({side})"
    return f"({d[0]:.1f}, {d[1]:.1f}, {d[2]:.1f})"


def camera_up(direction) -> tuple[float, float, float]:
    """Which way is up on a picture looking along `direction`: +Z, unless the camera looks along
    Z itself, when +Y is."""
    return (0.0, 0.0, 1.0) if abs(direction[2]) < 0.9 else (0.0, 1.0, 0.0)


#: THE PICTURES OF THE WHOLE PART and where each camera looks, in the order they are drawn. The
#: overview is the iso view, translucent and with the openings' names.
_ISO = _unit((-1.0, -1.0, -0.8))
GLOBAL_VIEWS: dict[str, tuple[float, ...]] = {
    "overview": _ISO, "iso": _ISO, "top": (0.0, 0.0, -1.0), "front": (0.0, 1.0, 0.0),
    "side": (-1.0, 0.0, 0.0), "end": (1.0, 0.0, 0.0)}


def global_view(name: str):
    """(direction, up) of a named picture of the whole part, or None for a close-up - what a
    picture stored before its camera was recorded beside it was drawn with."""
    direction = GLOBAL_VIEWS.get(name)
    return None if direction is None else (tuple(direction), camera_up(direction))


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
        pl = pv.Plotter(off_screen=True, window_size=list(WINDOW))
        # pyvista's decorated methods confuse the type checker; the calls are the documented ones
        pl.set_background("white")  # type: ignore[arg-type]
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
        up = camera_up(direction)
        pl.camera.up = up
        pl.reset_camera()  # type: ignore[call-arg]
        pl.camera.zoom(zoom)
        if part_label:
            pl.add_text(part_label, position="upper_left", font_size=12, color="black")
        # THE AXIS MARKER: red +X, green +Y, blue +Z in the corner, drawn by the same camera as the
        # part, so "the nose points along the red arrow" can be read straight off the picture -
        # without it the model cannot turn "faces left" into "+X" and answers unknown.
        pl.add_axes(line_width=4, xlabel="X", ylabel="Y", zlabel="Z", viewport=(0.0, 0.0, 0.28, 0.28))  # type: ignore[call-arg]
        pl.add_text(f"camera looks along {camera_words(direction)}", position="lower_right",
                    font_size=10, color="black")
        path = out_dir / f"{name}.png"
        pl.screenshot(str(path))
        pl.close()
        return Snapshot(name=name, path=path, direction=_unit(direction), facing=facing(direction), up=up)

    numbers = [str(i) for i in ids]
    named = [f"{i}  {o.get('name') or ''}".strip() for i, o in zip(ids, openings)]
    shots: list[Snapshot] = []
    far = 2.2 * diag
    for name, direction in GLOBAL_VIEWS.items():
        position = tuple(centre[k] - direction[k] * far for k in range(3))
        # THE OVERVIEW is the user's picture - translucent so every sticker shows, names on them
        shots.append(shoot(name, position, direction, named if name == "overview" else numbers,
                           translucent=name == "overview"))
    # CLOSE-UPS: straight into each mouth from outside, framed on the mouth
    for o, n, p in list(zip(openings, normals, pts))[:MAX_CLOSEUPS]:
        direction = tuple(-c for c in n)
        position = tuple(p[k] + n[k] * 0.9 * diag for k in range(3))
        shots.append(shoot(f"opening-{int(o['id'])}", position, direction, numbers, zoom=1.6,
                           focus=tuple(p)))
    return shots


@dataclass(frozen=True)
class UprightSheet:
    path: Path
    order: tuple[str, ...]    # the up axis each panel, A to F, was drawn with


#: The drawing-office three-quarter view the stage opens with, for a part standing +z up: from
#: the front left, a little above.
STAGE_VIEW = (-1.0, -1.0, 0.8)


def render_upright_sheet(skin_stl, out_path, *, order=None) -> UprightSheet:
    """THE SIX-WAY PICTURE the model is asked which way up a part is from: the part six times in
    panels A to F, each drawn with a different axis pointing up the panel, from the quarter the
    stage opens on - turned the way the console's Up control turns its camera, so the panel the
    model picks is the view the user then gets. No stickers, no axis marker: nothing but the shape
    to judge by."""
    import numpy as np
    import pyvista as pv

    from meshpipeline.cad.up_axis import UP_AXES, turn

    order = tuple(order or UP_AXES)
    pv.OFF_SCREEN = True
    skin = pv.read(str(skin_stl))
    if skin.n_points == 0:
        raise ValueError("the part's skin has no surface to draw")
    b = skin.bounds
    diag = math.sqrt((b[1] - b[0]) ** 2 + (b[3] - b[2]) ** 2 + (b[5] - b[4]) ** 2) or 1.0
    centre = np.asarray(skin.center, dtype=float)
    pl = pv.Plotter(off_screen=True, window_size=[1200, 800], shape=(2, 3), border=True)
    for i, axis in enumerate(order):
        pl.subplot(i // 3, i % 3)
        pl.set_background("white")  # type: ignore[arg-type]
        pl.add_mesh(skin, color=BODY, smooth_shading=True, specular=0.2)
        d = np.asarray(turn(axis, STAGE_VIEW))
        pl.camera.position = tuple(centre + d / np.linalg.norm(d) * 2.5 * diag)
        pl.camera.focal_point = tuple(centre)
        pl.camera.up = turn(axis, (0.0, 0.0, 1.0))
        pl.reset_camera()  # type: ignore[call-arg]
        pl.add_text("ABCDEF"[i], position="upper_left", font_size=22, color="black")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pl.screenshot(str(out_path))
    pl.close()
    return UprightSheet(path=out_path, order=order)
