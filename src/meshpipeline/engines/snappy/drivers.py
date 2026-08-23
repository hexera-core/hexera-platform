# Responsibility: Emit snappyHexMesh dictionaries from a declarative specification.
# Boundaries: the deterministic renderer: declared values become engine syntax the model never writes itself.
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import meshpipeline.engines.snappy.settings as scfg
import meshpipeline.settings.policy as polcfg
from meshpipeline.engines.workspace_facts import read_purpose

logger = logging.getLogger(__name__)

# The Builder's terminal contract. Imported from the Builder envelope so the two
# strategies cannot drift into two spellings of "the Builder finished with a mesh".
from meshpipeline.agents.builder.driver_run import BuilderDriverRun
from meshpipeline.contracts import execution_guard as _fence
from meshpipeline.contracts.event_stream import (
    ExecutionEventPublisher,
    StaleExecutionPublish,
)
from meshpipeline.engines.snappy.judge import (
    _judge_snappy,
)

# PipelineState is the inter-node contract (defined in graph.py). Imported under
# TYPE_CHECKING only - annotations are strings here, so this adds typing/IDE support
# without a runtime import (which would cycle: graph imports these node modules).
if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState





def _write_plan_memory(workspace: Path, strategy: dict) -> None:
    import json as _json
    import os as _os
    tmp = workspace / ".last_plan.json.tmp"
    try:
        tmp.write_text(_json.dumps(strategy))
        _os.replace(tmp, workspace / ".last_plan.json")
    except Exception:  # noqa: BLE001 - planner memory is best-effort, never fatal
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


async def _run_snappy_timed(R, workspace, cap, publish: ExecutionEventPublisher,
                            engine, purpose, *, run,
                            native_attempt: int = 1):
    import time as _t
    # FENCE FIRST. The trace opens only where ownership permits: a superseded
    # generation must not tell a reader it started a mesh it does not own.
    await run.fence("start native mesh")
    _op = await _op_begin(publish, "run_native_mesher", run, native_attempt)
    try:
        from meshpipeline.engines.mesh_history import estimate as _est
        await publish.ameshing(engine, cap, history=_est(engine, purpose))
    except StaleExecutionPublish:
        raise
    except Exception:  # noqa: BLE001
        pass
    _start = _t.monotonic()
    import asyncio as _a
    result = await _a.to_thread(R.run_snappy, workspace, timeout=cap)
    # BEFORE the result is judged, published, recorded or delivered. If ownership was
    # lost here, this raises and NO success result is published for the run above.
    await run.fence("accept native mesh output")
    await _op_end(publish, _op, "run_native_mesher",
            {"exit_code": result.get("rc")}, result.get("rc") == 0)
    _chk = await _op_begin(publish, "inspect_native_output", run, native_attempt)
    await _op_end(publish, _chk, "inspect_native_output",
            {"exit_code": result.get("rc")}, result.get("rc") == 0)
    if result.get("rc") == 0:
        # THE AUTHORITATIVE GATE: the mesher exited clean AND the acceptance fence
        # above granted this generation the right to accept it. Only here.
        try:
            from meshpipeline.contracts import rationale as _R
            await _R.abuilder_mesh_ready(publish, cells=result.get("cells"))
        except StaleExecutionPublish:
            raise
        except Exception:  # noqa: BLE001
            pass
        try:
            from meshpipeline.engines.mesh_history import record as _rec
            _rec(engine, purpose, _t.monotonic() - _start)
        except Exception:  # noqa: BLE001
            pass
    return result


def _plan_surface(state, workspace):
    from meshpipeline.cad.staging import staged_surface
    from meshpipeline.pipeline.geometry_state import materialized as _materialized
    return staged_surface(_materialized(state), Path(workspace) / "input.stl")


def _pass_shape(q: dict, wall_faces: int) -> str:
    _cells = f"{int(q['cells']):,}" if q.get("cells") else "no"
    _skew = int(q.get("skew_faces") or 0)
    return (f"{_cells} cells, {wall_faces:,} faces on the body"
            + (f", {_skew:,} skewed" if _skew else ", none skewed"))


