# Responsibility: CRS v5 step 12: the AGENT entrant source of the per-defect tournament. A model is shown a
#                 defect-local brief and proposes up to 3 candidates (a repair from the class menu + parameters inside
#                 the menu's ranges); anything else is rejected and logged.
# Owns: `AgentSource` (a paid `tournament.EntrantSource`), `menu_rows` (the menu with its parameter ranges),
#       `check_proposals`, `explores`, `PROPOSE_TOOL`, the caps.
# Boundaries: no op, no check, no ranking: the tournament runs what this returns through the same heats as every
#             other entrant. Model calls go through v4's route, admitted and settled on the file's ledger
#             (`model_route.metered_call`), so the agent, describe_ai and the search_ai fallback share one $1 per
#             file. Region facts and the picture are kernel calls (`v5_agent_region`, v4's `render_view`). It never
#             raises: a refused or failed call is an event in `events` and the tournament goes on without it.
"""The agent tier (owner decisions 2026-11-24/25: "agent <=3 from the class menu with a defect-local brief", "only when
no free entrant wins, or a small random exploration share", "$0.15/defect, $1/file").

WHEN IT ENTERS: the tournament asks it after the free tiers (rule seed, class menu) ran their heats. It enters when
none of them survived, and on a deterministic EXPLORATION_SHARE of the defects they did solve (a hash of the file,
the defect id, code and pair: the same run makes the same choice, so runs reproduce).

WHAT IT MAY PROPOSE: a repair ON THE MENU (by its name, `op` or `op+op`), with parameters only from that repair's
TUNABLE set and inside its range (`menu_rows`): enums from the op's schema, a [min, max] from the schema, or - for a
parameter the schema leaves open - a decade either side of the value the class's rule derived from the measurement.
Targets are never the model's: they are derived from the defect exactly as for the rule seed. A proposal that names
something else, a parameter out of range, a repeat of an entrant already run, or a fourth candidate, is REJECTED and
logged as a `rejected` match with the reason.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meshpipeline.cad.repair_v4 import seq as Q
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.ops import REGISTRY
from meshpipeline.cad.repair_v5 import tournament as T

#: owner caps: per defect (summed over every round it is played in) and per file (the ledger's file ceiling, shared
#: with describe_ai and the search_ai fallback)
DEFECT_CAP_USD = 0.15
FILE_CAP_USD = 1.00
#: at most this many agent candidates per tournament
MAX_PROPOSALS = 3
#: the share of the defects the free tiers already solved that the agent still enters (exploration)
EXPLORATION_SHARE = 0.10
#: the output allowance of one agent call: 3 candidates with a reason each are ~400 tokens; 1500 keeps one call's
#: reservation inside the $0.15 cap on the dearest route ($50/M out on gpt-6-astra = $0.075)
MAX_OUTPUT_TOKENS = 1500
RENDER_PX = 256
#: a parameter the schema leaves open may move this factor either side of the value the rule derived
OPEN_RANGE_FACTOR = 10.0
#: an open length with nothing derived to centre on (fuzzy 0): up to this fraction of the part's diagonal
OPEN_LENGTH_OF_DIAGONAL = 1e-3

ENTERS_UNSOLVED = "no_free_winner"
ENTERS_EXPLORING = "exploration"

_SYSTEM = """You choose repair candidates for ONE defect of a CAD part (B-rep) that blocks tetrahedral meshing
(gmsh, Netgen) or B-rep validity. You are given the defect (class, measured cause, the involved faces and bodies with
their sizes and gaps), the class's repair MENU with the parameters you may set and their allowed ranges, what the
rule-based entrants already tried on this defect and how each ended, and possibly a picture of the region.
Propose up to 3 candidates by calling propose_repairs once. Each candidate is one repair named exactly as on the
menu, with parameters only from its "tunable" set and inside the given ranges (omit a parameter to keep the rule's
derived value). Do not repeat a candidate that was already tried with the same parameters. Prefer the candidate most
likely to remove the defect without changing the part's shape or volume. Propose nothing if nothing on the menu can
help."""

PROPOSE_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "propose_repairs",
        "description": "Propose up to 3 repair candidates from the menu.",
        "parameters": {
            "type": "object",
            "properties": {"candidates": {"type": "array", "maxItems": MAX_PROPOSALS, "items": {
                "type": "object",
                "properties": {"repair": {"type": "string"}, "params": {"type": "object"},
                               "why": {"type": "string"}},
                "required": ["repair", "params", "why"]}}},
            "required": ["candidates"],
            "additionalProperties": False,
        },
    },
}


def defect_key(defect: Defect, pair: Any = None) -> str:
    return f"{defect.id}|{defect.code}|{'+'.join(pair) if pair else ''}"


def explores(file_id: str, defect: Defect, pair: Any = None, share: float = EXPLORATION_SHARE) -> bool:
    """Deterministic: the same file, defect and pair always draw the same."""
    h = int(hashlib.sha256(f"{file_id}|{defect_key(defect, pair)}".encode()).hexdigest()[:8], 16)
    return h % 10_000 < share * 10_000


# --- the menu, with what the model may set -------------------------------------------------------------------


def _tunable(op: str, derived: dict, defect: Defect, targets: list) -> dict[str, dict]:
    """{param: {"enum": [...]} | {"type", "min", "max"}} for the scalar parameters of `op` the model may set."""
    schema = (getattr(REGISTRY.get(op), "params_schema", None) or {}).get("properties", {}) or {}
    diag = (defect.measurement or {}).get("diagonal_mm")
    out: dict[str, dict] = {}
    for key, spec in schema.items():
        kind = spec.get("type")
        if "enum" in spec:
            out[key] = {"enum": list(spec["enum"])}
        elif kind == "string" and key == "mode" and op == "conformalise":
            bodies = [t for t in targets if str(t)[:1] == "B"]
            out[key] = {"enum": ["fragment", *[f"cut_by:{b}" for b in bodies]]}
        elif kind in ("number", "integer"):
            lo, hi = spec.get("minimum"), spec.get("maximum")
            value = derived.get(key)
            if hi is None and isinstance(value, (int, float)) and value > 0:
                lo, hi = max(lo if lo is not None else 0.0, value / OPEN_RANGE_FACTOR), value * OPEN_RANGE_FACTOR
            elif hi is None and diag and key.endswith("_mm"):
                hi = float(diag) * OPEN_LENGTH_OF_DIAGONAL
            if lo is None or hi is None or hi < lo:
                continue                    # nothing bounds it: derived only, never the model's
            out[key] = {"type": kind, "min": float(lo), "max": float(hi)}
    return out


def menu_rows(brief: T.Brief) -> list[dict]:
    """The menu as the model sees it: per repair its name, whether it is the rule seed, whether it can be aimed at
    this defect, its derived parameters (or why none), and the parameters the model may set."""
    live = set(brief.ws.ids) if getattr(brief.ws, "ids", None) else None
    rows = []
    for i, repair in enumerate(brief.menu):
        row: dict[str, Any] = {"repair": "+".join(repair), "rule_seed": i == 0}
        why_not = next((r for r in (Q.aim_reason(op, brief.defect) for op in repair if not op.startswith("run_stage:"))
                        if r), None)
        if why_not:
            row["unaimable"] = why_not
        if len(repair) == 1 and not repair[0].startswith("run_stage:"):
            try:
                targets, derived = Q.params_for(repair[0], brief.defect, pair=brief.pair, live=live)
                row["derived"] = json.loads(json.dumps(derived, default=str))
                row["tunable"] = _tunable(repair[0], derived, brief.defect, targets)
            except Q.NoParameters as why:
                row["no_parameters"] = str(why)
        rows.append(row)
    return rows


def _in(value: Any, spec: dict) -> bool:
    if "enum" in spec:
        return any(value == v or (isinstance(value, (int, float)) and isinstance(v, (int, float))
                                  and math.isclose(value, v, rel_tol=1e-9)) for v in spec["enum"])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    if spec.get("type") == "integer" and float(value) != int(value):
        return False
    return spec["min"] <= value <= spec["max"]


def check_proposals(raw: Any, rows: list[dict], menu: tuple) -> list[T.Entrant]:
    """Every proposal as an Entrant (tier agent): runnable, or `rejected` with the reason. A repeat of a free entrant
    is the tournament's to reject (it holds the keys)."""
    by_name = {r["repair"]: (repair, r) for repair, r in zip(menu, rows, strict=True)}
    out: list[T.Entrant] = []
    taken: set[tuple] = set()
    for n, item in enumerate(raw if isinstance(raw, list) else []):
        name = str(item.get("repair", "")) if isinstance(item, dict) else ""
        params = item.get("params") if isinstance(item, dict) else None
        why = str(item.get("why", ""))[:200] if isinstance(item, dict) else ""
        repair, row = by_name.get(name, ((name,) if name else ("?",), None))
        params = params if isinstance(params, dict) else {}

        def reject(reason: str, repair: tuple = repair, params: dict = params, why: str = why) -> None:
            out.append(T.Entrant(tuple(repair), T.AGENT, (dict(params),) if params else (), note=why,
                                 rejected=reason))

        if n >= MAX_PROPOSALS:
            reject(f"over the agent's {MAX_PROPOSALS} candidates")
            continue
        if row is None:
            reject(f"{name or 'nothing'!r} is not on the class menu ({', '.join(by_name)})")
            continue
        if row.get("unaimable"):
            reject(f"the menu said it cannot be aimed here: {row['unaimable']}")
            continue
        tunable = row.get("tunable") or {}
        bad = [k for k in params if k not in tunable]
        if bad:
            reject(f"{', '.join(map(str, bad))} not tunable for {name} (tunable: {', '.join(tunable) or 'none'})")
            continue
        off = [f"{k}={params[k]!r} outside {tunable[k]}" for k in params if not _in(params[k], tunable[k])]
        if off:
            reject("; ".join(off))
            continue
        derived = row.get("derived") or {}
        moved = {k: v for k, v in params.items() if derived.get(k) != v}
        entrant = T.Entrant(tuple(repair), T.AGENT, (moved,) if moved else (), note=why)
        if entrant.key in taken:
            reject("repeats an earlier candidate of this proposal")
            continue
        taken.add(entrant.key)
        out.append(entrant)
    return out


