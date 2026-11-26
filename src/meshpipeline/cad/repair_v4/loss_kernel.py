# Responsibility: The OCCT half of the catastrophic-loss measurement (lane B22).
# Owns: the `v4_volume_loss` kernel call - per-input-body COMMON volume with the output, and the part's.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. It measures and reports
#             numbers; the 10% threshold and the refusal are `loss.py`, in the parent.
"""How much of the input shape the output shape still occupies.

The measure for one input body is `volume(Common(body, output))`: the part of that body the output still
fills. Not a volume ratio - see loss.py for why a ratio cannot tell a fuse from a deletion.

The part's own number is the SUM of the measured bodies' common volumes over the sum of their volumes, so
it is the same measurement at the part level and cannot disagree with the per-body one.

A body whose Boolean does not complete is reported None - UNMEASURED, never 0.0: "the Boolean failed" and "the
body is gone" must not look alike to a rule that refuses deliveries.

COST (CRS v5 r2h, owner decision 2026-11-26: the guard is made CHEAP, its rule untouched). It used to run one
Boolean per input body on every candidate (~190 s per candidate on b25999e0f8be, 300 s timeouts on f62ed385ec02):
  1. the INPUT side - the original's solids oriented positive, their volumes, boxes and topology counts, layer 1's
     self-intersection answer per body, and each body's tessellated interior sample - depends on the original only,
     so it is computed ONCE per file (`_input_side`: memoised in the worker, and persisted beside the original so
     every worker of the pool and every later round reuse it) instead of once per candidate;
  2. an input body the output still holds UNCHANGED is kept 1.0 WITHOUT a Boolean: an output solid with the same
     face/edge/vertex counts, the same volume and the same box to `SAME_REL` (`same_body`). This is the exact
     screen: a body a candidate did not touch is the same solid in the same place, and Common of a body with
     its own copy is the body (the measured paths could only ever read it ~1.0, or fail);
  3. only the bodies the screen cannot vouch for are measured, exactly as before (Common, the tessellated
     measurement for a self-intersecting body or one Common calls lost), and - when there are several - in
     parallel kernel calls (`loss.check_loss` fans them out with `bodies`).
Bbox-prefiltered as before: a body whose box does not touch an output solid's box gets no Boolean against it.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import read_brep

#: the exact screen's tolerance: volumes within this fraction of the body's volume, box corners within this fraction
#: of the body's box diagonal (and identical topology counts) are the same body (owner: "1e-9 relative")
SAME_REL = 1e-9

#: Below this fraction of the input's whole volume a body is measured against the output's bodies one at a
#: time rather than against the whole compound: OCCT's Common is markedly faster on two solids than on a
#: solid and a 200-body compound, and the sum over the output's bodies is the same volume.
_PER_BODY_TOOLS = 32


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, found)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def _box(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box


def _positive(solid) -> tuple[Any, float]:
    """`solid` with its volume POSITIVE, and that volume. ORIENTATION IS NOT GEOMETRY: a solid whose faces point
    inward encloses the same point set, and `GProp` reports its volume negative.

    WHY THIS IS NOT COSMETIC (lane C20, on 0051_00510567): `BRepAlgoAPI_Common` against an INSIDE-OUT solid
    returns its COMPLEMENT, so everything OUTSIDE the inverted solid counted as kept. The delivered BREP of that
    part holds a -13.461 mm3 solid, and B22 read `part_kept` 0.697 for a delivery holding 0.07% of the part - it
    refused the file only because four other bodies happened to fall wholly outside. An inverted solid that
    covered the rest would have made the rule DELIVER a destroyed fragment, which is the thing the owner ruled
    against. Re-oriented, the same call reads 0.0004 kept and all 8 bodies lost.

    Returns `(None, volume)` when reversing does not make it positive (a zero or degenerate piece): the caller
    then measures nothing rather than measuring a complement.
    """
    volume = _volume(solid)
    if volume > 0.0:
        return solid, volume
    flipped = solid.Reversed()
    flipped_volume = _volume(flipped)
    if flipped_volume > 0.0:
        return flipped, flipped_volume
    return None, volume


def _common_volume(body, tools: list) -> float | None:
    """`volume(Common(body, tools))`, or None when a Boolean did not complete (UNMEASURED, not zero).

    The tools are intersected with the body one at a time and the volumes summed. The output's bodies may
    legitimately touch (that is what conformalise makes) but may not overlap - layer 1 reports
    `body_overlap` as a hard finding - so the pieces cannot double-count. An output that IS overlapping is
    invalid and already unshippable, and over-counting there can only make a loss look smaller, never
    invent one, so this cannot refuse a state it should not.
    """
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    total = 0.0
    body_box = _box(body)
    for tool in tools:
        if body_box.IsOut(_box(tool)):
            continue
        common = BRepAlgoAPI_Common(body, tool)
        if not common.IsDone():
            return None
        total += abs(_volume(common.Shape()))
    return total


def _counts(shape) -> tuple[int, int, int]:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out = []
    for kind in (TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX):
        found = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, found)
        out.append(found.Extent())
    return out[0], out[1], out[2]


def _signature(solid, volume: float) -> tuple:
    """(faces, edges, vertices, |volume|, box corners) - what `same_body` compares. The box is the geometry's own
    (no triangulation read), so a mesh left on one copy cannot make two identical solids look different."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(solid, box, False)
    corners = tuple(float(v) for v in box.Get()) if not box.IsVoid() else ()
    return (*_counts(solid), abs(float(volume)), corners)


