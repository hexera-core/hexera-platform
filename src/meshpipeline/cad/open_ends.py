# Responsibility: Find a part's holes - the open ends a fluid enters or leaves by - from its
# triangles alone. The one definition of an opening on a triangle surface: the upload's stickers
# (scout_mesh) and the stage's "Add an opening" click both read it, so the two always agree.
# Owns: what counts as a hole, where its centre is, how big it is and which way it faces.
# Boundaries: numpy over triangles. No OpenCASCADE, no storage, no model, and nothing about what
# the part is: a hole is found by its shape alone.
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

# WHAT A HOLE IS. A closed loop on the surface that bounds uncovered space, of one of two kinds:
#  - a RIM: edges used by one triangle only, chained into a loop - where a thin wall stops;
#  - a BORE MOUTH: a loop of sharp edges where a thick wall's end face (or the outside of the
#    wall) meets the wall that lines the passage - the inner of the two loops a cut thick wall
#    shows. Its passage wall runs back into the part from the loop, and faces into the hole.
# Either way nothing may span the loop, and outside it - the way the mouth faces - is open air.
# No loop has to be flat, round or square to the axis; every measure is taken about the loop
# itself, never about the world origin.
#
# WHAT A CAP IS. A file that IS the fluid - a closed volume, the way a solver exports it - has no
# holes: its openings are flat lids laid across each passage's end. A coarse tessellation leaves
# flat patches all over a curved wall too (a few facets that happen to share a plane), and a cap
# is told from them by measures taken against the mesh's OWN facets, never a fixed size:
#  - FLAT: every corner of the patch lies in one plane;
#  - A SHARP RIM ALL THE WAY ROUND: at nearly every rim edge the wall turns away from the lid by
#    far more than the wall's own facets turn against each other just beside it - a lid's rim is a
#    corner, a facet's edge is just the wall's curve, as gentle as every other edge there;
#  - THE WALL RUNS BACK BEHIND IT, and outside it - the way it faces - is open air.
# Each cap carries how sure that reading is, so a lid can be proposed and a near miss left for
# the user to add with a click.

#: Two faces meeting at more than this angle make a sharp edge: a cut rim, a corner.
SHARP_DEG = 30.0
#: A loop narrower than this share of the part is a flaw in the mesh, not an opening.
MIN_LOOP = 2e-3
#: The mouth's outside is open air when most of these rays, fanned this far around its axis, meet
#: nothing at all.
CONE_DEG = 30.0
CONE_RAYS = 8
MIN_CLEAR = 5                  # of the CONE_RAYS + 1 rays
#: A hole beside a far bigger one in the same face (a bolt hole beside the bore) is not a port.
SIDE_HOLE_AREA = 0.25
#: Only the largest this many loops are probed with rays: a perforated plate has thousands.
MAX_PROBED = 64
#: Points of a loop kept for the stage: enough to draw and click it, not the whole tessellation.
LOOP_POINTS = 96

#: FLAT PATCHES: faces meeting edge to edge within this angle grow one patch, and the patch is flat
#: when none of its corners strays from its plane by more than FLAT_TOL of its size.
FLAT_DEG = 2.0
FLAT_TOL = 0.01
#: A CAP'S RIM: a rim edge is sharp when the wall there turns away from the lid by RIM_CONTRAST
#: times the creases the wall's own facets make just beside the rim (their 90th percentile) - never
#: asked below RIM_MIN_DEG (a fine mesh: any real corner), nor above RIM_MAX_DEG (a mesh so coarse
#: its walls crease like corners: a square lid still shows) - and the wall runs back behind the lid,
#: not folded over it past FOLD_DEG.
RIM_CONTRAST = 1.5
RIM_MIN_DEG = 25.0
RIM_MAX_DEG = 65.0
FOLD_DEG = 165.0
#: ...along at least this share of the rim's length: all the way round, but for a sliver or two;
#: and nowhere along more than RIM_SMOOTH of it does the wall run on from the lid with no corner.
RIM_SHARE = 0.75
RIM_SMOOTH = 0.1
#: One triangle cannot tell a lid from a facet: a lid's rim runs over at least this many edges.
MIN_RIM_EDGES = 4
#: A lid spans one passage, so its outline is about as full as its convex hull (a round, square or
#: oval section, an oblique cut of one); a flat band of wall curving round a bend is not.
LID_SOLIDITY = 0.8
#: Lids whose outside is probed with rays: far more than any passage has mouths.
MAX_LIDS_PROBED = 256
#: A flat patch this many times the size of the patches round it is a face the part was drawn
#: with even where it runs smoothly into a fillet; a facet of a curved wall is their size.
FACE_OVER_FACETS = 4.0


@dataclass
class Hole:
    """One hole, in the triangles' own units. `normal` points out of the part, along the way the
    fluid leaves by it; `loop` is the mouth's edge and `outer`, for a thick wall, the edge of the
    end face around it (a click on that face means this hole)."""

    kind: str                       # "rim" (a thin wall's open edge) | "bore" (a thick wall's cut)
    centroid: np.ndarray
    normal: np.ndarray
    area: float
    wh: tuple[float, float]
    loop: np.ndarray
    outer: np.ndarray | None
    planarity: float                # how far the loop strays from its plane, over its diameter
    clear: int                      # how many of the outside rays met nothing
    #: how sure the reading is, 0..1: an open edge or a bore wall all round, and how much of the
    #: air outside is clear
    confidence: float = 0.0

    @property
    def equivalent_diameter(self) -> float:
        return 2.0 * math.sqrt(max(self.area, 0.0) / math.pi)

    @property
    def shape(self) -> str:
        return _shape(self.wh, self.area)

    def as_dict(self, mm_per_unit: float = 1000.0) -> dict:
        """The hole as the stage reads it: metres and the reading's millimetres side by side, the
        loops thinned to LOOP_POINTS points (metres)."""
        r6 = lambda v: [round(float(x), 6) for x in v]  # noqa: E731
        out = {"kind": self.kind, "shape": self.shape,
               "centroid_m": r6(self.centroid), "centroid_mm": [round(float(x) * mm_per_unit, 2) for x in self.centroid],
               "normal": [round(float(x), 5) for x in self.normal],
               "area_mm2": round(self.area * mm_per_unit * mm_per_unit, 2),
               "diameter_mm": round(self.equivalent_diameter * mm_per_unit, 2),
               "loop_m": [r6(p) for p in _thin(self.loop)],
               "planarity": round(self.planarity, 4), "confidence": round(self.confidence, 2)}
        if self.shape != "circle":
            out["width_mm"], out["height_mm"] = round(self.wh[0] * mm_per_unit, 2), round(self.wh[1] * mm_per_unit, 2)
        if self.outer is not None:
            out["outer_m"] = [r6(p) for p in _thin(self.outer)]
        return out


