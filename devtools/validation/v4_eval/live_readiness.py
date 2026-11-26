#!/usr/bin/env python3
# Responsibility: Prove, with REAL paid model calls, that the v4 AI proposer's route works before any paid wave:
#                 (probe) one tool-and-image round, then two rounds on the proposer's real static prefix to read the
#                 cached-token count; (search) `repair_v4()` on the search_ai arm on one known-fixable injected case.
# Owns: the lane ledger (one persistent `WaveLedger.for_shard` log whose per-"file" ceilings bound this script's
#       total spend), the wire tap (every request and the provider's RAW response, usage included, to JSONL) and the
#       proposer transcript dump.
# Boundaries: it spends money, so it refuses to run without `--spend`. It changes no engine code: the search run
#             goes through the production entry point, with only the ledger (persisted instead of in-memory) and the
#             transcript capture swapped in around it.
"""Live readiness for the v4 AI proposer.

    OPENAI_API_KEY=... PYTHONPATH=src python devtools/validation/v4_eval/live_readiness.py probe --spend \\
        --out /tmp/lane-b --route openai:gpt-6-astra
    OPENAI_API_KEY=... PYTHONPATH=src python devtools/validation/v4_eval/live_readiness.py search --spend \\
        --out /tmp/lane-b --route openai:gpt-6-astra --usd 0.50        # needs the native image (OCCT)

Every call is admitted against `<out>/ledger.jsonl` BEFORE it is sent (file ids `probe` and `search`, each with
its own ceiling), so the ledger is the hard cap, not a hope. Rerunning appends to the same ledger: the ceilings
hold across runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

WAVE_ID = "live-readiness"
PROBE_CEILING_USD = 1.50


# --- the ledger and the wire tap --------------------------------------------------------------------------------------


def lane_ledger(out: Path) -> Any:
    from meshpipeline.cad.repair_v4.model_ledger import FileMarkerStore, WaveLedger
    return WaveLedger.for_shard("normal", wave_id=WAVE_ID, n_shards=1, shard_index=0, path=out / "ledger.jsonl",
                                markers=FileMarkerStore(out / "markers"))


def _strip_images(obj: Any) -> Any:
    """The request as logged: image data URLs replaced by their length, so the tap stays readable."""
    if isinstance(obj, dict):
        return {k: (f"<data url, {len(v)} chars>" if isinstance(v, str) and v.startswith("data:image") else
                    _strip_images(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_images(v) for v in obj]
    return obj


def install_tap(tap_path: Path) -> None:
    """Wrap the REAL provider client so every request and the provider's raw response (usage and all) land in
    `tap_path`. The router, pricing, retries and normalisation are untouched."""
    from meshpipeline.adapters.model_inference import providers

    real_client_for = providers.client_for

    def client_for(target: Any) -> Any:
        client = real_client_for(target)
        real_create = client.responses.create

        class _Responses:
            async def create(self, **request: Any) -> Any:
                t0 = time.time()
                response = await real_create(**request)
                dump = response.model_dump() if hasattr(response, "model_dump") else repr(response)
                with open(tap_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"at": t0, "seconds": round(time.time() - t0, 2),
                                         "request": _strip_images(request), "response": dump},
                                        default=str) + "\n")
                return response

        class _Client:
            responses = _Responses()

        return _Client()

    providers.client_for = client_for


def metered(ledger: Any, file_id: str, ceiling: float, log: list[dict]) -> Any:
    """A `call_repair_v4_propose` that asks the ledger first and settles the router's priced reply after."""
    from meshpipeline.cad.repair_v4.proposer import estimate_call_usd
    from meshpipeline.contracts.model_inference import (
        REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS,
        call_repair_v4_propose,
        repair_v4_route_prices,
    )

    async def call(messages: list, tools: list, *, route: str, **kw: Any) -> Any:
        est, _tok = estimate_call_usd(messages, tools, prices=repair_v4_route_prices(route),
                                      output_tokens=REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS)
        adm = ledger.admit(file_id, est, file_ceiling_usd=ceiling)
        if not adm.ok:
            raise RuntimeError(f"ledger refused the call ({adm.reason}); spent so far "
                               f"${ledger.file_spent_usd(file_id):.4f} of ${ceiling:.2f}")
        try:
            priced = await call_repair_v4_propose(messages, tools, route=route, **kw)
        except BaseException:
            ledger.release(adm.reservation)
            raise
        r = priced.reply
        if r.failure_marker:
            ledger.release(adm.reservation)
        else:
            ledger.settle(adm.reservation, file_id=file_id, job_id=kw.get("job_id", ""), provider=priced.provider,
                          model=priced.model, input_tokens=r.input_tokens,
                          cached_input_tokens=r.cached_input_tokens, output_tokens=r.output_tokens,
                          cost_usd=priced.cost_usd)
        log.append({"route": route, "model": priced.model, "estimate_usd": round(est, 6),
                    "cost_usd": round(priced.cost_usd, 6), "input": r.input_tokens, "cached": r.cached_input_tokens,
                    "output": r.output_tokens, "reasoning": getattr(r, "reasoning_tokens", None),
                    "finish": r.finish_reason, "failure": r.failure_marker,
                    "tools": [(c.name, c.arguments) for c in r.tool_calls]})
        return priced

    return call