def same_body(a: tuple, b: tuple, rel: float = SAME_REL) -> bool:
    """True when signatures `a` (an input body) and `b` (an output solid) are the SAME solid in the same place: equal
    topology counts, volumes within `rel` of `a`'s, every box corner within `rel` of `a`'s box diagonal. Pure."""
    if a[:3] != b[:3] or not a[4] or not b[4]:
        return False
    va, vb = a[3], b[3]
    if va <= 0.0 or abs(va - vb) > rel * va:
        return False
    lo, hi = a[4][:3], a[4][3:]
    diag = sum((h - lo_) ** 2 for lo_, h in zip(lo, hi, strict=True)) ** 0.5
    return all(abs(x - y) <= rel * diag for x, y in zip(a[4], b[4], strict=True))


@dataclass
class _InputSide:
    """Everything the rule needs from the ORIGINAL, computed once per file (CRS v5 r2h)."""

    path: str
    stamp: tuple
    raw: list
    fixed: list
    solids: list
    volumes: list[float]
    vol_in: float
    unorientable: list[str]
    inverted: int
    sigs: list[tuple]
    boxes: list
    #: lazily filled, persisted beside the original: body index -> layer 1's self-intersection answer / the
    #: tessellated interior sample (`tess_kernel.inside_sample`)
    self_int: dict[int, bool] = field(default_factory=dict)
    samples: dict[int, dict] = field(default_factory=dict)


#: the worker's input sides (a handful of files at most: one per file the worker serves at a time)
_INPUTS: OrderedDict[tuple, _InputSide] = OrderedDict()
_INPUTS_MAX = 4
#: the worker's output-solid triangulations, keyed by signature: a solid unchanged across candidates is meshed once
_TRIS: OrderedDict[tuple, list] = OrderedDict()
_TRIS_MAX = 512


def _stamp(path: str) -> tuple:
    st = os.stat(path)
    return (path, st.st_size, st.st_mtime_ns)


def _sidecar(path: str) -> str:
    return f"{path}.loss-input.json"


def _input_side(path: str) -> _InputSide:
    stamp = _stamp(path)
    got = _INPUTS.get(stamp)
    if got is not None:
        _INPUTS.move_to_end(stamp)
        return got
    original = read_brep(path)
    raw = _solids(original)
    vol_in = _volume(original)
    # EVERY Boolean runs on POSITIVELY ORIENTED solids (see `_positive`). An inside-out solid makes `Common` return a
    # complement, which is how this rule came to read 0.697 for a 0.07% delivery.
    fixed = [_positive(s) for s in raw]
    solids = [s if s is not None else r for (s, _v), r in zip(fixed, raw, strict=True)]
    side = _InputSide(path=path, stamp=stamp, raw=raw, fixed=fixed, solids=solids,
                      volumes=[abs(v) for _s, v in fixed], vol_in=vol_in,
                      unorientable=[f"S{i + 1}" for i, (s, _v) in enumerate(fixed) if s is None],
                      inverted=sum(1 for (s, _v), r in zip(fixed, raw, strict=True) if s is not None and s is not r),
                      sigs=[_signature(s, v) for s, (_p, v) in zip(solids, fixed, strict=True)],
                      boxes=[_box(s) for s in solids])
    _load_sidecar(side)
    _INPUTS[stamp] = side
    while len(_INPUTS) > _INPUTS_MAX:
        _INPUTS.popitem(last=False)
    return side


