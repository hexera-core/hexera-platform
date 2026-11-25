# Responsibility: Keep a refused gate's facts beside its attempt, where the next attempt's plan reads them (LAD-cfd-8).
# Boundaries: a small record in the attempt workspace; it decides nothing - the plans read it, the builder carries it.
# Collaborates with: pipeline/executor (writes it on a refusal), agents/builder/attempt.CARRY_FILES (carries it),
# engines/snappy/drivers and engines/cfmesh/cfmesh_runner (read the openings to refine).
"""A LEVER THE PLAN NEVER READS IS NO LEVER (LAD-cfd-8, rulebook 75).

The gates record what they refused in structured facts (the port-area gate's openings and their built
areas, the CAD fidelity record), and until now the next attempt was handed only the gate's words. The
facts now stay beside the attempt, and the engines' plans read the ones they can act on: an opening the
port-area gate refused is refined at the opening on the next attempt."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

#: The record beside the attempt (carried to the next one by the builder).
GATE_FACTS_FILE = "gate_facts.json"
#: The facts kept: what a plan can act on, and what the delivery may say.
_KEPT = ("misses", "fidelity", "detail", "checks", "patches", "relative_next")


def write_gate_facts(workspace, gate: str, facts: Mapping | None) -> None:
    """The refused gate's facts beside the attempt. Never raises."""
    if not workspace or not gate:
        return
    try:
        rec = {"gate": str(gate), "facts": {k: v for k, v in dict(facts or {}).items() if k in _KEPT}}
        (Path(workspace) / GATE_FACTS_FILE).write_text(json.dumps(rec, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001 - a record never changes a run
        logger.debug("gate facts not written", exc_info=True)


#: The gates whose refusal names openings the next attempt refines: an opening built at the wrong size (port_areas,
#: its `misses`), and an opening the mesh came back with no faces on (manifest_valid, its `patches`: the mesher lost
#: it - abc 00063401's 933 mm2 outlet_3, lost on two cfMesh attempts of the same plan, 24 and 23 minutes each).
MISSED_PORT_GATES = ("port_areas", "manifest_valid")


def missed_ports(workspace) -> set[str]:
    """The openings the previous attempt's refusal named (MISSED_PORT_GATES): their names, or an empty set."""
    try:
        rec = json.loads((Path(workspace) / GATE_FACTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(rec, dict) or rec.get("gate") not in MISSED_PORT_GATES:
        return set()
    raw = rec.get("facts")
    facts: dict = raw if isinstance(raw, dict) else {}
    if rec.get("gate") == "manifest_valid":
        return {str(n) for n in facts.get("patches") or () if n}
    return {str(m.get("name")) for m in facts.get("misses") or () if isinstance(m, dict) and m.get("name")}


def relative_stack_next(workspace) -> bool:
    """CFDREQ-33 amended (rulebook 113): the previous attempt's layer-stack refusal was of a stack written in metres
    that may be written relative on the plain wall cell (its `relative_next`) - this attempt writes it so."""
    try:
        rec = json.loads((Path(workspace) / GATE_FACTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(rec, dict) or rec.get("gate") != "layer_stack":
        return False
    facts = rec.get("facts")
    return isinstance(facts, dict) and facts.get("relative_next") is True


__all__ = ["GATE_FACTS_FILE", "MISSED_PORT_GATES", "missed_ports", "relative_stack_next", "write_gate_facts"]