def _setup(out: Path) -> Any:
    from meshpipeline.cad.repair_v4 import model_route
    from meshpipeline.contracts import inference_telemetry

    model_route.require_model_key()
    out.mkdir(parents=True, exist_ok=True)
    install_tap(out / "wire.jsonl")
    # the engine reaches gs:// and the wave router through neutral ports; this process composes them
    from meshpipeline.adapters.mesh_execution.gcs_exchange import bind_gcs_buckets
    from meshpipeline.adapters.model_inference.crs_wave import wave_router_parts

    bind_gcs_buckets()
    model_route.set_wave_router_parts(wave_router_parts)
    model_route.install_router()
    inference_telemetry.set_inference_telemetry(None)
    return lane_ledger(out)


# --- probe: tools + image, then the cache ----------------------------------------------------------------------------


async def _probe(route: str, out: Path) -> dict:
    from meshpipeline.cad.repair_v4.proposer import (
        DECLARE_UNFIXABLE,
        cache_key_for,
        probe_route,
        system_prompt,
        tool_schemas,
    )
    from meshpipeline.cad.repair_v4.stages import STAGES

    ledger = _setup(out)
    log: list[dict] = []
    call = metered(ledger, "probe", PROBE_CEILING_USD, log)
    probe = await probe_route(route, call=call)
    # Two rounds on the proposer's OWN static prefix (system prompt + tool schemas, the same cache key the
    # proposer sends), each with a tiny user turn. The second must report cached input tokens.
    names = [s.name for s in STAGES]
    system, tools = system_prompt(stage_names=names), tool_schemas(stage_names=names)
    key = cache_key_for(system, tools)
    cache_rounds = []
    for i in range(2):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f"Connectivity check {i + 1}. Call {DECLARE_UNFIXABLE} with site_id "
                                            "'S0' and reason 'probe'. Nothing else."}]
        await call(msgs, tools, route=route, job_id=f"live-readiness-cache-{i}", tool_choice="required",
                   cache_key=key)
        cache_rounds.append(log[-1])
        if i == 0:
            time.sleep(3)      # let the prefix land in the provider's cache
    return {"route": route, "probe_ok": probe.ok, "probe_detail": probe.detail, "calls": log,
            "cache_key": key, "second_call_cached_tokens": cache_rounds[-1]["cached"],
            "ledger_spent_usd": ledger.spent_usd(), "ledger_entries": ledger.entries()}


# --- search: repair_v4() on search_ai, one fixable case --------------------------------------------------------------