@dataclass
class Cap:
    """One flat patch of a skin, measured as the lid over a passage's end might be, in the
    triangles' own units. `normal` points out of the part; `loop` is the patch's outline. `sharp`
    is the share of its rim, by length, where the wall turns away from it like a lid's rim and not
    like the wall's own facets; `clear` how many of the outside rays met nothing (-1: not looked)."""

    face: int                       # one of its faces, by index into the skin
    faces: np.ndarray               # all of them
    centroid: np.ndarray
    normal: np.ndarray
    area: float
    wh: tuple[float, float]
    loop: np.ndarray
    inner: float                    # the area of the largest outline inside it (an end face's hole)
    sharp: float
    crease: float                   # the wall's own creases beside the rim, degrees (90th percentile)
    rim_edges: int
    smooth: float                   # share of the rim where the wall runs on from it, no corner at all
    over: float                     # its area over the patches' across its rim (by rim length)
    solidity: float                 # how much of its outline's convex hull it fills
    clear: int
    confidence: float

    @property
    def face_of_part(self) -> bool:
        """A flat face the part was drawn with, not a facet of a curved wall: cornered, or far
        bigger than the wall's patches round it (a flat face that runs into a fillet)."""
        return self.sharp >= 0.5 or self.over >= FACE_OVER_FACETS

    @property
    def likely(self) -> bool:
        """A lid: a sharp rim all the way round, and open air outside."""
        return self._rim_ok and self.clear >= MIN_CLEAR

    @property
    def _rim_ok(self) -> bool:
        return (self.sharp >= RIM_SHARE and self.smooth <= RIM_SMOOTH and self.rim_edges >= MIN_RIM_EDGES
                and self.solidity >= LID_SOLIDITY)

    @property
    def equivalent_diameter(self) -> float:
        return 2.0 * math.sqrt(max(self.area, 0.0) / math.pi)

    @property
    def shape(self) -> str:
        return _shape(self.wh, self.area)


def surface(tris) -> _Mesh | None:
    """A triangle file's skin, read once for both finders (None when it holds too few faces)."""
    tris = np.asarray(tris, dtype=np.float64).reshape(-1, 3, 3)
    if len(tris) < 4:
        return None
    skin = skin_faces(tris)
    if len(skin[1]) < 4:
        return None
    return _Mesh(skin=skin)


def find_holes(tris=None, *, mesh: _Mesh | None = None) -> list[Hole]:
    """Every hole of a triangle surface, the largest first. `tris` is (n, 3, 3); or pass the
    surface() already read."""
    mesh = mesh if mesh is not None else surface(tris)
    if mesh is None:
        return []
    # the cheap look at every loop first (what runs along it), then the rays for the largest few
    shaped = [s for s in (mesh.shape(loop) for loop in mesh.feature_loops()) if s is not None]
    shaped.sort(key=lambda s: s["area"], reverse=True)
    holes = [h for h in (mesh.probe(s) for s in shaped[:MAX_PROBED]) if h is not None]
    return _distinct(holes)


def find_caps(tris=None, *, mesh: _Mesh | None = None, min_area: float = math.inf) -> list[Cap]:
    """The flat patches of a triangle surface, each measured as a lid (see WHAT A CAP IS), the
    largest first: every patch of at least `min_area` (the faces a click may land on), and every
    smaller one whose rim is mostly sharp (a small branch's lid). Read `Cap.likely` for the lids."""
    mesh = mesh if mesh is not None else surface(tris)
    if mesh is None:
        return []
    return mesh.caps(min_area)


# ---------------------------------------------------------------------------- the surface ----
def skin_faces(tris) -> tuple[np.ndarray, np.ndarray]:
    """A triangle file's outer skin as shared vertices and faces: corners at one spot welded,
    faces with no area dropped, and the faces inside an assembly taken out (see below)."""
    pts = np.asarray(tris, dtype=np.float64).reshape(-1, 3)
    span = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))) or 1.0
    key = np.round(pts / (1e-6 * span)).astype(np.int64)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts = pts[first]
    faces = np.asarray(inverse, dtype=np.int64).reshape(-1, 3)
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    # A FACE TWO SOLIDS SHARE IS INSIDE THE PART: an assembly (a duct and its flanges) carries the
    # face where they touch once per solid. Every pair of such copies goes; the skin stays.
    _, inv, cnt = np.unique(np.sort(faces, axis=1), axis=0, return_inverse=True, return_counts=True)
    inv = np.asarray(inv).reshape(-1)
    first = np.zeros(len(faces), dtype=bool)
    first[np.unique(inv, return_index=True)[1]] = True
    faces = faces[(cnt[inv] == 1) | ((cnt[inv] % 2 == 1) & first)]
    # ...and where two solids touch with faces tessellated apart, the faces pressed against each
    # other - one plane, opposite ways, one over the other - go too
    return verts, faces[~_pressed(verts, faces, span)]