async def _build_snappy_deterministic(workspace: Path, state: PipelineState, *, job_id: str,
                                      publish: ExecutionEventPublisher,
                                      initial_plan: dict | None,
                                      run: BuilderDriverRun) -> bool:
    import json as _json

    from meshpipeline.cad.analysis import analyze_surface, recommend_refinement

    # The staged surface is metres; this states which conversion produced it so the domain
    # bounds, refinement sizes and cell targets derived below are physical.
    from meshpipeline.cad.staging import staged_surface
    from meshpipeline.engines.snappy import snappy_runner as R
    from meshpipeline.engines.snappy.planner import clamp_cell_budget, plan_with_accounting
    from meshpipeline.engines.workspace_facts import contract_wall_patch as _contract_wall_patch
    from meshpipeline.pipeline.geometry_state import materialized as _materialized
    _surface = staged_surface(_materialized(state), workspace / "input.stl")
    analysis = analyze_surface(_surface)

    # SYMMETRY (3D external) comes in two shapes, told apart by how many patches were declared:
    # ONE is a half-model, cut on a plane, meshed on one side; TWO is a 2.5D slab - an extruded
    # section whose both end caps are symmetry planes. Either way, resolve it HERE and fail fast
    # if the geometry cannot carry what was declared, rather than build a mesh that dies on the
    # patch contract with "zero faces" and burns every retry after the mesh is already paid for.
    symmetry = None
    # EVERY declared symmetry patch, not just the first. Taking only the first silently dropped
    # the second one: blockMesh never created that face, the manifest contract still expected it,
    # and the run died on "zero faces" AFTER a production-grade mesh had already been built and
    # paid for. Two declared patches is the 2.5D slab case and needs both ends emitted.
    _sym_names = [p.get("name") for p in (state.get("intake_patches") or [])
                  if (p.get("type") or "").strip() == "symmetry" and p.get("name")]
    if _sym_names and (state.get("dimensionality") or "3D").upper() == "3D":
        # ONE declared patch is a half-model; TWO is a swept slab. Same decision, same two
        # outcomes, so they share this module's refusal and its note rather than adding sites:
        # every publication here is a certified user-facing message, and two ways of saying
        # "symmetry could not be placed" is one more than the reader needs.
        _slab = len(_sym_names) >= 2
        symmetry = (R.detect_slab_symmetry(analysis, _sym_names[0], _sym_names[1]) if _slab
                    else R.detect_symmetry_plane(analysis, _sym_names[0]))
        if symmetry is None:
            await publish.aerror(
                (f"Two symmetry boundaries were declared ('{_sym_names[0]}', '{_sym_names[1]}'), "
                 "which describes an extruded section meshed as a slab - but this geometry has "
                 "no pair of flat end caps to place them on."
                 if _slab else
                 f"The symmetry boundary '{_sym_names[0]}' was declared, but the geometry is not "
                 "a half-model - it has no coplanar cut face to place a symmetry plane on (a "
                 "full-span body straddles the centreline).")
                + " Mesh the full domain without symmetry, or supply a half-model.",
                op_id="snappy:symmetry-unusable")
            return False
        # `axis` is the INDEX every other consumer indexes with (snappy_runner's _BOX_FACES and
        # domain bounds); the reader is told which axis that is, not the number.
        await publish.anote(
            (f"Extruded slab detected - symmetry on both {'XYZ'[int(symmetry['axis'])]} end faces "
             f"('{symmetry['lo_name']}', '{symmetry['hi_name']}'); the domain is not padded along "
             "that axis."
             if _slab else
             f"Half-model detected - mirroring on the {'XYZ'[int(symmetry['axis'])]} cut face "
             f"and meshing one side only (boundary '{symmetry['name']}')"),
            op_id="snappy:symmetry-detected")

    plan = initial_plan
    # retry mode arrives with a prior failure but no pre-made plan → seed the first re-plan with it
    feedback = (state.get("classifier_result", {}) or {}).get("summary", "") \
        or state.get("reviewer_feedback", "")
    # PLANNER MEMORY: the plan the last attempt used, so the planner REVISES it (a delta) instead of
    # re-deriving the same numbers. Seed from the pre-made plan, or a plan carried from a prior
    # (across-node) attempt via .last_plan.json.
    previous_plan = initial_plan
    if previous_plan is None and (workspace / ".last_plan.json").exists():
        try:
            previous_plan = _json.loads((workspace / ".last_plan.json").read_text())
        except Exception:  # noqa: BLE001
            previous_plan = None
    max_attempts = int(scfg.MAX_SNAPPY_ATTEMPTS)
    _cap = 2400  # per-mesh cap; stays under the Cloud Run Job task-timeout
    last_valid = False   # last attempt produced a VALID (body-fitted, no fatal) mesh, if not clean

    for attempt in range(1, max_attempts + 1):
        if plan is None:   # repair: re-plan WITH the previous plan + critique (iterate with memory)
            _po = await plan_with_accounting(
                surface=_plan_surface(state, workspace),
                workspace=workspace, job_id=job_id,
                request_txt=state.get("request_txt", ""),
                mesh_fidelity=state.get("effective_mesh_fidelity", ""),
                prior_feedback=feedback, previous_plan=previous_plan,
                # a real model round deserves the same public lifecycle
                publish=publish, attempt=_attempt_of(state),
                # WHICH re-plan this is. Each meshing pass plans against its own failure, so the
                # pass number is what makes these separate operations rather than one operation
                # arriving three times with three different payloads.
                native_attempt=attempt)
            plan = _po.plan
            run.note_plan_round(_po.round)
        strategy = plan or {}
        previous_plan = strategy                       # remember for the next repair
        await run.fence("write plan memory")
        _mem = await _op_begin(publish, "author_configuration", run, attempt)
        _write_plan_memory(workspace, strategy)
        await publish.anote(f"Meshing pass {attempt} of {max_attempts} - "
                        f"{str(strategy.get('approach', 'default strategy'))[:80]}",
                op_id=f"snappy:pass-open:{attempt}")
        try:
            # CONFIGURE (deterministic)
            # Clamp the planner's budget to the compute ceiling (safety net - the planner is told
            # the ceiling, but this guarantees the mesh can't exceed what Cloud Run can build or what
            # the executor accepts, so an over-ambitious budget never wastes a run).
            _budget = clamp_cell_budget(strategy.get("max_cells"), ceiling=polcfg.CELL_HARD_LIMIT)
            strategy = {**strategy, "max_cells": _budget}
            rec = recommend_refinement(analysis, max_cells=_budget)
            dmin, dmax = R.domain_from_strategy(analysis, strategy, symmetry)
            wall = _contract_wall_patch(workspace) or "body"
            prep = R.prepare_surface(
                workspace, geometry_file="input.stl", domain_min=dmin, domain_max=dmax,
                wall_patch=wall, farfield_patch="farfield", feature_angle=150)
            await _op_end(publish, _mem, "author_configuration", {"stage": "plan"}, True)
            await run.fence("author mesh specification")
            _spec = await _op_begin(publish, "validate_configuration", run, attempt)
            summary = R.render_snappy_case(
                workspace, surface_name=prep["surface_name"], feature_file=prep["feature_file"],
                analysis=analysis, recommendation=rec, domain_min=dmin, domain_max=dmax,
                strategy=strategy, dimensionality=state.get("dimensionality", "3D"),
                symmetry=symmetry)
            # The case is authored, so the operation this pass opened is CLOSED. Without this the
            # reader was left with a validation that started every pass and never finished.
            await _op_end(publish, _spec, "validate_configuration",
                    {"surface_level": summary.get("surface_level")}, True)
            run.note_authoring()
            await publish.anote("Carving the body out of the background mesh - refinement level "
                            f"{summary['surface_level']}, {summary['n_layers']} boundary layers",
                    op_id=f"snappy:carving:{attempt}")
            # RUN (deterministic; Cloud Run Job / local) + user-facing instrumentation
            result = await _run_snappy_timed(
                R, workspace, _cap, publish, state.get("engine", "snappy"),
                read_purpose(workspace), run=run, native_attempt=attempt)
            # --- JUDGE (deterministic bar) --- past the post-native fence, so this generation
            # still owns the job and may accept, publish and record the result.
            q = R.check_mesh(workspace)
            await publish.ameshed(q.get("cells"))
            fc = R._patch_face_counts(workspace)
            wall_faces = sum(c for p, c in fc.items() if p != "farfield")
            production, reason = _judge_snappy(result, q, wall_faces)
            last_valid = bool(result.get("rc") == 0 and wall_faces > 0 and not q.get("fatal"))
            run.note_native_run(produced_usable_mesh=last_valid)
        except (_fence.StaleWorkerFenced, StaleExecutionPublish):
            # NOT a failed attempt. A newer generation owns this job; re-planning against a
            # "failure" that is really supersession would keep a zombie worker meshing. A
            # refused publication says the same thing at the other boundary.
            raise
        except Exception as exc:  # noqa: BLE001 - a config/run error is just a failed attempt
            logger.exception("snappy attempt %d errored - job_id=%s", attempt, job_id)
            production, reason, q, wall_faces = False, f"attempt errored: {type(exc).__name__}", {}, 0
            last_valid = False

        logger.info("snappy attempt %d - cells=%s wall_faces=%s skew_faces=%s -> %s (job_id=%s)",
                    attempt, q.get("cells"), wall_faces, q.get("skew_faces"),
                    "PRODUCTION-GRADE" if production else reason[:50], job_id)
        # the JUDGEMENT, in the engineer's language. `reason` is the driver's own re-plan
        # note (not another agent's private feedback), so it may be shown.
        _shape = _pass_shape(q, wall_faces)
        if production:
            await publish.anote(f"Pass {attempt} produced a production-grade mesh - {_shape}",
                    op_id=f"snappy:pass-outcome:{attempt}")
        else:
            await publish.anote(f"Pass {attempt} fell short - {_shape}; re-planning ({reason[:60]})",
                    op_id=f"snappy:pass-outcome:{attempt}")
        if production:
            return True                    # valid mesh is in the workspace; executor takes over
        feedback, plan = reason, None      # re-plan against the concrete failure next attempt

    # Exhausted the attempts. Hand a best-effort VALID mesh to the executor/reviewer (they make the
    # final delivery call); only a genuinely invalid last mesh (no body captured) is a hard fail.
    logger.warning("snappy build exhausted %d attempts - job_id=%s (last_valid=%s)",
                   max_attempts, job_id, last_valid)
    if last_valid:
        await publish.anote("Meshing passes exhausted - submitting the best mesh built "
                     "(valid, but not as clean as we aim for)",
                op_id="snappy:passes-exhausted")
    return last_valid


