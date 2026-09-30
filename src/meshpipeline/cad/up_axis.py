# Responsibility: Read, from the part's own shape, which way it stands up in the real world - the
# code's half of the "which way is up" proposal the geometry check shows the user. A car drawn
# upside down has its wheels or mounting struts at the top of the file; a city block drawn on its
# side has its buildings standing on a side wall of the box.
# Boundaries: pure numpy over triangles. It reads no file, calls no model and decides nothing for
# the user: it says which end the part stands on, how sure that is and why, and the geometry
# check weighs that against the pictures (application/geometry_check.decide_up).
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from meshpipeline.contracts.geometry_fields import DEFAULT_UP, UP_AXES

#: THE END BAND: what a part stands on lies in the last few percent of its height. Three percent
#: reaches the tips of a car's wheels or a wind-tunnel model's struts and stops short of its
#: underbody.
BAND = 0.03
#: Surface points sampled per part, on top of its own corners: enough that a strut a hundredth of
#: the part's length across still lands a dozen points in the band.
SAMPLES = 60_000
#: An end's footprint is read on a grid this many cells across its longer side.
GRID = 96
#: A piece of the band with fewer points than this is a stray sample, not a contact.
MIN_CONTACT_POINTS = 3
#: THE BOUND. Past this many triangles the shape is not read at all: this reading is optional and
#: must never be what makes a big upload's check late or short of memory. The largest corpus
#: files read well inside it (the buildings block, 400,000 triangles, in under two seconds; the
#: CAD skins are a picture's tessellation, a few thousand).
MAX_TRIANGLES = 1_000_000

#: LEGS - wheels, struts, stilts, feet: three or more separate contacts, spread over at least
#: LEGS_SPREAD of the footprint one way and LEGS_SPREAD_OTHER the other (the Windsor body's stilts
#: stand 0.28 of its length apart), and covering little of it (the rear of the OpenFOAM motorbike
#: and its rider meet the end band in three wide pieces covering 11%). A sting, a roof rail or a
#: rotor's hub is one or two contacts, or all in a line. A flat face is no sign: the Windsor body
#: has a flat roof and a flat back, the SAE body a flat nose.
MIN_LEGS = 3
LEGS_SPREAD, LEGS_SPREAD_OTHER = 0.4, 0.2
LEGS_MAX_COVER = 0.06
#: A SCENE - several pieces standing on one ground: most of them reach one end, few the other.
MIN_SCENE_PIECES = 3
SCENE_SHARE = 0.6
SCENE_OTHER = 0.3
#: A piece smaller than this share of the part's surface is a bolt or a stray shell, not a body
#: standing on the ground.
MIN_PIECE_SHARE = 0.005

#: How sure each sign makes the code.
LEGS_CONFIDENCE, SCENE_CONFIDENCE = 0.8, 0.75


@dataclass(frozen=True)
class UpReading:
    """The code's reading: `axis` is the direction that points up (None when the shape does not
    say), `confidence` how sure, `reason` in plain words, and `ends` what each end looked like."""
    axis: str | None
    confidence: float
    reason: str
    ends: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"axis": self.axis, "confidence": round(float(self.confidence), 2),
                "reason": self.reason, "ends": self.ends}


def opposite(axis: str) -> str:
    """"+z" for "-z" and so on."""
    return ("-" if axis[0] == "+" else "+") + axis[1]


def turn(axis: str, v) -> tuple[float, float, float]:
    """`v`, given for a part standing +z up, turned so `axis` points up: the proper rotation (never
    a mirror) taking +Z to that axis - the same turn the console's Up control gives its camera, so
    a picture drawn with it shows what the viewer shows."""
    x, y, z = (float(c) for c in v)
    return {"+z": (x, y, z), "-z": (x, -y, -z), "+y": (x, z, -y), "-y": (x, -z, y),
            "+x": (z, y, -x), "-x": (-z, y, x)}[axis]


def _points(tris: np.ndarray, rng) -> np.ndarray:
    """Points on the surface: every corner, plus samples spread by area."""
    a = tris[:, 0]
    ab, ac = tris[:, 1] - a, tris[:, 2] - a
    area = 0.5 * np.linalg.norm(np.cross(ab, ac), axis=1)
    total = float(area.sum())
    corners = tris.reshape(-1, 3)
    if total <= 0:
        return corners
    idx = rng.choice(len(tris), size=SAMPLES, p=area / total)
    u, v = rng.random(SAMPLES), rng.random(SAMPLES)
    flip = u + v > 1.0
    u[flip], v[flip] = 1.0 - u[flip], 1.0 - v[flip]
    return np.vstack([corners, a[idx] + ab[idx] * u[:, None] + ac[idx] * v[:, None]])


def _pieces(tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The low and high corner of every piece of the part big enough to stand on its own."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    pts = tris.reshape(-1, 3)
    span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    key = np.round(pts / (1e-6 * span)).astype(np.int64)
    _, inverse = np.unique(key, axis=0, return_inverse=True)
    faces = np.asarray(inverse).reshape(-1, 3)
    nv = int(faces.max()) + 1
    rows = np.concatenate([faces[:, 0], faces[:, 1]])
    cols = np.concatenate([faces[:, 1], faces[:, 2]])
    count, labels = connected_components(coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(nv, nv)),
                                         directed=False)
    piece = labels[faces[:, 0]]
    area = 0.5 * np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
    big = np.bincount(piece, weights=area, minlength=count) >= MIN_PIECE_SHARE * float(area.sum() or 1.0)
    lo = np.full((count, 3), np.inf)
    hi = np.full((count, 3), -np.inf)
    np.minimum.at(lo, piece, tris.min(axis=1))
    np.maximum.at(hi, piece, tris.max(axis=1))
    return lo[big], hi[big]


