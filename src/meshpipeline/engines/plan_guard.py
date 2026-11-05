# Responsibility: Keep the rules plan when the AI builder's configured plan is measurably worse - planned over the cell limit where the rules plan is not - and say so to the builder, the log and the why record (AIG-2).
# Owns: the comparison of the AI plan's planned cells with the rules plan's, the choice between them and its words.
# Boundaries: renders nothing itself - the caller's `render` writes a case and returns the engine's configure result; every estimate is the engine's own (EngineSpec.planned_cells) and the rules plan's arguments are engines/rules_plan's. Acts only on proof: no estimate, no rules plan or a failed rules render leaves the AI plan in force.
# Collaborates with: agents/builder/tools/meshing.configure_mesh (calls it after validation, with a render seam), engines/rules_plan.configure_args (what the rules plan sends), contracts/decision_record (the why record).
"""THE AI PLAN IS NEVER WORSE THAN THE RULES PLAN (docs/RULES.md AIG-2).

configure_mesh validates the AI builder's strategy against the engine's own grammar (keys, types,
ranges), but a strategy inside every bound can still plan a mesh no job can build: on a carotid
tube the AI's cfMesh strategy made 32.7 M cells against the 8 M one-job limit, where the rules plan
(the requested layers, the engine's own sizes) passed clean at well under a million.

So before an AI plan is used, its PLANNED cells - the engine's own estimate of the case it just
wrote - are compared with the limit. Over it, the rules plan is written instead and read the same
way; when the rules plan fits, it stays, the builder is told so in plain words (so it does not
fight it), and the why record says why. In every other case the AI plan stands: an estimate that
cannot be read, an engine with no rules arguments to send, a rules plan that fails or is no better.
The guard only ever acts on a measured difference.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RULE = "AIG-2"


def _mcells(n: float) -> str:
    return f"{n / 1e6:.1f} M" if n >= 1e5 else f"{n / 1e3:.0f} k"


def worse(ai_cells: int | None, rules_cells: int | None, limit: int) -> bool:
    """The AI plan is worse: it plans over the cell limit and the rules plan does not. Unknown on
    either side is never worse."""
    if ai_cells is None or rules_cells is None or not limit or limit <= 0:
        return False
    return int(ai_cells) > int(limit) >= int(rules_cells)


def words(ai_cells: int, rules_cells: int, limit: int) -> str:
    return (f"This strategy plans about {_mcells(ai_cells)} cells, over the {_mcells(limit)} cell "
            f"limit of one job; the rules plan (about {_mcells(rules_cells)} cells) was configured "
            f"instead (rule {RULE}).")


def _same(ai_strategy: Mapping[str, Any], rules_args: Mapping[str, Any]) -> bool:
    return {k: v for k, v in dict(ai_strategy or {}).items() if v is not None} == dict(rules_args)


def keep_no_worse(*, engine: str, workspace: Path, ai_strategy: Mapping[str, Any],
                  ai_result: dict, render: Callable[[dict | None], dict], limit: int,
                  job_id: str = "") -> dict:
    """The configure result to use: `ai_result` (the AI plan, already written) unless it plans over
    `limit` and the rules plan - written by `render(rules_args)` - does not. When the rules plan is
    written but not kept, the AI plan is written again (`render(None)`), so what is on disk is
    always what the returned result describes."""
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.rules_plan import configure_args
    spec = get_spec(engine)
    ai_cells = spec.planned_cells(ai_result)
    if ai_cells is None or ai_cells <= int(limit):
        return ai_result
    rules_args = configure_args(engine, workspace)
    if rules_args is None or _same(ai_strategy, rules_args):
        # no rules plan to set against it here, or it IS the rules plan: it stands as planned
        return ai_result
    rules_result: dict | None = None
    try:
        rules_result = render(dict(rules_args))
    except Exception:  # noqa: BLE001 - a rules render that fails leaves the AI plan in force
        logger.warning("plan guard: the rules plan could not be configured", exc_info=True)
    rules_cells = (spec.planned_cells(rules_result)
                   if isinstance(rules_result, dict) and rules_result.get("success") else None)
    if rules_result is None or rules_cells is None or not worse(ai_cells, rules_cells, int(limit)):
        logger.info("plan guard: the AI plan (%s cells) stands - the rules plan reads %s",
                    ai_cells, rules_cells)
        return render(None)          # the AI plan, written back over the rules plan's files
    said = words(ai_cells, rules_cells, int(limit))
    logger.warning("plan guard (%s): %s engine=%s job_id=%s", RULE, said, engine, job_id)
    try:
        from meshpipeline.contracts import decision_record as dr
        dr.cite(job_id, stage="builder", decided="which plan configured the mesh",
                value="rules plan", rule=RULE,
                inputs={"engine": engine, "ai_planned_cells": int(ai_cells),
                        "rules_planned_cells": int(rules_cells), "cell_limit": int(limit),
                        "ai_strategy": dict(ai_strategy or {})},
                alternatives=["the AI plan"], words=said)
    except Exception:  # noqa: BLE001 - a record never changes the decision
        logger.debug("plan guard: why record not kept", exc_info=True)
    return {**rules_result,
            "rules_plan_used": True, "rule": RULE,
            "ai_plan": {"planned_cells": int(ai_cells), "strategy": dict(ai_strategy or {})},
            "note": said,
            "next": (said + " The mesh is configured with the rules plan: call run_mesh NOW. Do "
                     "NOT configure a finer strategy again - a plan over the limit cannot be "
                     "built in one job, and it would be replaced the same way.")}


__all__ = ["RULE", "keep_no_worse", "words", "worse"]
