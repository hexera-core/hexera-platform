# Responsibility: The OCCT half of the catastrophic-loss measurement (lane B22).
# Owns: the `v4_volume_loss` kernel call - per-input-body COMMON volume with the output, and the part's.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. It measures and reports
#             numbers; the 10% threshold and the refusal are `loss.py`, in the parent.
"""How much of the input shape the output shape still occupies.

The measure for one input body is `volume(Common(body, output))`: the part of that body the output still
fills. Not a volume ratio - see loss.py for why a ratio cannot tell a fuse from a deletion.

The part's own number is the SUM of the measured bodies' common volumes over the sum of their volumes, so
it is the same measurement at the part level and cannot disagree with the per-body one.

Cost: one Boolean per input body, bbox-prefiltered (a body whose box does not even touch the output's box
keeps nothing, and that needs no Boolean). A body whose Boolean does not complete is reported None -
UNMEASURED, never 0.0: "the Boolean failed" and "the body is gone" must not look alike to a rule that
refuses deliveries.
"""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register
from meshpipeline.cad.repair_v4.ws_kernel import read_brep

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


@register("v4_volume_loss")
def _volume_loss(payload: dict) -> dict:
    """Per-input-body and whole-part KEPT volume of `repaired` against `original` (both BREP paths).

    Returns `{"bodies": {"S<k>": kept fraction or None}, "part_kept": .., "volume_ratio": .., "bodies_total":
    .., "solidless": ..}`. `solidless` (the input has no solids) means the rule does not apply; the caller
    (`loss.from_kernel`) applies the threshold.
    """
    start = time.perf_counter()
    original = read_brep(payload["original"])
    repaired = read_brep(payload["repaired"])
    max_bodies = int(payload.get("max_bodies") or 64)
    in_solids = _solids(original)
    out_solids = _solids(repaired)
    vol_in = _volume(original)
    vol_out = _volume(repaired)
    if not in_solids or vol_in <= 0.0:
        # no volume to keep: a loose-faces or shell input. Reported, never refused (loss.py).
        return {"solidless": True, "bodies": {}, "bodies_total": len(in_solids), "part_kept": None,
                "volume_ratio": None, "seconds": time.perf_counter() - start}
    volumes = [abs(_volume(s)) for s in in_solids]
    order = sorted(range(len(in_solids)), key=lambda i: volumes[i], reverse=True)[:max_bodies]
    bodies: dict[str, Any] = {}
    kept_sum = 0.0
    vol_sum = 0.0
    for i in sorted(order):
        name = f"S{i + 1}"
        if volumes[i] <= 0.0:
            # a zero-volume input "body" has nothing to keep; it is layer 1's `non_positive_volume`, not ours
            bodies[name] = None
            continue
        tools = out_solids if len(out_solids) <= _PER_BODY_TOOLS else [repaired]
        try:
            common = _common_volume(in_solids[i], tools)
        except Exception:  # noqa: BLE001 - one body that cannot be measured says so; the others still stand
            common = None
        if common is None:
            bodies[name] = None
            continue
        bodies[name] = min(1.0, common / volumes[i])
        kept_sum += min(common, volumes[i])
        vol_sum += volumes[i]
    return {
        "solidless": False,
        "bodies": bodies,
        "bodies_total": len(in_solids),
        "bodies_after": len(out_solids),
        # the same measurement at the part level: the measured bodies' kept volume over their own volume
        "part_kept": None if vol_sum <= 0.0 else kept_sum / vol_sum,
        # reported only, and deliberately not what the rule reads (loss.py's docstring says why)
        "volume_ratio": None if vol_in <= 0.0 else vol_out / vol_in,
        "unmeasured_why": "too_many_bodies" if len(in_solids) > max_bodies else None,
        "seconds": time.perf_counter() - start,
    }