async def _build_internal_deterministic(workspace: Path, state: PipelineState, *, job_id: str,
                                        publish: ExecutionEventPublisher, source_path: str,
                                        initial_plan: dict | None,
                                        run: BuilderDriverRun) -> bool:
    import asyncio as _asyncio
    import json as _json
    import math as _math

    from meshpipeline.engines.snappy import snappy_runner as R
    from meshpipeline.engines.snappy.planner import clamp_cell_budget, plan_with_accounting

    # tessellate the fluid solid ONCE (geometry is fixed across attempts; only strategy changes)
    if not (source_path and Path(source_path).exists()):
        logger.error("internal build: no CAD file to tessellate - job_id=%s", job_id)
        return False
    if Path(source_path).suffix.lower() == ".stl":
        logger.error("internal build: needs a CAD SOLID (STEP/IGES), got an STL - job_id=%s", job_id)
        return False
    try:
        # The coordinate state staging would have supplied. This path tessellates the CAD itself
        # instead of going through prepare_surface, and cad_tessellate refuses outright without it -
        # rightly, since the conversion to metres would otherwise be a guess. Omitting it killed
        # every internal-flow job in 8 seconds, before the mesher was ever reached: the external
        # path gets the same state from _plan_surface, so take it from there rather than
        # reconstructing a second opinion about the scale.
        _prepared = _plan_surface(state, workspace).consumed
        t = await _asyncio.to_thread(R.tessellate_internal, source_path,
                                     workspace / "_internal_stls", prepared=_prepared)
        prep = R.prepare_surface_internal(workspace, surfaces_src=t["stls"])
    except (_fence.StaleWorkerFenced, StaleExecutionPublish):
        raise
    except Exception:  # noqa: BLE001
        logger.exception("internal build: tessellation/prep failed - job_id=%s", job_id)
        await publish.aerror("The fluid volume could not be separated from the solid - the "
                      "geometry has no clean inlet/outlet openings to close off",
                op_id="internal:volume-unseparable")
        return False

    # bore diameter from the inlet port area - the resolution yardstick (D_h), no per-part constant
    _inlet_area = float(t["openings"]["inlet"]["area"]) or 1e-9
    bore_D = 2.0 * _math.sqrt(_inlet_area / _math.pi)
    # WHICH opening became the inlet is a GUESS - cad_tessellate takes the largest planar opening,
    # and that is wrong for every diffusing or combining part, where the feed is not the widest
    # port. The geometry alone often cannot settle it: a wye is a wye whether flow splits or joins.
    # Until the brief's declared roles are bound to the ports, say the assumption out loud with the
    # evidence behind it, so a reversed inlet is something the user can see rather than discover in
    # a solver run.
    _ports = t["openings"]
    _detail = "; ".join(
        f"{_nm} at ({', '.join(f'{v:.3f}' for v in _info['centroid'])}) m, "
        f"{float(_info['area']) * 1e6:.0f} mm²"
        for _nm, _info in _ports.items())
    _caveat = ("" if len(_ports) <= 2 else
               " - the inlet was taken as the LARGEST opening; on a diffuser or a combiner that is "
               "the wrong end, so check it before running")
    await publish.anote(f"Fluid volume identified - {len(_ports)} openings separated from the wall: "
                 f"{_detail}. Bore Ø{bore_D * 1000:.1f} mm{_caveat}",
            op_id="internal:volume-identified")

    plan = initial_plan
    feedback = (state.get("classifier_result", {}) or {}).get("summary", "") \
        or state.get("reviewer_feedback", "")
    previous_plan = initial_plan
    if previous_plan is None and (workspace / ".last_plan.json").exists():
        try:
            previous_plan = _json.loads((workspace / ".last_plan.json").read_text())
        except Exception:  # noqa: BLE001
            previous_plan = None
    max_attempts = int(scfg.MAX_SNAPPY_ATTEMPTS)
    _cap = 2400
    last_valid = False

    for attempt in range(1, max_attempts + 1):
        if plan is None:
            _po = await plan_with_accounting(
                surface=_plan_surface(state, workspace),
                workspace=workspace, job_id=job_id,
                request_txt=state.get("request_txt", ""),
                mesh_fidelity=state.get("effective_mesh_fidelity", ""),
                prior_feedback=feedback, previous_plan=previous_plan,
                flow_regime="internal",
                publish=publish, attempt=_attempt_of(state))
            plan = _po.plan
            run.note_plan_round(_po.round)
        strategy = plan or {}
        previous_plan = strategy
        await run.fence("write plan memory")
        _mem = await _op_begin(publish, "author_configuration", run, attempt)
        _write_plan_memory(workspace, strategy)

        # strategy → concrete numbers (internal knobs; tolerant of external-style keys)
        _budget = clamp_cell_budget(strategy.get("max_cells"), ceiling=polcfg.CELL_HARD_LIMIT)
        _sl = strategy.get("surface_level", 2)
        surface_level = int(_sl[-1] if isinstance(_sl, (list, tuple)) else _sl)
        cells_across = max(8, int(strategy.get("cells_across_diameter", 24)))
        n_layers = max(0, int(strategy.get("n_layers", 3)))
        first_rel = float(strategy.get("first_layer_rel", 0.3))
        quality = strategy.get("quality", "balanced")
        feature_level = int(strategy.get("feature_level", surface_level + 1))
        # base cell sized so the wall cell (base / 2^level) resolves the bore into `cells_across`
        base_cell = (bore_D / cells_across) * (2 ** surface_level)

        await publish.anote(f"Meshing pass {attempt} of {max_attempts} - "
                     f"{str(strategy.get('approach', 'default strategy'))[:80]}",
                op_id=f"internal:pass-open:{attempt}")
        try:
            await _op_end(publish, _mem, "author_configuration", {"stage": "plan"}, True)
            await run.fence("author mesh specification")
            _spec = await _op_begin(publish, "validate_configuration", run)
            summary = R.render_internal_case(
                workspace, names=prep["names"], features=prep["features"],
                interior_point=t["interior_point"], bbox_min=t["bbox_min"],
                bbox_max=t["bbox_max"], base_cell=base_cell, surface_level=surface_level,
                feature_level=feature_level, n_layers=n_layers, first_layer_rel=first_rel,
                max_cells=_budget, quality=quality)
            await publish.anote(f"Filling the cavity - about {cells_across} cells across the bore, "
                         f"refinement level {summary['surface_level']}, {n_layers} "
                         f"boundary layers",
                    op_id=f"internal:filling:{attempt}")
            run.note_authoring()
            result = await _run_snappy_timed(
                R, workspace, _cap, publish, state.get("engine", "snappy"),
                read_purpose(workspace), run=run, native_attempt=attempt)
            q = R.check_mesh(workspace)
            await publish.ameshed(q.get("cells"))
            fc = R._patch_face_counts(workspace)
            wall_faces = int(fc.get(prep["names"]["wall"], 0))
            production, reason = _judge_snappy(result, q, wall_faces)
            last_valid = bool(result.get("rc") == 0 and wall_faces > 0 and not q.get("fatal"))
            run.note_native_run(produced_usable_mesh=last_valid)
        except (_fence.StaleWorkerFenced, StaleExecutionPublish):
            raise                      # supersession stops the invocation; see the external build
        except Exception as exc:  # noqa: BLE001
            logger.exception("internal attempt %d errored - job_id=%s", attempt, job_id)
            production, reason, q, wall_faces = False, f"attempt errored: {type(exc).__name__}", {}, 0
            last_valid = False

        logger.info("internal attempt %d - cells=%s wall_faces=%s skew_faces=%s -> %s (job_id=%s)",
                    attempt, q.get("cells"), wall_faces, q.get("skew_faces"),
                    "PRODUCTION-GRADE" if production else reason[:50], job_id)
        _shape = _pass_shape(q, wall_faces)
        if production:
            await publish.anote(f"Pass {attempt} produced a production-grade mesh - {_shape}",
                    op_id=f"internal:pass-outcome:{attempt}")
        else:
            await publish.anote(f"Pass {attempt} fell short - {_shape}; re-planning ({reason[:60]})",
                    op_id=f"internal:pass-outcome:{attempt}")
        if production:
            return True
        feedback, plan = reason, None

    logger.warning("internal build exhausted %d attempts - job_id=%s (last_valid=%s)",
                   max_attempts, job_id, last_valid)
    if last_valid:
        await publish.anote("Meshing passes exhausted - submitting the best mesh built "
                     "(valid, but not as clean as we aim for)",
                op_id="internal:passes-exhausted")
    return last_valid