class _Mesh:
    def __init__(self, tris: np.ndarray | None = None, skin: tuple[np.ndarray, np.ndarray] | None = None):
        self.verts, self.faces = skin if skin is not None else skin_faces(tris)
        lo, hi = self.verts.min(axis=0), self.verts.max(axis=0)
        self.diag = float(np.linalg.norm(hi - lo)) or 1.0
        self._edges()
        self._orient()
        a, b, c = (self.verts[self.faces[:, k]] for k in range(3))
        cross = np.cross(b - a, c - a)
        n = np.linalg.norm(cross, axis=1)
        self.area = n / 2.0
        self.normal = cross / np.where(n > 0, n, 1.0)[:, None]
        self.centre = (a + b + c) / 3.0
        self.radius = np.max(np.stack([np.linalg.norm(x - self.centre, axis=1) for x in (a, b, c)]), axis=0)
        # a sharp edge: two faces at more than SHARP_DEG, by their consistent normals; every edge
        # with one face, or more than two, is a feature too
        both = self.count == 2
        cos = np.ones(len(self.count))
        cos[both] = np.einsum("ij,ij->i", self.normal[self.f0[both]], self.normal[self.f1[both]])
        self.cos = cos                                         # how far the two faces on each edge turn
        self.feature = (self.count != 2) | (cos < math.cos(math.radians(SHARP_DEG)))
        self.why = ""                                          # why the last loop was not a hole

    def _edges(self) -> None:
        f = self.faces
        a = f.reshape(-1)
        b = f[:, [1, 2, 0]].reshape(-1)
        lo, hi = np.minimum(a, b), np.maximum(a, b)
        key = lo * (len(self.verts) + 1) + hi
        order = np.argsort(key, kind="stable")
        ks = key[order]
        starts = np.r_[0, np.flatnonzero(np.diff(ks)) + 1]
        self.count = np.diff(np.r_[starts, len(ks)])
        h0 = order[starts]
        h1 = np.where(self.count >= 2, order[np.minimum(starts + 1, len(order) - 1)], -1)
        self.ea, self.eb = lo[h0], hi[h0]
        self.f0 = h0 // 3
        self.f1 = np.where(h1 >= 0, h1 // 3, -1)
        self.fwd0 = a[h0] == self.ea                       # face f0 runs the edge ea -> eb
        self.fwd1 = np.where(h1 >= 0, a[np.maximum(h1, 0)] == self.ea, False)
        self.edge_of = np.empty(len(a), dtype=np.int64)    # the edge of each face corner
        self.edge_of[order] = np.repeat(np.arange(len(starts)), self.count)

    def _orient(self) -> None:
        """Turn every face to agree with its neighbours, and every closed piece to face out: a
        triangle file's winding is often careless, and in- and outside decide what a bore is."""
        nf = len(self.faces)
        flip = np.zeros(nf, dtype=bool)
        seen = np.zeros(nf, dtype=bool)
        manifold = np.flatnonzero(self.count == 2)
        a, b = self.f0[manifold], self.f1[manifold]
        same = self.fwd0[manifold] == self.fwd1[manifold]  # both run the edge one way: one is turned
        if same.sum() <= 0.01 * max(len(same), 1):
            # the file winds its faces one way already (a CAD tessellation does): trust it, and
            # only learn the pieces - turning faces across an assembly's odd joins does harm
            same = np.zeros_like(same)
        nbr: list[list[tuple[int, bool]]] = [[] for _ in range(nf)]
        for x, y, s in zip(a.tolist(), b.tolist(), same.tolist()):
            nbr[x].append((y, s))
            nbr[y].append((x, s))
        piece = np.full(nf, -1, dtype=np.int64)
        pieces = 0
        for s0 in range(nf):
            if seen[s0]:
                continue
            seen[s0] = True
            piece[s0] = pieces
            todo = deque([s0])
            while todo:
                x = todo.popleft()
                for y, s in nbr[x]:
                    if not seen[y]:
                        seen[y] = True
                        piece[y] = pieces
                        flip[y] = flip[x] ^ s
                        todo.append(y)
            pieces += 1
        faces = self.faces.copy()
        faces[flip] = faces[flip][:, [0, 2, 1]]
        # a closed piece (no edge with one face) faces out when its volume is positive
        open_piece = np.zeros(pieces, dtype=bool)
        open_piece[piece[self.f0[self.count != 2]]] = True
        v = self.verts[faces]
        ref = self.verts.mean(axis=0)
        vol = np.einsum("ij,ij->i", v[:, 0] - ref, np.cross(v[:, 1] - ref, v[:, 2] - ref)) / 6.0
        turn = np.bincount(piece, weights=vol, minlength=pieces) < 0
        inward = turn[piece] & ~open_piece[piece]
        faces[inward] = faces[inward][:, [0, 2, 1]]
        self.faces = faces
        self.closed = ~open_piece[piece]                  # per face: its piece has an inside

    # ------------------------------------------------------------------------- the loops ----
    def feature_loops(self):
        """Every closed loop a hole could be: the chains of edges with one face (a thin wall's
        open edges), and the outline of every smooth patch of the surface - the faces joined
        across edges that are not sharp. A thick wall's cut end is such a patch, its inner outline
        the bore's mouth, whatever creases run along the bore behind it."""
        seen: set = set()
        for loop in self._open_chains():
            seen.add(frozenset(loop[1].tolist()))
            yield loop
        for loop in self._patch_outlines():
            key = frozenset(loop[1].tolist())
            if key not in seen:
                seen.add(key)
                yield loop

    def _open_chains(self):
        ids = np.flatnonzero(self.count == 1)
        yield from _chain(ids, self.ea[ids], self.eb[ids], _at(self.ea[ids], self.eb[ids]), self.verts)

    def _patch_outlines(self):
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        nf = len(self.faces)
        join = (self.count == 2) & ~self.feature
        g = coo_matrix((np.ones(int(join.sum())), (self.f0[join], self.f1[join])), shape=(nf, nf))
        _, label = connected_components(g, directed=False)
        # every face side that borders another patch, or nothing, run the way its face winds - so
        # each patch's outlines come out with the patch on their left
        frm = self.faces.reshape(-1)
        to = self.faces[:, [1, 2, 0]].reshape(-1)
        face = np.repeat(np.arange(nf), 3)
        e = self.edge_of
        other = np.where(self.f0[e] == face, self.f1[e], self.f0[e])
        border = (self.count[e] != 2) | (label[np.maximum(other, 0)] != label[face])
        hs = np.flatnonzero(border)
        out: dict[tuple[int, int], list[int]] = {}
        for h in hs.tolist():
            out.setdefault((int(label[face[h]]), int(frm[h])), []).append(h)
        used: set = set()
        for h0 in hs.tolist():
            if h0 in used:
                continue
            used.add(h0)
            lab = int(label[face[h0]])
            v0 = int(frm[h0])
            path_v, path_h = [v0], [h0]
            cur = int(to[h0])
            ok = False
            for _ in range(len(hs)):
                if cur == v0:
                    ok = True
                    break
                path_v.append(cur)
                nxt = next((h for h in out.get((lab, cur), ()) if h not in used), None)
                if nxt is None:
                    break
                used.add(nxt)
                path_h.append(nxt)
                cur = int(to[nxt])
            if ok and len(path_v) >= 3:
                yield np.asarray(path_v), e[np.asarray(path_h)]

    def shape(self, found) -> dict | None:
        """What a closed loop could be, from the faces along it alone: a thin wall's open edge, or
        a bore's mouth - with no face lying across it - else None."""
        verts_idx, edge_idx = found
        P = self.verts[verts_idx]
        m = _measure(P)
        if m is None:
            return None
        c, N, area, wh, dev = m
        d = 2.0 * math.sqrt(area / math.pi)
        if d < MIN_LOOP * self.diag:
            self.why = "too small"
            return None
        # each loop edge, the faces on it, and what each is: a WALL standing back from the loop
        # (tilted more than 45 degrees from its plane) - which side, and whether it faces into the
        # loop - or a face lying in the loop's plane, outside it (an end face) or across it
        a, b = P, np.roll(P, -1, axis=0)
        e = b - a
        e /= np.linalg.norm(e, axis=1, keepdims=True).clip(1e-30)
        inward = np.cross(N, e)                                  # in the plane, into the loop
        mid = (a + b) / 2.0
        k_ = np.repeat(np.arange(len(edge_idx)), 2)
        f_ = np.stack([self.f0[edge_idx], self.f1[edge_idx]], axis=1).reshape(-1)
        k_, f_ = k_[f_ >= 0], f_[f_ >= 0]
        if len(f_) == 0:
            self.why = "no faces"
            return None
        nrm, ar, cl = self.normal[f_], self.area[f_], self.closed[f_]
        dc = self.centre[f_] - mid[k_]
        dc /= np.linalg.norm(dc, axis=1, keepdims=True).clip(1e-30)
        fN = nrm @ N
        fm = np.einsum("ij,ij->i", nrm, inward[k_])
        cN = dc @ N
        cm = np.einsum("ij,ij->i", dc, inward[k_])
        n_edges = len(edge_idx)
        wall = np.abs(fN) <= 0.7
        across = ~wall & (cm > 0.3)                              # lying across the loop: it is covered
        if len(np.unique(k_[across])) > 0.3 * n_edges:
            self.why = "covered: a face lies across it"
            return None
        aN = np.where(wall, np.sign(cN), 0.0)
        is_rim = bool(np.all(self.count[edge_idx] == 1))
        if is_rim:
            kind = "rim"
            side = float(np.sum(ar[wall] * aN[wall])) if wall.any() else 0.0
            sides = [-math.copysign(1.0, side)] if abs(side) > 0.3 * float(ar.sum()) else [1.0, -1.0]
        else:
            kind = "bore"
            if not wall.any() or len(np.unique(k_[wall])) < 0.6 * n_edges:
                self.why = f"no wall runs back from {n_edges - len(np.unique(k_[wall]))} of {n_edges} edges"
                return None
            facing = np.where(cl[wall] > 0, fm[wall], np.abs(fm[wall]))   # a bore's wall faces into it
            if float(np.sum(ar[wall] * facing)) < 0.5 * float(ar[wall].sum()):
                self.why = f"the wall faces away ({float(np.sum(ar[wall] * facing)) / float(ar[wall].sum()):.2f})"
                return None
            side = float(np.sum(ar[wall] * aN[wall]))
            if abs(side) < 0.6 * float(ar[wall].sum()):
                self.why = "walls on both sides: a seam, not a cut"
                return None
            sides = [-math.copysign(1.0, side)]
        return {"kind": kind, "P": P, "c": c, "N": N, "area": area, "wh": wh, "dev": dev, "d": d,
                "sides": sides, "seeds": f_[~wall & ~across]}

    def probe(self, s: dict) -> tuple[Hole, set] | None:
        """The hole a shaped loop is, or None: nothing may span it, and outside it - the way it
        faces - must be open air."""
        kind, P, c, N, area, wh, dev, d = (s[k] for k in ("kind", "P", "c", "N", "area", "wh", "dev", "d"))
        out, clear = N * s["sides"][0], -1
        for sgn in s["sides"]:
            n_out = N * sgn
            n_clear = self._clear(c + n_out * (dev + 0.02 * d), n_out)
            if n_clear > clear:
                out, clear = n_out, n_clear
        if clear < MIN_CLEAR or self._spanned(P, c, out, d, dev):
            return None
        outer, band = self._end_face(c, out, d, dev, s["seeds"]) if kind == "bore" else (None, set())
        # an open edge is a hole for certain, a bore's mouth nearly so; how sure, by how open the
        # air outside is
        conf = (0.6 if kind == "rim" else 0.5) + 0.35 * clear / (CONE_RAYS + 1)
        hole = Hole(kind=kind, centroid=c, normal=out, area=area, wh=wh, loop=P, outer=outer,
                    planarity=dev / d, clear=clear, confidence=round(conf, 2))
        return hole, band

    # ---------------------------------------------------------------------------- the caps ----
    def caps(self, min_area: float) -> list[Cap]:
        """The flat patches, measured as lids (see WHAT A CAP IS): all of at least min_area, and
        any smaller whose rim is mostly sharp."""
        from scipy.sparse import coo_matrix
        from scipy.sparse.csgraph import connected_components

        nf = len(self.faces)
        V, F = self.verts, self.faces
        join = (self.count == 2) & (self.cos >= math.cos(math.radians(FLAT_DEG)))
        g = coo_matrix((np.ones(int(join.sum())), (self.f0[join], self.f1[join])), shape=(nf, nf))
        n_lab, lab = connected_components(g, directed=False)
        # each patch's plane: its area-weighted normal through its area centroid
        area = np.bincount(lab, weights=self.area, minlength=n_lab)
        wn = np.stack([np.bincount(lab, weights=self.normal[:, k] * self.area, minlength=n_lab) for k in range(3)], axis=1)
        wc = np.stack([np.bincount(lab, weights=self.centre[:, k] * self.area, minlength=n_lab) for k in range(3)], axis=1)
        L = np.linalg.norm(wn, axis=1)
        N = wn / np.where(L > 0, L, 1.0)[:, None]
        C = wc / np.where(area > 0, area, 1.0)[:, None]
        dev = np.zeros(n_lab)
        for k in range(3):
            np.maximum.at(dev, lab, np.abs(np.einsum("ij,ij->i", V[F[:, k]] - C[lab], N[lab])))
        flat = (L > 0) & (dev <= FLAT_TOL * np.sqrt(area)) & (np.sqrt(4.0 * area / math.pi) >= MIN_LOOP * self.diag)
        if not flat.any():
            return []

        # THE RIM: every edge between a flat patch and anything else, from the patch's side
        e = np.flatnonzero((self.count != 2) | (lab[self.f0] != lab[np.maximum(self.f1, 0)]))
        two = self.count[e] == 2
        has1 = self.f1[e] >= 0
        P = np.r_[lab[self.f0[e]], lab[self.f1[e][has1]]]
        E = np.r_[e, e[has1]]
        O = np.r_[np.where(two, self.f1[e], -1), np.where(two[has1], self.f0[e][has1], -1)]
        keep = flat[P]
        P, E, O = P[keep], E[keep], O[keep]
        if len(P) == 0:
            return []
        a, b = V[self.ea[E]], V[self.eb[E]]
        length = np.linalg.norm(b - a, axis=1)
        wall = O >= 0
        Os = np.maximum(O, 0)
        ang = np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", N[P], self.normal[Os]), -1.0, 1.0)))
        back = np.einsum("ij,ij->i", self.centre[Os] - (a + b) / 2.0, N[P])
        # a closed piece's faces face out, so its lid's wall lies behind it; an open piece's way out
        # is the side its walls do not stand on
        rim_len = np.bincount(P, weights=length, minlength=n_lab)
        behind_len = np.bincount(P, weights=length * (wall & (back < 0)), minlength=n_lab)
        front_len = np.bincount(P, weights=length * (wall & (back > 0)), minlength=n_lab)
        closed_lab = np.zeros(n_lab, dtype=bool)
        closed_lab[lab[self.closed]] = True
        turn = ~closed_lab & (front_len > behind_len)
        N[turn] = -N[turn]
        back = np.where(turn[P], -back, back)
        ang = np.where(turn[P], 180.0 - ang, ang)

        # THE WALL'S OWN CREASES beside the rim: where the wall's facets meet each other - not
        # inside one flat facet, never on the rim itself - over the two rows of faces across the
        # rim. That is what this mesh's facets do on this wall, here.
        # (only for the patches whose rim could be sharp at all: on a fine skin, a handful)
        rim_len = np.where(rim_len > 0, rim_len, 1.0)
        corner0 = wall & (back < 0) & (ang >= RIM_MIN_DEG) & (ang <= FOLD_DEG)
        maybe = np.bincount(P, weights=length * corner0, minlength=n_lab) / rim_len >= 0.5
        corners3 = self.edge_of.reshape(-1, 3)
        ring1, rp1 = O[wall & maybe[P]], P[wall & maybe[P]]
        e1 = corners3[ring1].reshape(-1)
        p1 = np.repeat(rp1, 3)
        f1 = np.repeat(ring1, 3)
        nxt = np.where(self.f0[e1] == f1, self.f1[e1], self.f0[e1])
        go = (nxt >= 0) & (lab[np.maximum(nxt, 0)] != p1)
        ring = np.r_[ring1, nxt[go]]
        RP = np.r_[rp1, p1[go]]
        RE = corners3[ring].reshape(-1)
        RP = np.repeat(RP, 3)
        pair = np.unique(np.stack([RP, RE], axis=1), axis=0)
        RP, RE = pair[:, 0], pair[:, 1]
        g1 = np.maximum(self.f1[RE], 0)
        okr = ((self.count[RE] == 2) & (lab[self.f0[RE]] != lab[g1])
               & (lab[self.f0[RE]] != RP) & (lab[g1] != RP))
        RE, RP = RE[okr], RP[okr]
        crease = np.zeros(n_lab)
        if len(RE):
            ra = np.degrees(np.arccos(np.clip(self.cos[RE], -1.0, 1.0)))
            order = np.lexsort((ra, RP))
            RPs, ras = RP[order], ra[order]
            labs, first, counts = np.unique(RPs, return_index=True, return_counts=True)
            crease[labs] = ras[first + np.floor(0.9 * (counts - 1)).astype(np.int64)]
        need = np.clip(RIM_CONTRAST * crease, RIM_MIN_DEG, RIM_MAX_DEG)
        corner = corner0 & maybe[P] & (ang >= need[P])
        sharp = np.bincount(P, weights=length * corner, minlength=n_lab) / rim_len
        n_rim = np.bincount(P, minlength=n_lab)
        # where the wall runs on from the patch with no corner at all: a lid crosses its passage,
        # it is never tangent to the wall (a flat side of a duct that runs into a bend is)
        smooth = np.bincount(P, weights=length * (wall & (ang < RIM_MIN_DEG)), minlength=n_lab) / rim_len
        # how much bigger the patch is than the patches across its rim: a facet of a curved wall
        # is the size of the facets round it, a flat face of the part is far bigger than a fillet's
        wall_len = np.bincount(P, weights=length * wall, minlength=n_lab)
        nb_area = np.bincount(P, weights=length * wall * area[lab[Os]], minlength=n_lab) / np.where(wall_len > 0, wall_len, 1.0)
        over = np.where(nb_area > 0, area / np.where(nb_area > 0, nb_area, 1.0), np.inf)

        # measured one by one: the patches big enough to click, and the small ones that could be lids
        pick = np.flatnonzero(flat & ((area >= min_area) | (sharp >= 0.5)))
        order = np.argsort(P, kind="stable")
        Ps = P[order]
        starts = np.searchsorted(Ps, pick, side="left")
        ends = np.searchsorted(Ps, pick, side="right")
        members = np.argsort(lab, kind="stable")
        mstart = np.searchsorted(lab[members], pick, side="left")
        mend = np.searchsorted(lab[members], pick, side="right")
        out: list[Cap] = []
        for i, p in enumerate(pick.tolist()):
            rim = np.unique(E[order[starts[i]:ends[i]]])
            ea, eb = self.ea[rim], self.eb[rim]
            loops = [(V[vs], m) for vs, _ in _chain(rim, ea, eb, _at(ea, eb), V) if (m := _measure(V[vs])) is not None]
            if not loops:
                continue
            loops.sort(key=lambda lm: lm[1][2], reverse=True)
            outline, (c, n_loop, outline_area, wh, _) = loops[0]
            faces = members[mstart[i]:mend[i]]
            out.append(Cap(face=int(faces[0]), faces=faces, centroid=c, normal=N[p].copy(), area=float(area[p]), wh=wh,
                           loop=outline, inner=float(loops[1][1][2]) if len(loops) > 1 else 0.0, sharp=float(sharp[p]),
                           crease=float(crease[p]), rim_edges=int(n_rim[p]), smooth=float(smooth[p]),
                           over=float(over[p]), solidity=_solidity(outline, c, n_loop, outline_area), clear=-1,
                           confidence=0.0))
        # OPEN AIR OUTSIDE, for the lids among them: the rays cost the most, so a part with very many
        # gets them for its surest rims first, whatever their size - a small branch's clean lid
        # before a big flat face's ragged one
        lids = sorted((cap for cap in out if cap._rim_ok), key=lambda cap: (round(cap.sharp, 2), cap.area), reverse=True)
        for cap in lids[:MAX_LIDS_PROBED]:
            cap.clear = self._clear(cap.centroid + cap.normal * 0.02 * cap.equivalent_diameter, cap.normal)
        # HOW SURE: a rim sharp all the way round, under open air - the share of the rim beyond
        # half that is sharp, times the share of the outside rays that met nothing
        for cap in out:
            q = min(max((cap.sharp - 0.5) / (1.0 - 0.5), 0.0), 1.0)
            air = cap.clear / (CONE_RAYS + 1) if cap.clear >= 0 else 0.0
            cap.confidence = round(0.3 + 0.65 * q * air, 2)
        out.sort(key=lambda cap: cap.area, reverse=True)
        return out

    # --------------------------------------------------------------------------- the rays ----
    def _hits(self, origins: np.ndarray, dirs: np.ndarray, faces: np.ndarray | None = None) -> np.ndarray:
        """The distance to the first face each ray meets (inf for none), either side of a face;
        among the given faces only, when the caller knows the rest cannot be met."""
        out = np.full(len(origins), np.inf)
        f = self.faces if faces is None else self.faces[faces]
        if len(f) == 0:
            return out
        V0 = self.verts[f[:, 0]]
        E1 = self.verts[f[:, 1]] - V0
        E2 = self.verts[f[:, 2]] - V0
        for r in range(len(origins)):
            o, dr = origins[r], dirs[r]
            p = np.cross(dr, E2)
            det = np.einsum("ij,ij->i", E1, p)
            ok = np.abs(det) > 1e-30
            inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
            t0 = o - V0
            u = np.einsum("ij,ij->i", t0, p) * inv
            q = np.cross(t0, E1)
            v = (q @ dr) * inv
            t = np.einsum("ij,ij->i", E2, q) * inv
            hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9 * self.diag)
            if hit.any():
                out[r] = float(t[hit].min())
        return out

    def _in_cone(self, origin: np.ndarray, axis: np.ndarray, half_deg: float) -> np.ndarray:
        """The faces a ray from the origin within half_deg of the axis could meet."""
        r = self.centre - origin
        along = r @ axis
        side = np.linalg.norm(r - np.outer(along, axis), axis=1)
        reach = np.tan(math.radians(min(half_deg + 5.0, 85.0))) * np.maximum(along, 0.0) + self.radius
        return np.flatnonzero((along >= -self.radius) & (side <= reach))

    def _near(self, point: np.ndarray, radius: float) -> np.ndarray:
        return np.flatnonzero(np.linalg.norm(self.centre - point, axis=1) <= radius + self.radius)

    def _clear(self, origin: np.ndarray, axis: np.ndarray) -> int:
        """How many of the rays fanned around the mouth's axis meet nothing."""
        u = np.cross(axis, [0.0, 0.0, 1.0] if abs(axis[2]) < 0.9 else [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        w = np.cross(axis, u)
        t = math.radians(CONE_DEG)
        dirs = np.asarray([axis] + [math.cos(t) * axis + math.sin(t) * (math.cos(a) * u + math.sin(a) * w)
                                    for a in np.linspace(0, 2 * math.pi, CONE_RAYS, endpoint=False)])
        hits = self._hits(np.repeat(origin[None, :], len(dirs), axis=0), dirs, self._in_cone(origin, axis, CONE_DEG))
        return int(np.sum(~np.isfinite(hits)))

    def _spanned(self, P, c, out, d, dev) -> bool:
        """Whether a surface lies across the loop - its own face, or another set just behind it.
        Points spread evenly over the loop's area look for one; a rod standing in a bore (an
        annular mouth) covers its middle only, and leaves the hole a hole."""
        reach = dev + 0.05 * d
        ring = P[np.linspace(0, len(P), 8, endpoint=False).astype(int)]
        pts = [c + f * (p - c) for f in (0.35, 0.61, 0.79, 0.93) for p in ring]
        origins = np.asarray(pts) + out * reach
        near = self._near(c, float(np.max(np.linalg.norm(P - c, axis=1))) + 3.0 * reach)
        hits = self._hits(origins, np.repeat(-out[None, :], len(origins), axis=0), near)
        return bool(np.sum(hits < 2.0 * reach) >= 0.9 * len(origins))

    def _end_face(self, c, out, d, dev, seeds) -> tuple[np.ndarray | None, set]:
        """The face around a bore's mouth - the cut wall's end - grown from the faces on the loop
        that lie in its plane, while they face the mouth's way: its outline, and its faces."""
        cos = math.cos(math.radians(35.0))
        band = {int(f) for f in seeds if self.normal[f] @ out >= cos}
        if not band:
            return None, set()
        reach, slab = 2.0 * d, dev + 0.15 * d
        todo = deque(band)
        corners = self.edge_of.reshape(-1, 3)
        while todo and len(band) < 50_000:
            f = todo.popleft()
            for ed in corners[f]:
                g = self.f1[ed] if self.f0[ed] == f else self.f0[ed]
                if g < 0 or g in band or self.count[ed] != 2:
                    continue
                r = self.centre[g] - c
                h = float(r @ out)
                if self.normal[g] @ out >= cos and abs(h) <= slab and np.linalg.norm(r - h * out) <= reach:
                    band.add(int(g))
                    todo.append(int(g))
        # the outline: the farthest corner of the face in each direction around the mouth
        u = np.cross(out, [0.0, 0.0, 1.0] if abs(out[2]) < 0.9 else [0.0, 1.0, 0.0])
        u /= np.linalg.norm(u)
        w = np.cross(out, u)
        pts = self.verts[self.faces[list(band)].reshape(-1)] - c
        x, y = pts @ u, pts @ w
        ang = np.floor((np.arctan2(y, x) + math.pi) / (2 * math.pi) * 64).astype(int) % 64
        rad = np.hypot(x, y)
        far = np.full(64, -1.0)
        np.maximum.at(far, ang, rad)
        if np.sum(far > 0) < 40:
            return None, band
        a = (np.arange(64) + 0.5) / 64 * 2 * math.pi - math.pi
        far = np.where(far > 0, far, np.interp(a, a[far > 0], far[far > 0], period=2 * math.pi))
        outline = c + np.outer(far * np.cos(a), u) + np.outer(far * np.sin(a), w)
        return outline, band


# ------------------------------------------------------------------------- the assemblies ----
def _pressed(verts: np.ndarray, faces: np.ndarray, diag: float) -> np.ndarray:
    """The faces lying against another face - the same plane, facing the other way, one's centre
    on the other: where the solids of an assembly touch. They are inside the part."""
    a, b, c = (verts[faces[:, k]] for k in range(3))
    cross = np.cross(b - a, c - a)
    L = np.linalg.norm(cross, axis=1)
    n = cross / np.where(L > 0, L, 1.0)[:, None]
    cen = (a + b + c) / 3.0
    off = np.einsum("ij,ij->i", n, cen)
    # one key per plane and way: a face's plane, and the same plane faced the other way
    q, tol = 1e-3, 1e-5 * diag
    key = np.c_[np.round(n / q), np.round(off / tol)].astype(np.int64)
    groups: dict = {}
    for i, k in enumerate(map(tuple, key.tolist())):
        if L[i] > 0:
            groups.setdefault(k, []).append(i)
    out = np.zeros(len(faces), dtype=bool)
    for k, A in groups.items():
        kb = (-k[0], -k[1], -k[2], -k[3])
        B = groups.get(kb)
        if not B or k > kb or len(A) * len(B) > 4_000_000:
            continue
        A, B = np.asarray(A), np.asarray(B)
        out[A[_covered(cen[A], a[B], b[B], c[B], n[A[0]])]] = True
        out[B[_covered(cen[B], a[A], b[A], c[A], n[B[0]])]] = True
    return out


def _covered(p, a, b, c, n) -> np.ndarray:
    """Which of the points p lie on one of the triangles (a, b, c), all in one plane of normal n."""
    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    P = np.c_[p @ u, p @ v][:, None, :]
    A, B, C = (np.c_[x @ u, x @ v][None, :, :] for x in (a, b, c))

    def cr(o, x, y):
        return (x[..., 0] - o[..., 0]) * (y[..., 1] - o[..., 1]) - (x[..., 1] - o[..., 1]) * (y[..., 0] - o[..., 0])

    s1, s2, s3 = cr(A, B, P), cr(B, C, P), cr(C, A, P)
    inside = ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))
    return inside.any(axis=1)