def _load_sidecar(side: _InputSide) -> None:
    """Another worker's (or an earlier round's) input-side answers, when they were made on THIS original (same
    size, mtime and body signatures). Anything unreadable is ignored: it is recomputed, never trusted."""
    try:
        with open(_sidecar(side.path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    if data.get("stamp") != [side.stamp[1], side.stamp[2]] or data.get("bodies") != len(side.raw):
        return
    side.self_int.update({int(k): bool(v) for k, v in (data.get("self_int") or {}).items()})
    side.samples.update({int(k): v for k, v in (data.get("samples") or {}).items()})


def _save_sidecar(side: _InputSide) -> None:
    """Persist what this worker computed, merged with what is already there (write-then-rename: a reader never sees
    a torn file). A failure costs only a recomputation elsewhere."""
    _load_sidecar(side)
    tmp = f"{_sidecar(side.path)}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stamp": [side.stamp[1], side.stamp[2]], "bodies": len(side.raw),
                       "self_int": {str(k): v for k, v in side.self_int.items()},
                       "samples": {str(k): v for k, v in side.samples.items()}}, fh)
        os.replace(tmp, _sidecar(side.path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _body_self_intersects(side: _InputSide, i: int, payload: dict) -> bool:
    """Layer 1's rule on input body `i` (the payload's own list when the caller knows it), once per file."""
    given = payload.get("self_intersecting")
    if given is not None:
        return f"S{i + 1}" in {str(s) for s in given}
    if i not in side.self_int:
        from meshpipeline.cad.repair_v4.verdict.valid_kernel import _self_intersects_once

        try:
            side.self_int[i] = bool(_self_intersects_once(side.solids[i]))
        except Exception:  # noqa: BLE001 - an unanswerable check claims nothing (Common stands, as before)
            side.self_int[i] = False
        _save_sidecar(side)
    return side.self_int[i]


def _sample(side: _InputSide, i: int) -> dict:
    """Input body `i`'s tessellated interior sample, once per file."""
    if i not in side.samples:
        from meshpipeline.cad.repair_v5 import tess_kernel as K

        sample = K.inside_sample(K.triangles(side.solids[i]))
        sample["inner"] = [list(p) for p in sample["inner"]]
        side.samples[i] = sample
        _save_sidecar(side)
    return side.samples[i]


def _tris_of(solid, sig: tuple) -> list:
    from meshpipeline.cad.repair_v5 import tess_kernel as K

    got = _TRIS.get(sig)
    if got is None:
        got = K.triangles(solid)
        _TRIS[sig] = got
        while len(_TRIS) > _TRIS_MAX:
            _TRIS.popitem(last=False)
    else:
        _TRIS.move_to_end(sig)
    return got


@register("v4_volume_loss")
def _volume_loss(payload: dict) -> dict:
    """Per-input-body and whole-part KEPT volume of `repaired` against `original` (both BREP paths).

    Returns `{"bodies": {"S<k>": kept fraction or None}, "part_kept": .., "volume_ratio": .., "bodies_total":
    .., "solidless": ..}`. `solidless` (the input has no solids) means the rule does not apply; the caller
    (`loss.from_kernel`) applies the threshold.

    CRS v5 r2h: `screened` names the bodies the exact screen kept unchanged (no Boolean). `bodies` (a list of `S<k>`)
    restricts the call to those input bodies, and `defer_over` (an int) makes a call whose changed bodies number
    more than that measure none of them and return them as `deferred` - `loss.check_loss` then measures them in
    parallel calls and merges (`kept_sum`/`vol_sum` are the part-level sums for that merge).
    """
    start = time.perf_counter()
    side = _input_side(payload["original"])
    repaired = read_brep(payload["repaired"])
    max_bodies = int(payload.get("max_bodies") or 64)
    # the rule's floor, only to know when Common's answer needs a second measurement (loss.py applies the rule)
    keep_fraction = float(payload.get("keep_fraction") or 0.10)
    raw_in, in_solids, volumes, fixed_in, vol_in = side.raw, side.solids, side.volumes, side.fixed, side.vol_in
    unorientable_in, inverted_in = side.unorientable, side.inverted
    raw_out = _solids(repaired)
    vol_out = _volume(repaired)
    if not raw_in or vol_in <= 0.0:
        # no volume to keep: a loose-faces or shell input. Reported, never refused (loss.py).
        return {"solidless": True, "bodies": {}, "bodies_total": len(raw_in), "part_kept": None,
                "volume_ratio": None, "seconds": time.perf_counter() - start}

    fixed_out = [_positive(s) for s in raw_out]
    out_solids = [s for s, _v in fixed_out if s is not None]
    unorientable_out = sum(1 for s, _v in fixed_out if s is None)
    inverted_out = sum(1 for (s, _v), raw in zip(fixed_out, raw_out, strict=True) if s is not None and s is not raw)
    if unorientable_out:
        # a solid in the OUTPUT that cannot be made positive would still turn its Common into a complement, and
        # this rule must never silently measure one. Nothing is claimed about this output (loss.py refuses
        # nothing on an unmeasured report; layer 1 reports the negative solid on its own account).
        return {"solidless": False, "bodies": {}, "bodies_total": len(raw_in), "bodies_after": len(raw_out),
                "part_kept": None, "volume_ratio": None if vol_in <= 0.0 else vol_out / vol_in,
                "unmeasured_why": f"{unorientable_out} output solid(s) could not be oriented positive",
                "inverted": {"input": inverted_in, "output": inverted_out,
                             "unorientable_input": unorientable_in, "unorientable_output": unorientable_out},
                "seconds": time.perf_counter() - start}
    out_sigs = [_signature(s, v) for s, v in fixed_out]
    order = sorted(range(len(in_solids)), key=lambda i: volumes[i], reverse=True)[:max_bodies]
    only = {str(b) for b in payload["bodies"]} if payload.get("bodies") is not None else None
    bodies: dict[str, Any] = {}
    kept_sum = 0.0
    vol_sum = 0.0
    screened: list[str] = []
    changed: list[int] = []
    for i in sorted(order):
        name = f"S{i + 1}"
        if only is not None and name not in only:
            continue
        if fixed_in[i][0] is None or volumes[i] <= 0.0:
            # a zero-volume input "body" has nothing to keep; it is layer 1's `non_positive_volume`, not ours
            bodies[name] = None
            continue
        if any(same_body(side.sigs[i], sig) for sig in out_sigs):
            # the exact screen: the output holds this body unchanged, so it keeps all of it - no Boolean
            bodies[name] = 1.0
            kept_sum += volumes[i]
            vol_sum += volumes[i]
            screened.append(name)
            continue
        changed.append(i)
    defer_over = payload.get("defer_over")
    deferred = [f"S{i + 1}" for i in changed] if defer_over is not None and len(changed) > int(defer_over) else []
    # the parent measures the deferred bodies in parallel calls; this call measures only what it was not asked to defer
    tessellated: dict[str, Any] = {}
    out_tris = [(s, sig) for (s, _v), sig in zip(fixed_out, out_sigs, strict=True) if s is not None]
    for i in [] if deferred else changed:
        name = f"S{i + 1}"
        # v5 step 8 (owner decision A): a body LAYER 1 SAYS SELF-INTERSECTS is measured by tessellation, never Common
        if _body_self_intersects(side, i, payload):
            got = _kept_tessellated_cached(side, i, out_tris)
            tessellated[name] = got
            if got.get("kept") is None:
                bodies[name] = None
                continue
            bodies[name] = min(1.0, float(got["kept"]))
            vol = float(got.get("volume_mc_mm3") or volumes[i])
            kept_sum += bodies[name] * vol
            vol_sum += vol
            continue
        # the compound shortcut is only safe when NOTHING had to be re-oriented: the compound carries the
        # original orientations, and one inverted solid inside it would make the whole Common a complement
        tools = (out_solids if (len(out_solids) <= _PER_BODY_TOOLS or inverted_out or unorientable_out)
                 else [repaired])
        try:
            common = _common_volume(in_solids[i], tools)
        except Exception:  # noqa: BLE001 - one body that cannot be measured says so; the others still stand
            common = None
        if common is None:
            bodies[name] = None
            continue
        if common < keep_fraction * volumes[i]:
            # step 7/8: Common read a body as lost that the plain volumes say is still there (a PURE SPLIT of a valid
            # body read "kept 0.85%" at a 5e-14 volume change). The tessellated measurement decides instead; it is a
            # real measurement, so this trigger cannot let a real loss through (a deleted body's points are not in
            # the output, whatever the total volume does).
            # CRS v5 round 2: EVERY body Common reads as lost is confirmed this way, not only when the part's volume
            # contradicts it. 7b13ec12fee5 (step 13): a located defeature of B3 took 0.19% off the part, so the
            # volume test could not fire, and S1/S10 read "kept 0.0" - while both are in the output unchanged (same
            # volume and centre; Common returns 0 even against the body's own copy; tessellation keeps 0.999 / 1.0).
            # The rule (10%) is unchanged; only a refusal now rests on a measurement that saw the loss.
            got = _kept_tessellated_cached(side, i, out_tris)
            got["trigger"] = (f"Common kept {common / volumes[i]:.3g} vs volume ratio {vol_out / vol_in:.9g}"
                              + ("" if common_contradicts_volumes(common, volumes[i], vol_in, vol_out, keep_fraction)
                                 else " (confirming a Common loss)"))
            tessellated[name] = got
            if got.get("kept") is None:
                bodies[name] = None
                continue
            bodies[name] = min(1.0, float(got["kept"]))
            kept_sum += bodies[name] * volumes[i]
            vol_sum += volumes[i]
            continue
        bodies[name] = min(1.0, common / volumes[i])
        kept_sum += min(common, volumes[i])
        vol_sum += volumes[i]
    return {
        "solidless": False,
        "bodies": bodies,
        "bodies_total": len(raw_in),
        "bodies_after": len(raw_out),
        # the same measurement at the part level: the measured bodies' kept volume over their own volume
        "part_kept": None if vol_sum <= 0.0 else kept_sum / vol_sum,
        # reported only, and deliberately not what the rule reads (loss.py's docstring says why)
        "volume_ratio": None if vol_in <= 0.0 else vol_out / vol_in,
        "unmeasured_why": "too_many_bodies" if len(in_solids) > max_bodies else None,
        # what had to be re-oriented, so a reader can tell a re-oriented measurement from a plain one
        "inverted": {"input": inverted_in, "output": inverted_out,
                     "unorientable_input": unorientable_in, "unorientable_output": 0},
        # the MEASURED input bodies layer 1 says self-intersect, and their tessellated measurement (v5 step 8)
        "self_intersecting": [f"S{i + 1}" for i in changed if side.self_int.get(i)],
        "tessellated": tessellated,
        # CRS v5 r2h: kept 1.0 by the exact screen (no Boolean), left to the parent's parallel calls, and the sums
        # the parent merges those calls with
        "screened": screened,
        "deferred": deferred,
        "kept_sum": kept_sum,
        "vol_sum": vol_sum,
        "seconds": time.perf_counter() - start,
    }


def common_contradicts_volumes(common: float, body_volume: float, vol_in: float, vol_out: float,
                               keep_fraction: float) -> bool:
    """True when Common says this body fell under `keep_fraction` while the PLAIN volumes say far less was lost: the
    volume Common calls lost is more than twice the part's whole volume drop. Pure, unit-tested."""
    if body_volume <= 0.0 or common / body_volume >= keep_fraction:
        return False
    lost_by_common = body_volume - common
    return lost_by_common > 2.0 * max(vol_in - vol_out, 0.0)


def _kept_tessellated_cached(side: _InputSide, i: int, out_tris: list) -> dict:
    """What the output kept of input body `i`, by tessellation (`repair_v5.tess_kernel`): the body's interior sample
    (computed once per file) checked for containment in the output solids near it (their meshes memoised by
    signature), plus Monte-Carlo and mesh volumes. `kept` None = unmeasured (no inside sample, or tessellation failed).
    The same arithmetic as `tess_kernel.kept_by_sampling` on the same triangles; only where it is computed moved."""
    from meshpipeline.cad.repair_v5 import tess_kernel as K

    try:
        sample = _sample(side, i)
        box = side.boxes[i]
        near = [t for s, sig in out_tris if not box.IsOut(_box(s)) for t in _tris_of(s, sig)]
        got = K.kept_from_sample(sample, near)
    except Exception as exc:  # noqa: BLE001 - unmeasured, never a zero
        return {"kept": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
    return {k: (round(v, 9) if isinstance(v, float) else v) for k, v in got.items()}