# builder-tool hooks (spec._load_run_enricher / _load_build_driver)

def _snappy_guidance(ok: bool, fatal: list, res: dict, q: dict, wall_faces: int) -> str:
    if res["rc"] != 0:
        return ("snappyHexMesh failed - see log_tail. Common causes: locationInMesh not in "
                "the fluid, a missing eMesh feature file, or a malformed dict block.")
    if wall_faces == 0:
        return ("CARVE LEAKED: no wall-patch faces - the body was not captured. locationInMesh "
                "is not in the fluid region, or the surface refinement is too coarse to SEAL the "
                "body. Fix the point / raise the surface level, then run_mesh again.")
    if fatal:
        return f"Fatal defects {fatal}: retighten meshQualityControls or add local refinement, then run_mesh."
    cov = res.get("layer_coverage")
    skew = q.get("max_skewness")
    skew_frac = q.get("skew_fraction", 0.0) or 0.0
    n_skew = q.get("skew_faces", 0) or 0
    cov_s = f"{cov:.0f}%" if cov is not None else "n/a"
    msgs = []
    if cov is not None and cov < 70:
        msgs.append(f"layer coverage only {cov:.0f}% - raise nRelaxedIter, loosen the relaxed{{}} "
                    "quality block, or reduce nSurfaceLayers / thin the first layer at sharp regions "
                    "to keep layers. Keep layers PRESENT.")
    # Skewness judged by LOCALIZATION, not the single worst value. A few skewed faces at the
    # wing-body junction (an inherent limit of octree-hex+prism meshers - a structured C/H-block is
    # the only real fix, which snappy can't build) is PRODUCTION-GRADE and fully solvable with
    # skewness-corrected schemes. Only WIDESPREAD skew warrants a re-mesh; chasing checkMesh's
    # `max < 4` forces endless futile re-meshing of a mesh that is already good.
    if skew is not None and skew_frac > 5e-4:
        msgs.append(f"skewness is WIDESPREAD: {n_skew:,} faces ({skew_frac * 100:.3f}%) exceed the "
                    f"threshold (worst {skew:.1f}) - not just the junction. Add local refinement or "
                    "tighten the quality block, then run_mesh.")
    if not msgs:
        loc = (f"skew localized to {n_skew} junction face(s) ({skew_frac * 100:.4f}% of faces, worst "
               f"{skew:.1f}) - normal + solvable for external aero" if n_skew else "no skew flags")
        return (f"Body-fitted, layers {cov_s}, {loc}. This is PRODUCTION-GRADE - call submit_mesh now.")
    return " ".join(msgs)


