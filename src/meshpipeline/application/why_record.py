# Responsibility: Build a finished run's "why" record from its final state: the engine it started on, every engine switch, and the attempt it delivered, each with the rule (docs/RULES.md) behind it.
# Owns: reading those decisions back out of the run's own records (the fallback ladder record, the delivery account) into contracts/decision_record entries.
# Boundaries: reads only; it decides nothing and changes no state. A record that cannot be built is empty, never an error.
# Collaborates with: contracts/decision_record (the entry shape, the rules, the words), pipeline/engine_fallback (the ladder record it reads), application/review_delivery (the delivery account), application/run_reproducibility (writes the result).
from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from meshpipeline.contracts import decision_record as dr

logger = logging.getLogger(__name__)


def _mapping(v: Any) -> Mapping:
    return v if isinstance(v, Mapping) else {}


def _source_format(state: Mapping) -> str:
    from meshpipeline.contracts.intake_formats import format_for_suffix
    geo = _mapping(state.get("geometry"))
    ref = _mapping(geo.get("ref"))
    for name in (geo.get("local_path"), ref.get("original_filename"), ref.get("suffix_hint")):
        text = str(name or "").strip()
        if not text:
            continue
        suffix = text.lower() if text.startswith(".") else PurePath(text).suffix.lower()
        fmt = format_for_suffix(suffix)
        if fmt is not None:
            return fmt.key
    return ""


def _own_format(candidates: list[str], fmt: str) -> list[str]:
    if not fmt:
        return []
    from meshpipeline.engines.fitness import reads_own_format
    return [e for e in candidates if reads_own_format(e, fmt)]


_PICK_WORDS = {
    "ENG-4": "it ranked first on its lab record for {shape}{reason}.",
    "ENG-4-thin": "it ranked first for {shape}, on little lab evidence{reason}.",
    "ENG-6": "the user named it, so it is never switched on its own.",
    "ENG-7": "it reads the uploaded {fmt} file itself.",
    "ENG-8": "nothing was measured or named, so the run took the declared default for its flow.",
    "ENG-9": "the user confirmed it; the measured ranking put {top} first.",
}


def engine_pick_entry(state: Mapping) -> dr.Entry | None:
    from meshpipeline.pipeline.engine_fallback import engine_source
    ladder = _mapping(state.get("engine_ladder"))
    approved = str(ladder.get("approved") or "")
    if not approved:
        first = next((a for a in (ladder.get("attempts") or ())
                      if isinstance(a, Mapping) and a.get("engine")), None)
        approved = str((first or {}).get("engine") or state.get("engine") or "")
    if not approved:
        return None
    order = [str(e) for e in (ladder.get("order") or ()) if e]
    fmt = _source_format(state)
    inputs = {"engine_source": engine_source(state), "approved": approved,
              "recommended": str(ladder.get("order_recommended") or ""),
              "measured": ladder.get("order_source") == "measured",
              "own_format": _own_format(order or [approved], fmt)}
    value, rule = dr.engine_pick(inputs)
    reasons = _mapping(ladder.get("order_reasons"))
    reason = str(reasons.get(approved) or "")
    # a ranking with little or no record for this shape class still ranks: say how little
    thin = rule == "ENG-4" and reason.startswith(("little", "not yet", "no "))
    words = _PICK_WORDS["ENG-4-thin" if thin else rule].format(
        shape=str(ladder.get("order_shape") or "shapes like this one"),
        reason=f": {reason}" if reason else "", fmt=fmt.upper() or "source",
        top=inputs["recommended"] or "no engine")
    return dr.Entry(stage=dr.STAGE_ENGINE_PICK, decided="engine", value=value, rule=rule,
                    inputs={**inputs, "order_source": ladder.get("order_source") or "",
                            "order_table": ladder.get("order_table") or "", "source_format": fmt},
                    alternatives=[{"engine": e, "reason": str(reasons.get(e) or "")}
                                  for e in order if e != value],
                    source=dr.engine_pick_source(rule), words=words, also=dr.ENGINE_PICK_ALSO)


def switch_entries(state: Mapping) -> list[dr.Entry]:
    out = []
    for s in _mapping(state.get("engine_ladder")).get("switches") or ():
        if not isinstance(s, Mapping):
            continue
        inputs = {"why": str(s.get("why") or ""), "from": str(s.get("from") or ""),
                  "to": str(s.get("to") or ""), "attempt": s.get("attempt"),
                  "because": s.get("because"), "kind": s.get("kind")}
        value, rule = dr.switch(inputs)
        out.append(dr.Entry(
            stage=dr.STAGE_SWITCH, decided="engine", value=value, rule=rule, inputs=inputs,
            alternatives=[inputs["from"]], source=dr.switch_source(rule), also=dr.SWITCH_ALSO,
            words=(f"attempt {s.get('attempt')} moved from {inputs['from']} to {inputs['to']}: "
                   f"{inputs['why']}" + (f" ({s.get('reason')})" if s.get("reason") else "") + ".")))
    return out


def _delivered_a_mesh(state: Mapping) -> bool:
    return state.get("executor_success") is True and not state.get("executor_failed_gate")


def delivered_entry(state: Mapping) -> dr.Entry | None:
    account = _mapping(_mapping(state.get("engine_ladder")).get("delivered_from"))
    if account:
        pool = [{"attempt": c.get("attempt"), "engine": c.get("engine"), "rank": c.get("rank")}
                for c in (account.get("candidates") or ()) if isinstance(c, Mapping)]
        inputs = {"candidates": pool}
        # the rule the delivery itself named (review_delivery cites it); an older account that
        # named none is read by the same rule from its candidates
        rule = str(account.get("rule") or dr.delivered(inputs)[1])
        return dr.Entry(stage=dr.STAGE_DELIVERED, decided="attempt",
                        value=_int(account.get("attempt")), rule=rule, inputs=inputs,
                        alternatives=[c["attempt"] for c in pool
                                      if c["attempt"] != account.get("attempt")],
                        words=f"attempt {account.get('attempt')} ({account.get('engine')}): "
                              f"{account.get('why') or ''}.")
    if not _delivered_a_mesh(state):
        return None
    attempt = _int(state.get("retry_count")) or 0
    inputs = {"candidates": [{"attempt": attempt, "engine": str(state.get("engine") or ""),
                              "rank": []}]}
    return dr.Entry(stage=dr.STAGE_DELIVERED, decided="attempt", value=attempt,
                    rule=dr.DELIVERY_ONLY_ONE, inputs=inputs,
                    words=f"attempt {attempt} ({state.get('engine')}) is the only valid mesh "
                          "the run built.")


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def entries(state: Mapping) -> list[dr.Entry]:
    out: list[dr.Entry] = []
    for part in (engine_pick_entry, switch_entries, delivered_entry):
        try:
            got = part(state)
        except Exception:  # noqa: BLE001 - a record never touches a verdict
            logger.warning("why record: %s could not be read", part.__name__, exc_info=True)
            continue
        if isinstance(got, list):
            out.extend(got)
        elif got is not None:
            out.append(got)
    return out


def build(job_id: str, state: Mapping) -> dict:
    """The run's `why` section: its decisions in order, the rulebook version, and the sentences."""
    rows: list[Any] = [*entries(state), *dr.cited_for(job_id)]
    return dr.record(rows)


__all__ = ["build", "delivered_entry", "engine_pick_entry", "entries", "switch_entries"]
