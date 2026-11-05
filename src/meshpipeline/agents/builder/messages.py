# Responsibility: Compose the builder's system and first-turn messages from the engine's declarations.
# Boundaries: assembly from the engine bundle's own prompt fragments; no engine vocabulary is written here.
from __future__ import annotations

import logging
from pathlib import Path

from meshpipeline.contracts.model_inference import Conversation

logger = logging.getLogger(__name__)






# The engine pack (system prompt + tool set) is spec-declared
# selection is now driven by the catalog + selector (pipeline.engine_select /
# (engines/<name>/pack.py); this module only composes messages.


def _build_engine_messages(
    source_path: str,
    workspace: Path,
    domain: str = "",
    intake_patches: list | None = None,
    dimensionality: str = "",
    engine: str = "cfmesh",
    engine_params: dict | None = None,
    purpose: str = "",
) -> Conversation:
    import meshpipeline.settings.policy as polcfg
    from meshpipeline.engines.registry import get_spec
    # THE BUILDER'S SYSTEM MESSAGE = the shared role contract + this engine's specialization.
    # The general half is a packaged prompt loaded through the same REQUIRED_PROMPTS authority
    # Intake and Reviewer use; the engine half is the bundle's own declared prompt. Order matters:
    # role first, then the engine it drives.
    _system_prompt = "\n\n".join(
        (polcfg.prompts.builder_system.strip(), get_spec(engine).system_prompt.strip()))
    system_content = (
        _system_prompt
        .replace("{CELL_CAP}", f"{polcfg.CELL_HARD_LIMIT:,}")
    )
    _briefing = get_spec(engine).briefing
    contract = ""
    if intake_patches:
        _bullets = "\n".join(
            f"  - {(_p.get('name') or '').strip()}  ->  type {(_p.get('type') or '').strip()}"
            for _p in intake_patches
            if (_p.get('name') or '').strip()
        )
        if _bullets:
            contract = (
                f"\n## {_briefing.contract_title}\n"
                f"{_bullets}\n"
                f"{_briefing.contract_guidance}\n"
            )
    # Every engine-specific fragment comes from the spec's DECLARED briefing
    # (the audit-proven seam) - this function never branches on the engine.
    _workflow = _briefing.workflow
    _geom_line = _briefing.geometry_line
    # The user-declared PURPOSE frames the job (structural / external CFD / internal
    # CFD) - the builder meshes for THIS workflow, not one inferred from the engine.
    _purpose_line = ""
    if purpose:
        from meshpipeline.engines.purposes import PURPOSES
        _pp = PURPOSES.get(purpose)
        if _pp:
            _purpose_line = f"Simulation workflow (user-declared): {_pp.label}."
    _params_line = ""
    if engine_params:
        import json as _json
        _params_line = ("## Declared engine parameters (user-confirmed at intake - HONOR them)\n"
                        + _json.dumps(engine_params))
    user_content = "\n".join([
        _geom_line,
        f"Workspace root: {workspace}",
        f"The user's requirements are in {workspace}/request.txt - READ THIS FIRST.",
        contract,
        _params_line,
        f"Domain: {domain or _briefing.domain_default}.",
        _purpose_line,
        _workflow,
    ])
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _build_initial_messages(
    source_path: str,
    workspace: Path,
    domain: str = "",
    intake_patches: list | None = None,
    dimensionality: str = "",
    engine: str = "cfmesh",
    engine_params: dict | None = None,
    purpose: str = "",
) -> Conversation:
    return _build_engine_messages(
        source_path, workspace, domain,
        intake_patches, dimensionality, engine, engine_params, purpose,
    )