def run_enricher(R, workspace, res: dict, q: dict, out: dict) -> None:
    fatal = out.get("fatal_defects", [])
    ok = bool(out.get("success"))
    # body-fitted engine - surface the production evidence cfMesh can't give.
    # Reach the shared patch-count helper THROUGH the engine seam (the adapter
    # forwards it) so builder_tools stays engine-agnostic.
    fc = R._patch_face_counts(workspace)
    wall_faces = sum(c for p, c in fc.items() if p != "farfield")
    out["layer_coverage_pct"] = res.get("layer_coverage")
    out["per_patch_layers"] = res.get("per_patch_layers")
    out["wall_faces"] = wall_faces
    # Report a PRODUCTION-GRADE signal, NOT checkMesh's raw mesh_ok=False - which a handful of
    # localized junction skewed faces trip on every hex+prism external-aero mesh. The model was
    # re-meshing a good mesh because it read the raw mesh_ok=False (+ a high single max_skewness)
    # as failure despite the guidance saying submit. The bar comes from the per-engine criteria
    # registry (engines.quality_criteria - same thresholds the judge and manifest use), so the
    # signal and the guidance agree and the verdict is user-citable.
    from meshpipeline.engines.quality_criteria import production_grade
    _pg, _ = production_grade("snappy", {
        "fatal": fatal, "wall_faces": wall_faces,
        "skew_fraction": q.get("skew_fraction", 0.0) or 0.0,
    })
    out["mesh_ok"] = bool(_pg)
    out["skew_faces"] = q.get("skew_faces", 0)
    out["skew_pct"] = round((q.get("skew_fraction", 0.0) or 0.0) * 100, 4)
    ok = ok and wall_faces > 0
    out["success"] = ok
    # Mark a production-grade mesh so configure_mesh can steer the model to SUBMIT it - GLM
    # tends to keep re-configuring the strategy after a good mesh instead of committing.
    if out["mesh_ok"] and ok:
        (workspace / ".mesh_ok").write_text("production-grade")
    out["guidance"] = _snappy_guidance(ok, fatal, res, q, wall_faces)


