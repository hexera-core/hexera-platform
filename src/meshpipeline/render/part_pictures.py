# Responsibility: Draw the pictures the parts reading is judged on and the console shows: the whole model from three sides with every part in its own colour and its number on it where that part can be SEEN, sheets of each part alone with its number, and the console's card views (a neutral shaded picture, an id picture the console reads which part is under each pixel from, and where each visible part's number goes) - so the model and the user tie "the brackets" to parts by shape when the CAD names say nothing.
# Boundaries: pyvista and Pillow only, off screen. It reads the measured triangle surface (cad/fea_features.Surface) and the part ids; it decides nothing and calls no model.
# Collaborates with: cad/part_words.py (the model is shown these), application/part_matches.py (the console's card), render/scout_snapshots.py (the same camera conventions).
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
#: the console's card: two sides (the second shows what the first hides), each a neutral shaded picture and its ids
CARD_VIEWS = {"card_a": (-1.0, -1.0, -0.8), "card_b": (1.0, 1.0, 0.8)}
CARD_WINDOW = (960, 640)
#: the card's parts, all one neutral grey: the console colours the parts a row means
NEUTRAL = "#C9CDD3"
#: the id picture: part k (0-based) is drawn flat in (k+1) & 255, (k+1) >> 8, ID_MARK; anything else is no part
ID_MARK = 200
#: a part with fewer visible pixels than this in a view gets no number there (it is hidden in that view)
MIN_VISIBLE_PX = 40
#: a number's tag (pixels): its size, and the gap kept between two tags
TAG_H = 24
TAG_GAP = 3


def colour_of(index: int) -> str:
    """The colour part number index+1 (in the table's order) is drawn in, for the console to match."""
    return PALETTE[int(index) % len(PALETTE)]


def _num(pid: str) -> str:
    return str(pid).rsplit("_", 1)[-1]


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def _id_rgb(k: int) -> tuple[float, float, float]:
    n = int(k) + 1
    return ((n & 255) / 255.0, ((n >> 8) & 255) / 255.0, ID_MARK / 255.0)


def decode_ids(img):
    """The part index (0-based) under each pixel of an id picture; -1 where there is none."""
    import numpy as np

    a = np.asarray(img).astype(np.int64)
    ids = a[:, :, 0] + 256 * a[:, :, 1] - 1
    hit = np.abs(a[:, :, 2] - ID_MARK) <= 1
    if a.shape[2] == 4:
        hit &= a[:, :, 3] > 0
    return np.where(hit, ids, -1)


def _shot(pieces: dict, colour, cam: tuple, window, *, flat: bool, transparent: bool):
    """One picture of the pieces from `cam` (position, focal point, up), as an RGBA array."""
    import pyvista as pv

    pl = pv.Plotter(off_screen=True, window_size=list(window))
    try:
        pl.disable_anti_aliasing()
    except Exception:  # noqa: BLE001, S110 - older pyvista: no anti-aliasing to switch off
        pass
    pl.set_background("white")  # type: ignore[arg-type]
    for p, mesh in pieces.items():
        if flat:
            pl.add_mesh(mesh, color=colour(p), lighting=False, smooth_shading=False)
        else:
            pl.add_mesh(mesh, color=colour(p), smooth_shading=True, specular=0.2)
    pl.camera.position, pl.camera.focal_point, pl.camera.up = cam
    pl.reset_camera()  # type: ignore[call-arg]
    img = pl.screenshot(None, transparent_background=transparent, return_img=True)
    pl.close()
    return img


def tag_places(ids, n_parts: int) -> tuple[list[dict], list[int]]:
    """Where each part's number goes in a view, read off its id picture: a point deep inside the part's VISIBLE pixels
    (a part behind another gets no number on the one in front), each tag clear of the others. Returns
    ([{"k", "x", "y"}], [indices of the parts not visible in this view])."""
    import numpy as np
    from scipy import ndimage

    tags: list[dict] = []
    hidden: list[int] = []
    taken: list[tuple[float, float, float, float]] = []
    found = ndimage.find_objects(np.where(ids >= 0, ids + 1, 0), max_label=n_parts)
    sizes = np.bincount(ids[ids >= 0].ravel(), minlength=n_parts)
    order = sorted(range(n_parts), key=lambda k: -int(sizes[k]))
    for k in order:
        sl = found[k] if k < len(found) else None
        if sl is None or sizes[k] < MIN_VISIBLE_PX:
            hidden.append(k)
            continue
        mask = np.pad(ids[sl] == k, 1)
        depth = ndimage.distance_transform_edt(mask)
        y, x = np.unravel_index(int(np.argmax(depth)), depth.shape)
        cy, cx = sl[0].start + y - 1, sl[1].start + x - 1
        w = 14 + 9 * len(str(k + 1))
        for dx, dy in [(0, 0)] + [(r * math.cos(a), r * math.sin(a)) for r in (14, 26, 40, 56)
                                  for a in [i * math.pi / 4 for i in range(8)]]:
            box = (cx + dx - w / 2, cy + dy - TAG_H / 2, cx + dx + w / 2, cy + dy + TAG_H / 2)
            if all(box[2] + TAG_GAP <= o[0] or o[2] + TAG_GAP <= box[0] or box[3] + TAG_GAP <= o[1]
                   or o[3] + TAG_GAP <= box[1] for o in taken):
                taken.append(box)
                tags.append({"k": k, "x": int(round(cx + dx)), "y": int(round(cy + dy))})
                break
    return sorted(tags, key=lambda t: t["k"]), sorted(hidden)