# ------------------------------------------------------------------------------ the loops ----
def _at(ea, eb) -> dict[int, list[int]]:
    """Which of the edges (by position) meet at each corner."""
    at: dict[int, list[int]] = {}
    for k, (x, y) in enumerate(zip(ea.tolist(), eb.tolist())):
        at.setdefault(x, []).append(k)
        at.setdefault(y, []).append(k)
    return at


def _solidity(P: np.ndarray, c: np.ndarray, n: np.ndarray, area: float) -> float:
    """How much of its convex hull a plane loop fills: 1 for a round or square section, far less
    for a band that curves round a bend."""
    from scipy.spatial import ConvexHull, QhullError

    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    w = np.cross(n, u)
    xy = np.c_[(P - c) @ u, (P - c) @ w]
    try:
        hull = float(ConvexHull(xy).volume)                 # a 2-D hull's "volume" is its area
    except (QhullError, ValueError):
        return 1.0
    return min(area / hull, 1.0) if hull > 0 else 1.0


def _shape(wh: tuple[float, float], area: float) -> str:
    w, h = wh
    if w <= 0 or h <= 0:
        return "other"
    if abs(w - h) <= 0.08 * max(w, h) and abs(area - math.pi * w * h / 4.0) <= 0.12 * area:
        return "circle"
    if abs(area - w * h) <= 0.12 * area:
        return "rectangle"
    return "other"