async def drive(workspace, state, *, job_id: str, publish: ExecutionEventPublisher,
                run: BuilderDriverRun,
                source_path: str = ""):
    from meshpipeline.engines.snappy.planner import plan_with_accounting
    plan = None
    if state.get("builder_mode", "initial") in ("initial", "rebuild"):
        prior_fb = (state.get("classifier_result", {}) or {}).get("summary", "") \
            or state.get("reviewer_feedback", "")
        _po = await plan_with_accounting(
            surface=_plan_surface(state, workspace),
            workspace=workspace, job_id=job_id,
            request_txt=state.get("request_txt", ""),
            mesh_fidelity=state.get("effective_mesh_fidelity", ""), prior_feedback=prior_fb,
            flow_regime="internal" if state.get("flow_topology") == "internal" else "external",
            # the first plan is a real model round too, and omitting the publisher traces
            # nothing: the plan still succeeds, so the only symptom is a silent card
            publish=publish, attempt=_attempt_of(state))
        plan = _po.plan
        run.note_plan_round(_po.round)
    if state.get("flow_topology") == "internal":
        ok = await _build_internal_deterministic(
            workspace, state, job_id=job_id, publish=publish,
            source_path=source_path, initial_plan=plan, run=run)
        note = "internal build exhausted attempts - delivering best-effort mesh"
    else:
        ok = await _build_snappy_deterministic(
            workspace, state, job_id=job_id, publish=publish, initial_plan=plan, run=run)
        note = "snappy build exhausted attempts - delivering best-effort mesh"
    # The LAST fence: a superseded worker must not return Builder success, however much work it
    # completed. Past this point the outcome is this generation's to deliver.
    await run.fence("deliver builder outcome")
    outcome = run.outcome(produced_deliverable=ok, exhausted=True, failure_marker=note)
    return outcome.produced_deliverable, (outcome.terminal_value or note), outcome


