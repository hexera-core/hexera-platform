# Responsibility: the kernel half of P1 (lane C18): one key for "is layer 1's answer on this BREP the same as on that
#                 one", so the staged healer can reuse the standing state's layer-1 report instead of re-checking.
# Owns: the kernel function `layer1_gate`.
# Boundaries: runs inside a kernel worker only; OCP is imported inside functions. The per-solid key is A17's OWN
#             (`valid_kernel._shape_memo_key`: geometry_key + tolerances + reversed faces), called, never copied, so
#             the memo and this gate cannot drift apart.
"""Why the per-solid key alone is not enough, and what the two counts are for: layer 1 also judges what is NOT inside a
solid - a free shell's open edges (`open_shell` on H<k>), loose faces, the whole-shape BRepCheck pass. C18 measured
the per-solid rules missing exactly those (open_shell on free shells: 20+ states). The FACE count and the FREE-SHELL
count are the whole-shape facts an op can change without touching any solid's key, so a gate that compares only
solids would reuse a verdict a free-shell edit made stale."""

from __future__ import annotations

import hashlib

from meshpipeline.cad.repair_v4.kernel_calls import register


@register("layer1_gate")
def _layer1_gate(payload: dict) -> dict:
    """payload: `brep`. Returns `gate` (a hex digest) and the parts it was made of, for the trace."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad.repair_v4.verdict import valid_kernel as vk

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, payload["brep"], BRep_Builder()):
        raise ValueError(f"could not read BREP {payload['brep']}")
    solids = vk._children(shape, TopAbs_SOLID)
    keys = sorted(repr(vk._shape_memo_key(solid)) for solid in solids)  # A17's key; a multiset, order-free
    faces, free_shells = len(vk._children(shape, TopAbs_FACE)), len(vk._free_shells(shape))
    gate = hashlib.sha256(repr((keys, faces, free_shells)).encode()).hexdigest()
    return {"gate": gate, "solids": len(solids), "faces": faces, "free_shells": free_shells}