def _chain(ids, ea, eb, at, V):
    """Closed loops walked along the given edges; where more than two meet, the walk goes on along
    the straightest. A walk that does not close gives its edges back to the others."""
    used = np.zeros(len(ids), dtype=bool)
    for start in range(len(ids)):
        if used[start]:
            continue
        used[start] = True
        v0 = int(ea[start])
        prev, cur = v0, int(eb[start])
        path_v, path_e = [v0], [start]
        closed = False
        for _ in range(len(ids)):
            if cur == v0:
                closed = True
                break
            path_v.append(cur)
            cands = [k for k in at[cur] if not used[k]]
            if not cands:
                break
            k = cands[0]
            if len(cands) > 1:
                d_in = V[cur] - V[prev]
                d_in = d_in / (np.linalg.norm(d_in) or 1.0)
                score = -9.0
                for c in cands:
                    nxt = int(eb[c]) if int(ea[c]) == cur else int(ea[c])
                    d_out = V[nxt] - V[cur]
                    sc = float(np.dot(d_in, d_out / (np.linalg.norm(d_out) or 1.0)))
                    if sc > score:
                        k, score = c, sc
            used[k] = True
            path_e.append(k)
            prev, cur = cur, (int(eb[k]) if int(ea[k]) == cur else int(ea[k]))
        if closed and len(path_v) >= 3:
            yield np.asarray(path_v), ids[np.asarray(path_e)]
        else:
            used[np.asarray(path_e[1:], dtype=np.int64)] = False