def _attempt_of(state) -> int:
    raw = (state or {}).get("current_attempt")
    try:
        return int(raw) if isinstance(raw, (int, str)) else 1
    except (TypeError, ValueError):
        return 1

async def _op_begin(publish: ExecutionEventPublisher, op: str, run,
                    native_attempt: int = 1) -> str:
    if publish is None:
        return ""
    try:
        # The MESHING PASS is part of the identity. Every pass authors its own configuration and
        # runs its own mesher, so without it three separate operations arrive under one event id
        # and a reader keyed by that id sees one operation changing its mind.
        cid = (f"op:{getattr(run, 'job_id', '') or 'job'}:{getattr(run, 'attempt', 0)}"
               f":{int(native_attempt)}:{op}")
        await publish.atool_call(cid, op, None, "started", op_id=cid)
        return cid
    except StaleExecutionPublish:
        raise
    except Exception:      # observability never costs a mesh
        return ""


async def _op_end(publish: ExecutionEventPublisher, cid: str, op: str,
                  result: dict | None, ok: bool) -> None:
    if not cid or publish is None:
        return
    try:
        await publish.atool_result(f"{cid}:r", cid, op, result,
                                   "success" if ok else "failure", op_id=f"{cid}:r")
    except StaleExecutionPublish:
        raise
    except Exception:
        pass