def _contacts(uv: np.ndarray, lo: np.ndarray, size: np.ndarray) -> tuple[int, tuple[float, float], float]:
    """An end band seen from outside it: how many separate pieces it holds, how far apart their
    middles lie (as shares of the footprint, both ways), and the share of the footprint it covers."""
    from scipy import ndimage

    cell = float(max(size[0], size[1])) / GRID or 1.0
    shape = (int(np.ceil(size[0] / cell)) + 1, int(np.ceil(size[1] / cell)) + 1)
    ij = np.clip(((uv - lo) / cell).astype(int), 0, np.array(shape) - 1)
    hits = np.zeros(shape, dtype=np.int32)
    np.add.at(hits, (ij[:, 0], ij[:, 1]), 1)
    occupied = hits > 0
    cover = float(occupied.sum()) * cell * cell / max(float(size[0] * size[1]), 1e-30)
    # one cell of slack bridges the gaps sampling leaves in a continuous face
    labels, n = ndimage.label(ndimage.binary_dilation(occupied), structure=np.ones((3, 3)))
    labels = np.where(occupied, labels, 0)
    middles = [np.argwhere(labels == lab).mean(axis=0) * cell for lab in range(1, n + 1)
               if int(hits[labels == lab].sum()) >= MIN_CONTACT_POINTS]
    if len(middles) < 2:
        return len(middles), (0.0, 0.0), cover
    m = np.asarray(middles)
    return len(middles), (float(np.ptp(m[:, 0]) / max(size[0], 1e-30)),
                          float(np.ptp(m[:, 1]) / max(size[1], 1e-30))), cover


def read_ends(tris) -> dict:
    """What each of the part's six ends looks like: how many separate contacts reach into it, how
    spread out they are, how much of the footprint they cover, and what share of the part's
    pieces reach it. Keyed by the direction pointing out of the end ("-z" is the bottom of a
    z-up file)."""
    tris = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    pts = _points(tris, np.random.default_rng(0))
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    ext = np.maximum(hi - lo, 1e-12)
    piece_lo, piece_hi = _pieces(tris)
    scene = len(piece_lo) >= MIN_SCENE_PIECES
    ends: dict[str, dict] = {}
    for k in range(3):
        a, b = [j for j in range(3) if j != k]
        band = BAND * ext[k]
        for sign in ("-", "+"):
            if sign == "-":
                inside, reach = pts[:, k] <= lo[k] + band, piece_lo[:, k] <= lo[k] + band
            else:
                inside, reach = pts[:, k] >= hi[k] - band, piece_hi[:, k] >= hi[k] - band
            uv = pts[inside][:, [a, b]]
            n, spread, cover = _contacts(uv, np.array([lo[a], lo[b]]), np.array([ext[a], ext[b]]))
            ends[sign + "xyz"[k]] = {
                "contacts": int(n), "spread": [round(spread[0], 3), round(spread[1], 3)],
                "cover": round(cover, 3), "pieces": round(float(reach.mean()), 3) if scene else 0.0,
                "n_pieces": int(len(piece_lo)),
            }
    return ends


def _legs(e: dict) -> bool:
    return (e["contacts"] >= MIN_LEGS and max(e["spread"]) >= LEGS_SPREAD
            and min(e["spread"]) >= LEGS_SPREAD_OTHER and e["cover"] <= LEGS_MAX_COVER)


def _stands_on(bottom: dict, top: dict) -> tuple[float, str] | None:
    """How sure it is that the part stands on this end, and why - or None when it does not look
    it. `top` is the opposite end, which must not look the same."""
    if (bottom["n_pieces"] >= MIN_SCENE_PIECES and bottom["pieces"] >= SCENE_SHARE
            and top["pieces"] <= SCENE_OTHER):
        return SCENE_CONFIDENCE, f"{bottom['pieces']:.0%} of its {bottom['n_pieces']} pieces stand on its {{end}} end"
    if _legs(bottom) and not _legs(top):
        return LEGS_CONFIDENCE, f"it stands on {bottom['contacts']} feet (wheels, struts or legs) at its {{end}} end"
    return None


def up_from_ends(ends: dict) -> UpReading:
    """The direction that points up: away from the one end the part stands on. Two ends that
    both look like something to stand on say nothing."""
    found: list[tuple[float, str, str]] = []
    for down, bottom in ends.items():
        s = _stands_on(bottom, ends[opposite(down)])
        if s is not None:
            found.append((s[0], opposite(down), s[1].format(end=down)))
    if not found:
        return UpReading(None, 0.0, "nothing in its shape says which end it stands on", ends)
    found.sort(key=lambda f: f[0], reverse=True)
    best = found[0]
    if any(f[0] >= best[0] and f[1] != best[1] for f in found[1:]):
        return UpReading(None, 0.0, "more than one end looks like something to stand on", ends)
    return UpReading(best[1], best[0], best[2], ends)


def too_many(n_triangles: int) -> UpReading | None:
    """The reading for a part past the bound - the pictures decide - or None when it is within."""
    if n_triangles <= MAX_TRIANGLES:
        return None
    return UpReading(None, 0.0, f"too many triangles ({n_triangles:,}) to read quickly; the pictures decide")


def read_up(tris) -> UpReading:
    """The code's reading of which way the part stands up, from its triangles."""
    tris = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if len(tris) == 0:
        return UpReading(None, 0.0, "the part has no surface to read")
    return too_many(len(tris)) or up_from_ends(read_ends(tris))


__all__ = ["DEFAULT_UP", "MAX_TRIANGLES", "UP_AXES", "UpReading", "opposite", "read_ends", "read_up", "too_many", "turn",
           "up_from_ends"]
