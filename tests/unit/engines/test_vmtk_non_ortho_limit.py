# Responsibility: Verify a VMTK flow mesh is held to the same 70-degree non-orthogonality production limit as the
# other flow meshers: past it, delivered with concerns and the number (engines/verdict_limits, VER-2).
# Boundaries: the verdict rule's inputs and outcome only; no mesher runs.
from __future__ import annotations

from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import verdict_limits as vl


def _verdict(non_ortho: float) -> tuple[str, list]:
    mf = {"quality": {"mesh_ok": True, "cells": 1_688_489, "max_non_ortho": non_ortho, "regions": 1}}
    inputs = vl.inputs_for(engine="vmtk", topology="internal", manifest=mf, valid=True)
    out, _rule = dr.verdict(inputs)
    return out, [r["key"] for r in dr.missed_limits(inputs)]


def test_vmtk_past_70_degrees_reads_concerns_with_the_number():
    # abc 00096107 on the LAD-cfd-11 proof run: VMTK delivered at 81.6 degrees, read "pass" before
    out, missed = _verdict(81.61)
    assert out == "concerns" and "max_non_ortho" in missed


def test_vmtk_inside_70_degrees_is_not_flagged_for_it():
    out, missed = _verdict(64.0)
    assert "max_non_ortho" not in missed


def test_the_vmtk_limit_is_the_other_flow_meshers_limit():
    vmtk = [lim.limit for lim in vl.limits_for("vmtk", "internal", layers_planned=False)
            if lim.key == "max_non_ortho"]
    assert vmtk == [70.0]
    snappy = [lim.limit for lim in vl.limits_for("snappy", "internal", layers_planned=False)
              if lim.key == "max_non_ortho"]
    assert snappy == [70.0]
