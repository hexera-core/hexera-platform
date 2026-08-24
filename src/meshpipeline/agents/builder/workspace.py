# Responsibility: Resolve and prepare the directories a builder attempt works in.
# Boundaries: layout only; it decides nothing about content.
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.pipeline.enums import Dimensionality

logger = logging.getLogger(__name__)


def _write_workspace_context_files(
    workspace: Path,
    source_path: str,
    request_txt: str = "",
    review_brief_txt: str = "",
    intake_patches: list | None = None,
    dimensionality: str = "",
    engine_params: dict | None = None,
    flow_topology: str = "",
    purpose: str = "",
) -> tuple[str, str]:
    if not request_txt:
        step_basename = os.path.basename(source_path) if source_path else "input.stl"
        request_txt = (
            f"Geometry: {step_basename}\n"
            f"Objective: Generate a simulation-ready mesh for the uploaded geometry."
        )

    try:
        (workspace / "request.txt").write_text(request_txt, encoding="utf-8")
        if review_brief_txt:
            (workspace / "review_brief.txt").write_text(review_brief_txt, encoding="utf-8")
        if intake_patches:
            _lines: list[str] = [
                "PATCH CONTRACT (USER-CONFIRMED AT INTAKE - ENFORCED BY THE PIPELINE)",
                "",
                "The mesh you produce must declare EXACTLY these patches by name AND",
                "by type. Deviations are detected automatically and bounce the mesh",
                "back to you for a rebuild. Do not invent new patches, do not split",
                "or merge them, do not rename them, do not change their types.",
                "",
                "Patches:",
            ]
            for _p in intake_patches:
                _n = (_p.get("name") or "").strip()
                _t = (_p.get("type") or "").strip()
                if _n and _t:
                    _lines.append(f"  • {_n}  →  type {_t}")
            if dimensionality:
                _lines += [
                    "",
                    f"Dimensionality: {dimensionality}",
                ]
                if dimensionality == Dimensionality.TWO_D:
                    _lines += [
                        "  • True 2D - the front/back faces of the thin spanwise slab MUST",
                        "    be a single physical group whose entry above has type=empty.",
                        "    Do not list them as symmetry. Do not produce two separate front",
                        "    and back patches.",
                    ]
                elif dimensionality == Dimensionality.THREE_D:
                    _lines += [
                        "  • Fully 3D - no spanwise reduction. Do not introduce 'empty' patches.",
                    ]
            (workspace / "patches_contract.txt").write_text("\n".join(_lines), encoding="utf-8")
            # The text contract above keeps only name/type - the DECLARATION (sizes, locations,
            # interchangeability) must survive for workspace-only runners (cfMesh's internal
            # path binds from it), so it travels verbatim as JSON beside the contract.
            (workspace / "port_declaration.json").write_text(
                json.dumps(intake_patches), encoding="utf-8")
        # The user's DECLARED engine params, verbatim (engine-native knobs ONLY). Same class
        # as the patch contract: a user declaration the engine must obey, not something to
        # re-derive.
        if engine_params:
            (workspace / "engine_params.json").write_text(
                json.dumps(engine_params, indent=2), encoding="utf-8")
        # The flow REGIME (internal/external) is a DERIVED FACT of the purpose, not an engine
        # param - it travels on its own neutral file so a workspace-only runner (cfMesh's
        # configure_mesh) reads it without inferring internal-vs-external from patch names,
        # and engine_params.json stays engine-native.
        if flow_topology:
            (workspace / "flow_topology").write_text(flow_topology, encoding="utf-8")
        # Dimensionality is likewise a USER-DECLARED intake fact (2D/3D) - same
        # neutral-file treatment, so a runner (cfMesh's cartesian2DMesh branch) reads the
        # declaration instead of trusting the model to pass it along.
        if dimensionality:
            (workspace / "dimensionality").write_text(str(dimensionality), encoding="utf-8")
        # The user-declared PURPOSE (external_cfd / internal_cfd / structural …). Written
        # as a neutral fact so run_mesh can key the DURATION HISTORY on engine+purpose -
        # a full-aircraft external run and a pipe internal run are not the same population.
        if purpose:
            (workspace / "purpose").write_text(str(purpose), encoding="utf-8")
    except Exception as exc:
        logger.warning("Builder: failed to write workspace context files - %s", exc)

    return request_txt, review_brief_txt


# The READ side of these files lives in engines.workspace_facts (the neutral layer,
# so engine bundles never import meshpipeline.agents.*); re-exported here for this package's callers.
from meshpipeline.engines.workspace_facts import (  # noqa: F401
    read_dimensionality,
    read_engine_params,
    read_flow_topology,
)


def _setup_workspace(job_id: str, attempt_num: int, engine: str = "",
                     generation: int = 0) -> Path:
    workspace = rtcfg.WORKSPACE_BASE / job_id / f"generation_{int(generation)}" / f"attempt_{attempt_num}"
    workspace.mkdir(parents=True, exist_ok=True)
    from meshpipeline.engines.registry import get_spec
    get_spec(engine).scaffold_workspace(workspace)
    return workspace