def _font(size: int):
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_tags(img, tags: list[dict], labels: list[str]):
    """The numbers drawn on a picture where tag_places put them (white rounded tags, black numbers)."""
    from PIL import Image, ImageDraw

    im = Image.fromarray(img).convert("RGBA")
    d = ImageDraw.Draw(im)
    font = _font(15)
    for t in tags:
        s = labels[t["k"]]
        w = 10 + 9 * len(s)
        box = (t["x"] - w / 2, t["y"] - TAG_H / 2, t["x"] + w / 2, t["y"] + TAG_H / 2)
        d.rounded_rectangle(box, radius=5, fill=(255, 255, 255, 235), outline=(40, 40, 40, 255))
        d.text((t["x"], t["y"]), s, fill=(0, 0, 0, 255), font=font, anchor="mm")
    return im


def _crop_box(alpha, margin: int = 12):
    import numpy as np
    ys, xs = np.nonzero(alpha > 0)
    if not len(xs):
        return 0, alpha.shape[0], 0, alpha.shape[1]
    return (max(0, ys.min() - margin), min(alpha.shape[0], ys.max() + 1 + margin),
            max(0, xs.min() - margin), min(alpha.shape[1], xs.max() + 1 + margin))


def render_parts(surface, part_ids: list[str], out_dir, *, card: bool = False) -> list[Path]:
    """The overview pictures, then the sheets of single parts; returns their paths in that order. `card`: the console's
    views too (CARD_VIEWS, last: for each side a neutral shaded picture `<view>.png`, its id picture
    `<view>_ids.png`, and `<view>.json` - its size, where each visible part's number goes, the parts it hides)."""
    import json

    import numpy as np
    import pyvista as pv
    from PIL import Image

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
    labels = [_num(p) for p in part_ids]

    def cam(d) -> tuple:
        dv = np.asarray(d, dtype=float) / np.linalg.norm(d)
        up = (0.0, 0.0, 1.0) if abs(dv[2]) < 0.9 else (0.0, 1.0, 0.0)
        return tuple(centre - dv * 2.2 * diag), tuple(centre), up

    def coloured(p):
        return _rgb(PALETTE[order[p] % len(PALETTE)])

    def ids_of(p):
        return _id_rgb(order[p])

    paths: list[Path] = []
    for name, d in VIEWS.items():
        c = cam(d)
        img = _shot(pieces, coloured, c, WINDOW, flat=False, transparent=False)
        ids = decode_ids(_shot(pieces, ids_of, c, WINDOW, flat=True, transparent=True))
        tags, _hidden = tag_places(ids, len(part_ids))
        im = _draw_tags(np.asarray(img)[:, :, :3], tags, labels)
        from PIL import ImageDraw
        ImageDraw.Draw(im).text((10, 8), f"whole model, {name.split('_')[1]} view: each part its own colour and "
                                "number (numbers only where the part is seen)", fill=(0, 0, 0, 255), font=_font(14))
        path = out / f"{name}.png"
        im.convert("RGB").save(path)
        paths.append(path)
    ids_list = list(pieces)
    for s in range(min(MAX_SHEETS, math.ceil(len(ids_list) / PER_SHEET))):
        chunk = ids_list[s * PER_SHEET:(s + 1) * PER_SHEET]
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
    if card:
        neutral = _rgb(NEUTRAL)
        for name, d in CARD_VIEWS.items():
            c = cam(d)
            shaded = np.asarray(_shot(pieces, lambda _p: neutral, c, CARD_WINDOW, flat=False, transparent=True))
            ids = decode_ids(_shot(pieces, ids_of, c, CARD_WINDOW, flat=True, transparent=True))
            y0, y1, x0, x1 = _crop_box(shaded[:, :, 3])
            shaded, ids = shaded[y0:y1, x0:x1], ids[y0:y1, x0:x1]
            tags, hidden = tag_places(ids, len(part_ids))
            enc = np.zeros(ids.shape + (4,), dtype=np.uint8)
            on = ids >= 0
            n = ids + 1
            enc[..., 0] = np.where(on, n & 255, 0)
            enc[..., 1] = np.where(on, (n >> 8) & 255, 0)
            enc[..., 2] = np.where(on, ID_MARK, 0)
            enc[..., 3] = np.where(on, 255, 0)
            Image.fromarray(np.ascontiguousarray(shaded)).save(out / f"{name}.png")
            Image.fromarray(enc).save(out / f"{name}_ids.png")
            (out / f"{name}.json").write_text(json.dumps({
                "w": int(ids.shape[1]), "h": int(ids.shape[0]),
                "tags": [{"part": part_ids[t["k"]], "n": labels[t["k"]], "x": t["x"], "y": t["y"]} for t in tags],
                "hidden": [part_ids[k] for k in hidden]}))
            paths += [out / f"{name}.png", out / f"{name}_ids.png", out / f"{name}.json"]
    return paths


__all__ = ["CARD_VIEWS", "PALETTE", "colour_of", "decode_ids", "render_parts", "tag_places"]
