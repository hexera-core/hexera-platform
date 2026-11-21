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
_KEPT = ("misses", "fidelity", "detail", "checks")


def write_gate_facts(workspace, gate: str, facts: Mapping | None) -> None:
    """The refused gate's facts beside the attempt. Never raises."""
    if not workspace or not gate:
        return
    try:
        rec = {"gate": str(gate), "facts": {k: v for k, v in dict(facts or {}).items() if k in _KEPT}}
        (Path(workspace) / GATE_FACTS_FILE).write_text(json.dumps(rec, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001 - a record never changes a run
        logger.debug("gate facts not written", exc_info=True)


def missed_ports(workspace) -> set[str]:
    """The openings the port-area gate refused on the previous attempt: their names, or an empty set."""
    try:
        rec = json.loads((Path(workspace) / GATE_FACTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(rec, dict) or rec.get("gate") != "port_areas":
        return set()
    raw = rec.get("facts")
    facts: dict = raw if isinstance(raw, dict) else {}
    return {str(m.get("name")) for m in facts.get("misses") or () if isinstance(m, dict) and m.get("name")}


__all__ = ["GATE_FACTS_FILE", "missed_ports", "write_gate_facts"]