def _measure(P: np.ndarray):
    """A closed loop measured in its own plane: area centroid, unit normal (Newell), area, the
    smallest box around it (long side first) and how far it strays from the plane."""
    if len(P) < 3:
        return None
    q = np.roll(P, -1, axis=0)
    n = np.array([np.sum((P[:, 1] - q[:, 1]) * (P[:, 2] + q[:, 2])),
                  np.sum((P[:, 2] - q[:, 2]) * (P[:, 0] + q[:, 0])),
                  np.sum((P[:, 0] - q[:, 0]) * (P[:, 1] + q[:, 1]))])
    L = float(np.linalg.norm(n))
    if L <= 0:
        return None
    n = n / L
    mean = P.mean(axis=0)
    u = np.cross(n, [0.0, 0.0, 1.0] if abs(n[2]) < 0.9 else [0.0, 1.0, 0.0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    x, y = (P - mean) @ u, (P - mean) @ v
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    cr = x * y1 - x1 * y
    a2 = float(cr.sum())
    if abs(a2) <= 0:
        return None
    cx, cy = float(((x + x1) * cr).sum()) / (3 * a2), float(((y + y1) * cr).sum()) / (3 * a2)
    c = mean + cx * u + cy * v
    dev = float(np.max(np.abs((P - c) @ n)))
    return c, n, abs(a2) / 2.0, _box(x - cx, y - cy), dev


def _box(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """The smallest box around a plane loop over the directions of its own edges."""
    n = len(x)
    best = None
    for i in range(0, n, max(1, n // 90)):
        j = (i + 1) % n
        ax, ay = x[j] - x[i], y[j] - y[i]
        L = math.hypot(ax, ay)
        if L <= 0:
            continue
        ax, ay = ax / L, ay / L
        s, t = x * ax + y * ay, -x * ay + y * ax
        w, h = float(s.max() - s.min()), float(t.max() - t.min())
        if best is None or w * h < best[0] * best[1]:
            best = (w, h)
    if best is None:
        return 0.0, 0.0
    return (best[0], best[1]) if best[0] >= best[1] else (best[1], best[0])


def _thin(P: np.ndarray, k: int = LOOP_POINTS) -> np.ndarray:
    if len(P) <= k:
        return P
    return P[np.linspace(0, len(P), k, endpoint=False).astype(int)]


def _distinct(found: list[tuple[Hole, set]]) -> list[Hole]:
    """One hole per opening. A bolt hole beside the bore in the same end face goes. Of two mouths
    facing the same way on one axis, one just behind the other - a counterbore, a socket's step,
    a nozzle's throat seen through its bell - the one behind goes: it is the same opening seen
    from outside. Two side by side in one plane (a coaxial fitting's two ports) both stay."""
    found = sorted(found, key=lambda hb: hb[0].area, reverse=True)
    gone = set()
    for i, (h, band) in enumerate(found):
        for j, (k, kb) in enumerate(found[:i]):
            if j in gone:
                continue
            if h.area <= SIDE_HOLE_AREA * k.area and band & kb:
                gone.add(i)
                break
    for i, (h, _) in enumerate(found):
        for j, (k, _) in enumerate(found):
            if i == j or i in gone or j in gone or float(h.normal @ k.normal) < 0.95:
                continue
            r = h.centroid - k.centroid
            depth = float(r @ k.normal)                      # how far h stands out beyond k
            side = float(np.linalg.norm(r - depth * k.normal))
            big = max(h.equivalent_diameter, k.equivalent_diameter)
            if side <= 0.2 * big and 0.02 * big < -depth <= big:
                gone.add(i)                                  # h sits behind k: k is the mouth
                break
    return [h for i, (h, _) in enumerate(found) if i not in gone]


__all__ = ["Cap", "Hole", "find_caps", "find_holes", "surface", "CONE_DEG", "MIN_LOOP", "RIM_SHARE", "SHARP_DEG"]