# --- the brief ------------------------------------------------------------------------------------------------


def _refs(ws: Any, ids: list) -> list[dict]:
    kinds = {"F": "face", "E": "edge", "B": "solid"}
    indices = getattr(ws, "indices", None) or {}
    return [{"id": e, "type": kinds[str(e)[0]], "index": indices[e]} for e in ids
            if str(e)[:1] in kinds and e in indices]


def involved(defect: Defect, pair: Any = None) -> list:
    aim = list((defect.cause or {}).get("aim") or [])
    return list(dict.fromkeys([*(pair or ()), *defect.entities, *aim, *Q.cause_bodies(defect)]))


def _tried_rows(tried: tuple) -> list[dict]:
    return [{"tier": m.tier, "repair": m.op, "params": m.params, "result": m.result, "heat": m.heat,
             "why": (m.reason or "")[:200]} for m in tried]


def messages_for(brief: T.Brief, rows: list[dict], region: dict | None, png: bytes | None) -> list[dict]:
    d = brief.defect
    facts = {"defect": {"class": d.cls, "code": d.code, "kind": d.kind, "member": d.member, "rule": d.rule,
                        "cause": json.loads(json.dumps(d.cause, default=str)),
                        "measurement": json.loads(json.dumps(
                            {k: v for k, v in (d.measurement or {}).items() if k != "free_loop_edges"},
                            default=str)),
                        "pair": list(brief.pair) if brief.pair else None, "points": [list(p) for p in d.xyz[:4]]},
             "region": region or {"unmeasured": True},
             "menu": rows, "already_tried": _tried_rows(brief.tried),
             "free_entrant_won": brief.solved}
    text = json.dumps(facts, default=str, separators=(",", ":"))
    user: Any = text
    if png:
        user = [{"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def _proposals(reply: Any) -> list | str:
    if reply.failure_marker:
        return f"provider failure: {reply.failure_marker}"
    calls = [c for c in reply.tool_calls if c.name == "propose_repairs"]
    if not calls:
        return "the model did not call propose_repairs"
    try:
        got = json.loads(calls[0].arguments).get("candidates", [])
    except (ValueError, AttributeError):
        return "the model's arguments were not a JSON object"
    return got if isinstance(got, list) else "no candidates list"


# --- the source -----------------------------------------------------------------------------------------------


@dataclass
class AgentSource:
    """A PAID entrant source (`tournament.is_paid`): built per file, appended to the tournament's sources."""

    pool: Any
    ledger: Any
    file_id: str
    route: str
    job_id: str = ""
    file_cap_usd: float = FILE_CAP_USD
    defect_cap_usd: float = DEFECT_CAP_USD
    share: float = EXPLORATION_SHARE
    render: bool = True
    #: tests: the metered call (default `model_route.metered_call` on the ledger) and its pre-call estimate
    call: Any = None
    estimate: Callable[[list, list], float] | None = None
    trace: Any = None
    #: one row per time it was asked: defect, why it entered or not, $ spent, what came of it
    events: list[dict] = field(default_factory=list)
    spent_usd: float = 0.0
    _by_defect: dict[str, float] = field(default_factory=dict)

    paid = True

    def __call__(self, brief: T.Brief) -> list[T.Entrant]:
        key = defect_key(brief.defect, brief.pair)
        event: dict[str, Any] = {"defect": key, "class": brief.defect.cls, "code": brief.defect.code}
        try:
            return self._propose(brief, key, event)
        except Exception as exc:  # noqa: BLE001 - never raises: the tournament goes on without the agent
            event.update(outcome="error", why=f"{type(exc).__name__}: {exc}"[:300])
            return []
        finally:
            if event.get("why") != "solved":          # a solved defect it did not explore is not an event
                self.events.append(event)
                if self.trace is not None:
                    self.trace.record("v5_agent", **event)

    def _propose(self, brief: T.Brief, key: str, event: dict) -> list[T.Entrant]:
        if brief.solved and not explores(self.file_id, brief.defect, brief.pair, self.share):
            event.update(outcome="not_entered", why="solved")
            return []
        event["enters"] = ENTERS_EXPLORING if brief.solved else ENTERS_UNSOLVED
        rows = menu_rows(brief)
        if not any(not r.get("unaimable") for r in rows):
            event.update(outcome="not_entered", why="nothing on the menu can be aimed at this defect")
            return []
        region = self._region(brief)
        png = self._png(brief) if self.render else None
        messages = messages_for(brief, rows, region, png)
        left = self.defect_cap_usd - self._by_defect.get(key, 0.0)
        est = self._estimate(messages)
        if est is not None and est > left and png:
            messages, png = messages_for(brief, rows, region, None), None       # the picture is what can go
            est = self._estimate(messages)
        event.update(estimate_usd=None if est is None else round(est, 4), rendered=png is not None)
        if est is not None and est > left:
            event.update(outcome="refused", why=f"over the ${self.defect_cap_usd:g} defect cap "
                                                f"(estimate ${est:.3f}, ${left:.3f} left for this defect)")
            return []
        call = self.call
        if call is None:
            from meshpipeline.cad.repair_v4.model_route import metered_call

            call = metered_call(self.ledger, self.file_id, self.file_cap_usd, kind="v5_agent",
                                max_output_tokens=MAX_OUTPUT_TOKENS)
        try:
            priced = asyncio.run(call(messages, [PROPOSE_TOOL], route=self.route, job_id=self.job_id,
                                      tool_choice="required", cache_key="v5_agent"))
        except Exception as exc:  # noqa: BLE001 - LedgerRefused (file cap), provider errors: logged, not raised
            event.update(outcome="refused" if type(exc).__name__ == "LedgerRefused" else "error",
                         why=f"{type(exc).__name__}: {exc}"[:300])
            return []
        cost = float(getattr(priced, "cost_usd", 0.0) or 0.0)
        self.spent_usd += cost
        self._by_defect[key] = self._by_defect.get(key, 0.0) + cost
        event["usd"] = round(cost, 5)
        got = _proposals(priced.reply)
        if isinstance(got, str):
            event.update(outcome="error", why=got)
            return []
        out = check_proposals(got, rows, brief.menu)
        event.update(outcome="proposed", proposed=len(out), rejected=sum(bool(e.rejected) for e in out),
                     candidates=[{"repair": "+".join(e.repair), "params": [dict(o) for o in e.overrides],
                                  "rejected": e.rejected} for e in out])
        return out

    def _estimate(self, messages: list) -> float | None:
        if self.estimate is not None:
            return float(self.estimate(messages, [PROPOSE_TOOL]))
        try:
            from meshpipeline.cad.repair_v4.proposer import estimate_call_usd
            from meshpipeline.contracts.model_inference import repair_v4_route_prices

            return estimate_call_usd(messages, [PROPOSE_TOOL], prices=repair_v4_route_prices(self.route),
                                     output_tokens=MAX_OUTPUT_TOKENS)[0]
        except Exception:  # noqa: BLE001 - unpriced: the ledger's own admission still bounds the file
            return None

    def _region(self, brief: T.Brief) -> dict | None:
        refs = _refs(brief.ws, involved(brief.defect, brief.pair))
        if self.pool is None or not refs:
            return None
        res = self.pool.call("v5_agent_region", {"brep_path": brief.ws.brep_path, "entities": refs},
                             timeout_s=60.0)
        return res.value if getattr(res, "ok", False) and isinstance(res.value, dict) else None

    def _png(self, brief: T.Brief) -> bytes | None:
        refs = _refs(brief.ws, involved(brief.defect, brief.pair))
        if self.pool is None:
            return None
        res = self.pool.call("render_view", {"brep_path": brief.ws.brep_path, "entities": refs,
                                             "xyz": [list(p) for p in brief.defect.xyz[:8]], "size": RENDER_PX,
                                             "budget_s": 30.0}, timeout_s=60.0)
        b64 = (res.value or {}).get("png_b64") if getattr(res, "ok", False) and isinstance(res.value, dict) else None
        try:
            png = base64.b64decode(b64, validate=True) if b64 else None
        except ValueError:
            return None
        return png if png and png.startswith(b"\x89PNG") else None

    def summary(self) -> dict[str, Any]:
        """For the row: calls, $, how often it entered and why, what came of it."""
        import collections

        return {"asked": len(self.events), "spent_usd": round(self.spent_usd, 5),
                "enters": dict(collections.Counter(e.get("enters") for e in self.events if e.get("enters"))),
                "outcomes": dict(collections.Counter(e.get("outcome") for e in self.events)),
                "proposed": sum(int(e.get("proposed") or 0) for e in self.events),
                "rejected": sum(int(e.get("rejected") or 0) for e in self.events),
                "events": self.events[:40]}