def _search(route: str, out: Path, usd: float, *, max_turns: int, ops: int, wall_s: float) -> dict:
    repo = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import inject as inj  # devtools/validation/v4_eval/inject.py
    from tests.native._v4_flush_patch_part import write_flush_patch_box

    import meshpipeline.cad.repair_v4 as v4
    from meshpipeline.cad.repair_v4 import search as search_mod

    ledger = _setup(out)
    before = ledger.file_spent_usd("search")
    if before + 1e-9 >= usd:
        raise SystemExit(f"the search ceiling ${usd:.2f} is already spent (${before:.4f}) in {out}/ledger.jsonl")
    case_dir = out / "case"
    case_dir.mkdir(exist_ok=True)
    clean = write_flush_patch_box(case_dir / "flush_patch_box.step")
    case = inj.inject(clean, "face_delete", 3, case_dir)       # the 3x3 mm flush square, as the e2e test makes it

    # The engine builds `WaveLedger.for_file(model_usd)`, in memory. Swap in the persistent lane ledger, under the
    # file id "search", so this run's spend is on disk and bounded with the probe's.
    proposers: list[Any] = []
    real_ai_kwargs, real_arm_generators = v4._ai_kwargs, search_mod.arm_generators

    def ai_kwargs(source: str, budget: Any) -> dict:
        kw = real_ai_kwargs(source, budget)
        return {**kw, "ledger": ledger, "file_id": "search"}

    def arm_generators(arm: str, **kw: Any) -> Any:
        gens, proposer = real_arm_generators(arm, **kw)
        if proposer is not None:
            proposers.append(proposer)
        return gens, proposer

    v4._ai_kwargs = ai_kwargs
    search_mod.arm_generators = arm_generators
    v4.ARM, v4.ROUTE = "search_ai", route
    budget = v4.Budget(wall_s=wall_s, ops=ops, model_calls=max_turns, model_usd=usd - before)
    t0 = time.monotonic()
    result = v4.repair_v4(str(case.path), str(out / "out.step"), budget=budget,
                          deadline_s=time.monotonic() + wall_s + 180)
    seconds = time.monotonic() - t0
    transcript = out / "transcript.json"
    if proposers:
        transcript.write_text(json.dumps(_strip_images(proposers[0].messages), indent=1, default=str))
    trace = json.loads(Path(result.trace_path).read_text()) if result.trace_path else {"events": []}
    (out / "trace.json").write_text(json.dumps(trace, indent=1, default=str))
    cands = [e for e in trace["events"] if e.get("kind") == "candidate"]
    return {"route": route, "case": str(case.path), "label": case.label, "seconds": round(seconds, 1),
            "delivered": result.delivered, "verdict_summary": result.verdict_summary,
            "proposer": proposers[0].result() if proposers else None,
            "candidates": [{k: e.get(k) for k in ("op", "source", "status", "state", "targets", "params")}
                           for e in cands],
            "stop": next((e for e in trace["events"] if e.get("kind") == "stop"), None),
            "search_spent_usd": ledger.file_spent_usd("search") - before,
            "transcript": str(transcript), "trace": str(out / "trace.json"), "wire": str(out / "wire.jsonl"),
            "ledger": str(out / "ledger.jsonl")}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=("probe", "search"))
    p.add_argument("--spend", action="store_true", help="required: this makes PAID model calls")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--route", default="openai:gpt-6-astra")
    p.add_argument("--usd", type=float, default=0.50, help="search: the file's model ceiling (<= 1.00)")
    p.add_argument("--turns", type=int, default=6)
    p.add_argument("--ops", type=int, default=12)
    p.add_argument("--wall-s", type=float, default=600.0)
    a = p.parse_args(argv)
    if not a.spend:
        p.error("refusing to make paid model calls without --spend")
    if a.what == "probe":
        report = asyncio.run(_probe(a.route, a.out))
    else:
        report = _search(a.route, a.out, a.usd, max_turns=a.turns, ops=a.ops, wall_s=a.wall_s)
    (a.out / f"{a.what}-report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
