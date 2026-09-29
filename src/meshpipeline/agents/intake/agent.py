# Responsibility: Run the intake conversation and produce a typed, approved job.
# Owns: system-prompt composition, the conversation turn, and the node's state patch.
# Boundaries: it establishes intent; it never meshes, and it never proceeds past an unconfirmed geometry scale.
# Collaborates with: agents/intake/approval.py, engine_selection.py and unit_clarification.py.

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import meshpipeline.settings.policy as polcfg
from meshpipeline.agents.intake import engine_selection as es
from meshpipeline.agents.intake import refusal, turn
from meshpipeline.agents.intake import settings as icfg
from meshpipeline.agents.intake import vocabulary as _vocab
from meshpipeline.agents.intake.executor import (
    IntakeExecutionState,
    IntakeToolExecutor,
)
from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy
from meshpipeline.agents.intake.validation import (
    _ALL_BOUNDARY_ROLES,
    _DIMENSIONALITY_VALUES,
    _MESH_FIDELITY_INPUT_VALUES,
    normalise_submission,  # noqa: F401
)

# The submit-validation if-forest lives in its own module; the tool SCHEMA below
# reuses its role/dimensionality vocab so the contract and the checks never drift.
from meshpipeline.contracts import model_inference as llm_router

logger = logging.getLogger(__name__)

from meshpipeline.agents.loop.diagnostics import sanitized as _sanitize_run_record  # noqa: E402
from meshpipeline.agents.loop.runner import run_agent_loop  # noqa: E402
from meshpipeline.agents.loop.tracing import TraceContext  # noqa: E402
from meshpipeline.contracts.agent_loop import (
    LoopExit as _LoopExit,
)
from meshpipeline.contracts.agent_loop import (
    LoopLimits as _LoopLimits,
)
from meshpipeline.trace.sink import PublicTraceSink  # noqa: E402


def _implemented_engine_names() -> list[str]:
    from meshpipeline.engines.registry import engine_names
    return engine_names()


_IMPLEMENTED_ENGINES = _implemented_engine_names()

# WHAT THE MODEL IS OFFERED. Display names only - the internal keys stay out of every enum,
# description and error message intake can see. `vocabulary.normalize_tool_args` converts back at
# the executor boundary, so validation and everything downstream still route on the keys.
_PURPOSE_CHOICES = list(_vocab.purpose_choices())
_INPUT_KIND_CHOICES = list(_vocab.input_kind_choices())
_ENGINE_CHOICES = list(_vocab.engine_choices())

# PipelineState is the inter-node contract (defined in graph.py). Imported under
# TYPE_CHECKING only - annotations are strings here, so this adds typing/IDE support
# without a runtime import (which would cycle: graph imports these node modules).
if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState


INTAKE_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for reference examples and typical parameters for a "
                "specific geometry type and simulation domain. A search sub-agent "
                "retrieves and distills the results into a short answer. Use only "
                "after the user has established the simulation type (e.g. CFD, FEA, "
                "thermal) to look up representative configurations and sensible defaults."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A specific question about the geometry type and simulation "
                            "scenario. E.g. 'typical domain sizing for rocket external "
                            "aerodynamics', 'centrifugal pump internal flow CFD setup', "
                            "'knee implant structural FEA mesh parameters'."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_compatible_engines",
            "description": (
                "Compare the REGISTERED engines for the user's declared setup and get the admission "
                "catalog's own verdict for each. Call this ONLY when the user's latest message "
                "explicitly asked which engines are compatible, for a recommendation, or for "
                "alternatives. It is READ-ONLY: it selects no engine, writes nothing, and authorizes "
                "nothing. Report only the engines and reasons it returns - never add an engine, "
                "never invent a compatibility reason, never rank by your own knowledge. An "
                "incompatible candidate is just one row of the comparison, not a problem to solve. "
                "Even if exactly ONE engine is compatible it is NOT selected: say which are "
                "compatible, ask which the user wants, and STOP - in this turn you may not propose a "
                "selection, check admission, submit, or dispatch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string", "enum": _PURPOSE_CHOICES,
                                "description": "The declared use case."},
                    "input_kind": {"type": "string", "enum": _INPUT_KIND_CHOICES,
                                   "description": "What the geometry represents."},
                    "dimensionality": {"type": "string", "enum": _DIMENSIONALITY_VALUES,
                                       "description": "2D or 3D, if the user declared it."},
                    "patches": {
                        "type": "array",
                        "description": "The boundary patches the user declared, EXACTLY as stated - "
                                       "compatibility can depend on patch count and roles.",
                        "items": {"type": "object", "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": _ALL_BOUNDARY_ROLES}},
                            "required": ["name", "type"]},
                    },
                    "engine_params": {"type": "object",
                                      "description": "Declared engine params, if any were given."},
                },
                "required": ["purpose", "input_kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_engine_selection",
            "description": (
                "Record that the user NAMED or CHANGED the engine they want, in their own latest "
                "message (e.g. 'use Gmsh', 'switch to cfMesh'). This is a PROPOSAL, not a selection: "
                "the application then shows its own deterministic 'Selected engine: X' statement and "
                "asks the user to confirm, and the turn ENDS there - you will not be asked to compose "
                "that reply, so do not try to. NEVER call this because an engine was recommended, "
                "because it is the only compatible one, because it looks best to you, or because you "
                "think the user meant it. Only their explicit naming counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "engine": {"type": "string", "enum": _ENGINE_CHOICES,
                               "description": "The engine the user named in their own words."},
                    "user_named_verbatim": {
                        "type": "string",
                        "description": (
                            "The user's OWN words naming this engine, quoted exactly from their "
                            "latest message (e.g. 'use snappyHexMesh'). Supply this whenever they "
                            "named it themselves - the application then selects it directly instead "
                            "of asking them to confirm what they just said. Leave it out if they "
                            "did not name the engine; never invent or paraphrase it."),
                    },
                },
                "required": ["engine"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_engine_selection",
            "description": (
                "Record the user's confirmation of the proposed engine, given after they saw the "
                "application's 'Selected engine: X' question. A plain 'yes', 'ok', 'sure' or 'go "
                "with that' answers that question and confirms X - the application reads it "
                "itself, so this call is then simply accepted. Otherwise quote their words "
                "exactly: the application checks the quote against their actual message and "
                "refuses words they did not write. If they answered with a different engine, call "
                "propose_engine_selection for that engine instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_agreed_verbatim": {
                        "type": "string",
                        "description": ("The user's exact words confirming the engine. If you cannot "
                                        "quote them from their latest message, they did not confirm."),
                    },
                },
                "required": ["user_agreed_verbatim"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preview_selected_admission",
            "description": (
                "DETERMINISTICALLY check whether the user's CONFIRMED engine can mesh their DECLARED "
                "setup, decided by the engine admission catalog - NOT your own judgement. Requires an "
                "engine the user has explicitly selected AND confirmed; an engine that was merely "
                "recommended, proposed, or assumed will be refused. Pass EVERY value the user has "
                "declared (purpose, input_kind, and also dimensionality, patches and engine_params "
                "when known) - declaring patches is essential because some impossibilities depend on "
                "patch count/roles. You MUST call this before telling the user their setup is "
                "impossible, and before submit_requirements, which accepts ONLY the token this "
                "returns for the EXACT payload previewed. If the verdict is 'impossible' the result "
                "says why and what would pass, and NOTHING was recorded: a value that was your own "
                "(a patch the user never named, a role you assigned) you repair and check again in "
                "the same turn; a value the USER declared you keep - put the finding to them with "
                "the one revision that would pass, and ask. Never add an engine suggestion. NEVER "
                "silently drop, merge, rename or re-role a patch the user declared to make it fit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_engine": {"type": "string", "enum": _ENGINE_CHOICES,
                                        "description": "The engine the user confirmed."},
                    "purpose": {"type": "string", "enum": _PURPOSE_CHOICES,
                                "description": "The declared use case."},
                    "input_kind": {"type": "string", "enum": _INPUT_KIND_CHOICES,
                                   "description": "What the geometry represents."},
                    "dimensionality": {"type": "string", "enum": _DIMENSIONALITY_VALUES,
                                       "description": "2D or 3D, if the user declared it."},
                    "patches": {
                        "type": "array",
                        "description": "The boundary patches the user declared, EXACTLY as stated - "
                                       "every separately named patch, none merged or dropped.",
                        "items": {"type": "object", "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": _ALL_BOUNDARY_ROLES}},
                            "required": ["name", "type"]},
                    },
                    "engine_params": {"type": "object",
                                      "description": "The chosen engine's declared params, if known."},
                },
                "required": ["selected_engine", "purpose", "input_kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_requirements",
            "description": (
                "Call this when all required information has been gathered and you are ready "
                "to submit the simulation requirements. This signals completion of the intake "
                "conversation and triggers mesh generation. "
                "All domain-specific parameters (flow conditions, load cases, material properties, "
                "etc.) must be captured inside request_txt - do not submit until the user has "
                "confirmed every parameter you intend to include."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "2-5 word plain-English label for the geometry and task, e.g. 'aircraft external aerodynamics', 'elbow internal flow'. DESCRIPTIVE only - it becomes the reviewer's task context and the run's corpus label; nothing routes on it.",
                    },
                    "request_txt": {
                        "type": "string",
                        "description": (
                            "Complete natural language summary of all user requirements. "
                            "Must include: geometry description, simulation type and purpose, "
                            "all domain-specific parameters confirmed by the user (e.g. flow "
                            "conditions, loads, material constraints), mesh density target, "
                            "domain sizing constraints, geometry problem areas, and patch/boundary "
                            "requirements. 4-10 sentences. "
                            "Every quantitative requirement you record MUST be unambiguous (admit a "
                            "single interpretation) and be worded IDENTICALLY here and in "
                            "review_brief_txt, so the builder and the reviewer cannot read it "
                            "differently. If the user did NOT specify a given parameter, do NOT "
                            "invent a value - state plainly that it is unspecified and left to the "
                            "builder's engineering discretion. (This applies per simulation type: "
                            "for external-flow it is most often the far-field domain extent - avoid "
                            "vague phrasings such as 'N body-lengths in all directions'; for "
                            "internal-flow, biomedical, or structural cases it applies to whatever "
                            "parameters the user left open.) "
                            "CRITICAL: treat 'standard', 'typical', 'normal', or 'at your discretion' "
                            "as UNSPECIFIED - do NOT substitute a textbook figure (e.g. '100 chord "
                            "lengths', '50 diameters', '20 body-lengths'). The builder uses its own "
                            "sound default; your job is only to record explicit user-given values, "
                            "not to invent acceptance thresholds the builder never agreed to. "
                            "If the user leaves a parameter unspecified but gave OTHER values that "
                            "bound or imply it, say so explicitly instead of inventing a number "
                            "(e.g. 'first-layer thickness unspecified - size it for y+ ~50 at the "
                            "stated 70 m/s and sea-level air') - the builder then computes it."
                        ),
                    },
                    "review_brief_txt": {
                        "type": "string",
                        "description": (
                            "Qualitative acceptance criteria for the mesh reviewer. "
                            "Must include: expected domain/region sizing, required patch structure, "
                            "near-wall or near-surface cell quality targets, surface integrity "
                            "requirements, transition smoothness, and any "
                            "case-specific quality criteria from the conversation. 5-10 sentences. "
                            "Do NOT include a cell-count target or budget - mesh size is gated by "
                            "the executor for compute feasibility, not judged by the reviewer. "
                            "Word every quantitative criterion IDENTICALLY to request_txt (same "
                            "single interpretation). Wherever the user left a parameter to the "
                            "builder's engineering discretion, instruct the reviewer to ACCEPT any "
                            "sound choice (sanity only - e.g. for external flow a far-field that "
                            "comfortably encloses the body and is not clipped) rather than fail for "
                            "a specific figure the user never required."
                        ),
                    },
                    "patches": {
                        "type": "array",
                        "description": (
                            "Structured list of boundary patches the user wants the mesh to expose, captured "
                            "verbatim from the conversation. The downstream pipeline enforces this list "
                            "exactly - patch names AND types must match what is produced. Use the names the "
                            "user actually said (e.g. 'airfoil', 'fuselage', 'inlet1') - do NOT translate or "
                            "normalise them. Each patch entry needs a name and a ROLE from the "
                            "chosen engine's vocabulary (validated mechanically against that engine)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Patch name exactly as the user named it (or as inferred from their description if they didn't name it explicitly).",
                                },
                                "type": {
                                    "type": "string",
                                    "enum": _ALL_BOUNDARY_ROLES,
                                    "description": (
                                        "Boundary ROLE in the declared PURPOSE's vocabulary (roles belong "
                                        "to the workflow, not to any engine) - flow purposes "
                                        "(external/internal CFD): 'wall' for solid surfaces, "
                                        "'inlet'/'outlet' for explicit flow boundaries, 'farfield' for a "
                                        "single combined freestream patch, 'symmetry' for symmetry planes, "
                                        "'empty' for the front/back faces of a true 2D case. Structural: "
                                        "'fixed' for constrained faces, 'load' where forces act, "
                                        "'contact', 'free'. Only the declared purpose's roles are accepted."
                                    ),
                                },
                            },
                            "required": ["name", "type"],
                            "additionalProperties": True,
                        },
                    },
                    "port_details_note": {
                        "type": "string",
                        "description": (
                            "INTERNAL FLOW ONLY, and only when the user stated them: each "
                            "inlet/outlet patch entry may also carry the user's OWN dimensions "
                            "and hints as extra fields on the patch object - 'diameter_mm' (a "
                            "circular bore diameter - the pipe's bore, NEVER the radial gap; "
                            "for an ANNULAR opening add 'inner_diameter_mm', the centre body's "
                            "diameter, and state both as given - never compute the ring's area "
                            "yourself), OR 'area_mm2', "
                            "OR 'width_mm' plus 'height_mm' (rectangular); 'near_mm' as [x, y, z] in the "
                            "geometry's millimetre coordinates when the user located the port; "
                            "'interchangeable_with' as a list of other port names ONLY when the "
                            "user explicitly confirmed those same-size ports carry no distinct "
                            "streams. Capture verbatim from the conversation - NEVER invent a "
                            "dimension, location or interchangeability the user did not state. "
                            "Set this field to 'captured' when any port carries details, else "
                            "omit it."
                        ),
                    },
                    "requested_extents": {
                        "type": "object",
                        "description": (
                            "EXTERNAL FLOW ONLY, and only when the user stated far-field "
                            "margins in body/reference lengths (e.g. '5 lengths upstream, 8 "
                            "downstream'): capture them VERBATIM as numbers per direction. "
                            "Never invent margins the user did not state."
                        ),
                        "properties": {
                            "upstream": {"type": "number"},
                            "downstream": {"type": "number"},
                            "lateral": {"type": "number"},
                            "vertical": {"type": "number"},
                        },
                    },
                    "flow_axis": {
                        "type": "string",
                        "enum": ["+x", "-x", "+y", "-y", "+z", "-z"],
                        "description": (
                            "EXTERNAL FLOW: the direction the flow travels, as the user stated "
                            "it (e.g. 'flow is along +Y' -> '+y'). REQUIRED whenever "
                            "requested_extents is captured - upstream/downstream are "
                            "meaningless without it. Never guess it from the geometry."
                        ),
                    },
                    "reference_length_m": {
                        "type": "number",
                        "description": (
                            "The reference length IN METRES the user's extents multiply (their "
                            "stated chord/body length - convert their unit to metres). Required "
                            "whenever requested_extents is given. Never invent it."
                        ),
                    },
                    "requirements_strict": {
                        "type": "boolean",
                        "description": (
                            "true ONLY if the user explicitly said requirements must be met "
                            "exactly (no near-miss deliveries). Default false: a measured "
                            "NEAR-miss on a stated requirement is delivered with the miss "
                            "stated plainly, rather than refused."
                        ),
                    },
                    "mesh_engine": {
                        "type": "string",
                        "enum": _ENGINE_CHOICES,
                        "description": (
                            "The engine that builds the mesh - a DIRECT USER INPUT, never "
                            "an inferred output. Either the user named their toolchain "
                            "when you asked (authoritative - do not second-guess it from "
                            "the geometry), or they were unsure and you proposed one from "
                            "the objective questions and THEY CONFIRMED it. There is no "
                            "'auto': every submission carries a user-confirmed engine."
                        ),
                    },
                    "engine_source": {
                        "type": "string",
                        "enum": ["user_direct", "suggested_confirmed"],
                        "description": (
                            "Provenance of mesh_engine: 'user_direct' = the user named "
                            "their engine/toolchain themselves; 'suggested_confirmed' = "
                            "you inferred a fit from the objective questions, proposed it "
                            "openly, and the user explicitly agreed."
                        ),
                    },
                    "dimensionality": {
                        "type": "string",
                        "enum": _DIMENSIONALITY_VALUES,
                        "description": (
                            "Geometric dimensionality of the simulation: "
                            "'2D' - true 2D problem (e.g. NACA airfoil cross-section, fully developed flow); "
                            "the mesh will be a single layer of cells with front/back faces marked 'empty' "
                            "and simpleFoam will skip the spanwise dimension. "
                            "'3D' - fully three-dimensional case (default; a thin slab meshed in "
                            "full 3D with symmetry patches is declared '3D', not 2D)."
                        ),
                    },
                    "purpose": {
                        "type": "string",
                        "enum": _PURPOSE_CHOICES,
                        "description": (
                            "The user's declared USE CASE for the mesh - what they will DO with it, "
                            "derived from the analysis type they described: 'structural' (FEA - stress, "
                            "modal), 'external_cfd' (flow AROUND a body in a far-field), 'internal_cfd' "
                            "(flow THROUGH a cavity/duct), 'conjugate_heat_transfer' (coupled fluid + "
                            "solid heat transfer - a multi-region CHT case: fluid plus one or more solids "
                            "meshed together with fluid-solid interfaces). This drives the boundary-role "
                            "vocabulary and the (engine × purpose × geometry) compatibility gate. A USER "
                            "declaration - never inferred from the geometry or filename."
                        ),
                    },
                    "input_kind": {
                        "type": "string",
                        "enum": _INPUT_KIND_CHOICES,
                        "description": (
                            "What the submitted geometry REPRESENTS (ask the user; never infer from the "
                            "filename): 'solid-body' - a CAD solid of the physical part (mesh its interior "
                            "for structural); 'fluid-domain' - a CAD solid of the FLUID region itself, e.g. "
                            "the wetted volume or a body already subtracted from a box (mesh it directly for "
                            "CFD); 'body-surface' - the body's surface, to wrap with a fluid domain or fill "
                            "as a cavity (the usual external/internal CFD input); 'solid-assembly' - a "
                            "multi-solid CAD assembly, one closed solid per region (fluid + each solid), for "
                            "a multi-region case such as conjugate heat transfer; 'planar-domain' - a FLAT "
                            "face/sheet body for 2D plane-stress/strain FEA (boundary conditions on its "
                            "edges). Which engines can consume "
                            "which kind is DECLARED per engine (capabilities) and enforced at submit - the "
                            "engine must be able to produce the purpose's mesh from THIS geometry kind."
                        ),
                    },
                    "engine_params": {
                        "type": "object",
                        "description": (
                            "The chosen engine's OWN declared parameters - keys and "
                            "allowed values come from that engine's spec (the native "
                            "questions listed for it in the ENGINE FIRST block). Many "
                            "engines declare none: pass {}. Do NOT put topology here - "
                            "internal-vs-external follows from the purpose and is derived "
                            "automatically. Validated MECHANICALLY against the submitted "
                            "mesh_engine: unknown keys, missing required params, or "
                            "out-of-enum values are rejected and you must re-ask. Answers "
                            "given for a previously-considered engine do NOT carry over - "
                            "re-confirm after any engine switch."
                        ),
                    },
                    "mesh_fidelity": {
                        "type": "string",
                        "enum": _MESH_FIDELITY_INPUT_VALUES,
                        "description": (
                            "OPTIONAL. The user's mesh DETAIL preference, if they expressed one: "
                            "'draft' (fast, coarse - geometry checks, boundary setup, early "
                            "iteration), 'standard' (the balanced default), or 'max' (the highest "
                            "bounded detail the engine supports). "
                            "DO NOT ASK for this - never spend a turn obtaining a tier, and never "
                            "hold up a submission for it. OMIT the field entirely when the user has "
                            "expressed no speed/detail preference; the system then applies Standard "
                            "as its own default and says so in the confirmation summary. "
                            "Record a tier ONLY when the user selects one or clearly states an "
                            "equivalent preference: 'make it quick'/'rough pass' -> draft; 'use the "
                            "standard tier' -> standard; 'high fidelity'/'high detail'/'high "
                            "quality'/'maximum detail'/'prioritize detail'/'production-quality "
                            "detail' -> max. There are exactly three tiers: draft, standard, max. "
                            "It is a QUALITATIVE preference, never a cell count: never ask the user "
                            "for target cells, maximum cells, element counts or refinement levels."
                        ),
                    },
                    "preview_token": {
                        "type": "string",
                        "description": (
                            "The token returned by a SUCCESSFUL single-engine preview_admission of "
                            "the EXACT payload you are submitting (same engine, purpose, input_kind, "
                            "dimensionality, patches, engine_params). Submission is IMPOSSIBLE "
                            "without it; a token for a different payload, from recommendation "
                            "exploration, or from before the user's latest message is rejected."
                        ),
                    },
                },
                "required": ["domain", "request_txt", "review_brief_txt", "patches", "dimensionality", "purpose", "input_kind", "mesh_engine", "engine_source", "engine_params", "preview_token"],
            },
        },
    },
]


def _confirmation_block(state: dict) -> str:
    _patches = ", ".join(
        f"{p.get('name')}({p.get('type')})" for p in (state.get("intake_patches") or [])
    ) or "(none)"
    return (
        "\n\n## CONFIRMATION TURN - the requirements below are ALREADY SUBMITTED\n"
        "You asked the user whether to proceed. Their latest message is the answer.\n\n"
        f"  engine:         {state.get('engine') or '(unset)'}\n"
        f"  engine_params:  {json.dumps(state.get('engine_params') or {})}\n"
        f"  purpose:        {state.get('purpose') or '(unset)'}\n"
        f"  input_kind:     {state.get('input_kind') or '(unset)'}\n"
        f"  dimensionality: {state.get('dimensionality') or '(unset)'}\n"
        f"  patches:        {_patches}\n"
        f"  request_txt:    {state.get('request_txt') or '(unset)'}\n\n"
        "Dispatch belongs to the APPLICATION alone, and THIS TURN DID NOT DISPATCH: a message\n"
        "the application reads as plain consent dispatches directly and never reaches you.\n"
        "Because you are running, any pending approval is dead - a fresh submit_requirements\n"
        "is REQUIRED before anything can dispatch. Decide from the WHOLE conversation:\n"
        "- They changed anything (a number, the engine, a patch, the domain size, the\n"
        "  dimensionality) → apply it, re-run preview_selected_admission on the FULL updated\n"
        "  payload, and call submit_requirements again. The application will show them a new\n"
        "  summary; you do not write it.\n"
        "- They changed the ENGINE → that is a new selection: call propose_engine_selection.\n"
        "- They refuse, hesitate, or ask a question → answer them.\n"
        "- It reads to you like pure agreement with no change → the application could not be\n"
        "  certain it was consent. Do not argue and do not announce progress: re-run\n"
        "  preview_selected_admission on the UNCHANGED payload and call submit_requirements\n"
        "  again, so the user gets a fresh summary to approve.\n"
        "- You are unsure what they meant → ask. Never guess.\n"
        "You cannot start the mesh yourself and must never claim it has started or will\n"
        "start.\n"
        "Do NOT re-ask for information already settled above."
    )


def _submit_fields() -> tuple[str, ...]:
    """submit_requirements' own argument names, less the single-use preview token - read off the
    tool definition, so the rerun context can never fall behind an argument the tool gains."""
    for tool in INTAKE_TOOLS:
        fn = tool.get("function") or {}
        if fn.get("name") == "submit_requirements":
            return tuple(k for k in fn["parameters"]["properties"] if k != "preview_token")
    return ()


#: where each submit_requirements argument lives on the session, for a run whose approval
#: record is gone. Only these survive there; the rest (engine_source, the typed far-field
#: request, strictness, ...) were only ever on the approval record.
_SESSION_COLUMN_OF = {
    "domain": "domain", "request_txt": "request_txt", "review_brief_txt": "review_brief_txt",
    "patches": "intake_patches", "dimensionality": "dimensionality", "purpose": "purpose",
    "input_kind": "input_kind", "mesh_engine": "engine", "engine_params": "engine_params",
    "mesh_fidelity": "requested_mesh_fidelity",
}


def _approved_last_time(state: Mapping[str, Any]) -> tuple[dict, bool]:
    """What the previous run was approved with, as submit_requirements arguments, and whether
    that is the approval record itself.

    The dispatched approval snapshot's payload IS the exact argument set the approval authority
    ran - request and review brief, engine provenance, the typed far-field request and its
    strictness included - so 'the same again' can be re-submitted from it without the model
    having to recreate anything. The session columns are only a fallback, and an incomplete one:
    request_txt is cleared from them at dispatch (approval.py disarms consent that way) and the
    typed values were never stored there."""
    fields = _submit_fields()
    approval = (state.get("intake_gate") or {}).get("approval") or {}
    if approval.get("job_id") and approval.get("payload"):
        payload = dict(approval.get("payload") or {})
        return {k: payload[k] for k in fields if k in payload}, True
    recorded = {}
    for arg, column in _SESSION_COLUMN_OF.items():
        value = state.get(column)
        if value not in (None, "", [], {}):
            recorded[arg] = value
    return recorded, False


def _previous_run_block(state: Mapping[str, Any]) -> str:
    """THE TURN AFTER A RUN HAS ENDED. This conversation already produced a run on this geometry
    and the user is back for another one - the same again, or with a change. The block tells the
    model how the last run ended and what it was approved with, so every question it asks can
    carry 'the same as last time' as the proposal. It records nothing: a new run dispatches only
    after a fresh submit_requirements and a fresh approval, exactly like the first."""
    prev = state.get("previous_run") or {}
    last, from_record = _approved_last_time(state)
    ended = str(prev.get("status") or "").strip()
    outcome = str(prev.get("outcome") or "").strip()
    outcome_lines = ("\n".join("  " + ln for ln in outcome.splitlines() if ln.strip())
                     if outcome else "  (no verdict text is on record for it)")
    if from_record:
        provenance = (
            "That run was approved with these submit_requirements arguments - the approval record\n"
            "itself, every argument it ran with except the single-use preview_token. An argument\n"
            "that is absent was not given, and stays absent for the same again:\n")
    else:
        missing = [k for k in _submit_fields() if k not in last]
        provenance = (
            "No approval record survives for that run, so these are the conversation's last\n"
            "recorded requirements, and they are INCOMPLETE - not on record: "
            f"{', '.join(missing) or '(nothing)'}.\n"
            "Establish each missing value that applies from the conversation before submitting;\n"
            "never invent one:\n")
    return (
        "\n\n## ANOTHER RUN ON THE SAME GEOMETRY - the previous run has ended\n"
        f"This conversation already approved a run (job {prev.get('job_id') or 'unknown'}), "
        f"and it ended: {ended or 'the record of it is gone'}. The user was told:\n"
        f"{outcome_lines}\n\n"
        f"{provenance}"
        f"{json.dumps(last, indent=2, ensure_ascii=False, default=str)}\n\n"
        "The uploaded geometry and its confirmed unit are unchanged; never ask about them again.\n"
        "NOTHING above is recorded for the new run - it is your proposal, and the user's latest\n"
        "message says what they want. Decide from it:\n"
        "- The same again ('run it again', 'same as last time', a bare yes) → re-run\n"
        "  preview_selected_admission on the payload above and call submit_requirements with\n"
        "  EVERY argument above exactly as it is, plus the new preview_token. The application\n"
        "  shows the summary and takes a fresh approval; you do not write the summary.\n"
        "- A change (a number, a patch, the fluid, the domain size, the mesh detail) → apply it\n"
        "  to the arguments above and keep every other one as it is (request_txt and\n"
        "  review_brief_txt must both state the change, worded identically), re-run\n"
        "  preview_selected_admission on the FULL updated payload, and call submit_requirements.\n"
        "- A different ENGINE → that is a new selection: call propose_engine_selection.\n"
        "- The verdict above blames the request itself → say in one line what it needs, propose\n"
        "  the specific change that addresses it, and ask whether to go with that.\n"
        "- You cannot tell what they want → ask ONE question whose proposal is the same settings\n"
        "  again.\n"
        "If a tool answers that the engine selection is no longer confirmed, propose it again\n"
        "and let the user confirm it in their next message.\n"
        "You cannot start the mesh yourself and must never claim it has started or will start.\n"
        "Do NOT re-ask for information already settled above."
    )


def _build_llm_messages(system: str, state_messages: list) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": system}]
    for m in state_messages:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": str(m.get("content", ""))})
    return messages


# intake system-prompt composition
# The system prompt = the static prompt file + DECLARED blocks. Every block is
# registered here with a name and its purpose - features must add blocks via
# this registry (tests/test_data_contract.py enforces it), never by ad-hoc
# `system += ...` appends whose intent gets lost.

def _block_quality_criteria() -> str:
    from meshpipeline.engines.quality_criteria import render_defaults_block
    return (
        "\n\nPRODUCTION-GRADE MESH CRITERIA (the pipeline's enforced, "
        "evidence-backed bars - if the user asks what counts as production grade, or "
        "disputes a delivered mesh's quality, explain the relevant criterion and cite "
        "its source link verbatim; do not invent thresholds or links):\n"
        + render_defaults_block()
    )


def _block_engine_first() -> str:
    from meshpipeline.engines.registry import ENGINE_CATALOG, catalog_menu, engine_label
    native_qs = "\n".join(
        f"  [{engine_label(sp.name)}] {sp.intake_guidance}"
        for sp in ENGINE_CATALOG.values() if sp.implemented and sp.intake_guidance)
    declared_params = "\n".join(
        f"  [{engine_label(sp.name)}] {p.key} ({' | '.join(p.values)}): {p.ask}"
        for sp in ENGINE_CATALOG.values() if sp.implemented
        for p in sp.intake_params)
    advisories = "\n".join(
        f"  [{engine_label(sp.name)}] {a}"
        for sp in ENGINE_CATALOG.values() if sp.implemented
        for a in sp.intake_advisories)
    return (
        "\n\nENGINE FIRST - QUESTION-ORDER POLICY (this is the single authority for how the engine "
        "is settled; it overrides any general ordering hint elsewhere in the prompt):\n"
        "  1. The engine is a DIRECT USER INPUT, never inferred and never inferred FROM (do not "
        "     read the engineering purpose off the engine name - a mesher's name does not imply a use case).\n"
        "  2. If the user has ALREADY named an engine (in their message or earlier in the chat), "
        "     that answer is AUTHORITATIVE: ACCEPT it (engine_source=user_direct) and do NOT ask "
        "     them to choose or confirm it again - never re-derive or second-guess it from the "
        "     geometry, and never silently change it.\n"
        "  3. If the engine is still MISSING, your FIRST substantive question is which engine/"
        "     toolchain the user already works in - asked BEFORE any engine-specific parameters. "
        "     Offer the implemented options with a one-line gist of each, plus 'not sure':\n"
        + catalog_menu() + "\n"
        "  4. Ask only ONE substantive question per turn; if a prerequisite (e.g. the purpose) is "
        "     needed to explain a compatibility conflict, ask the minimum necessary question.\n"
        "  5. Do NOT recommend a specific engine unless the user explicitly asks for a "
        "     recommendation.\n"
        "  6. WHAT 'REQUIRED' MEANS: only the fields submit_requirements marks required. A question "
        "     about anything else (what else might be inside the geometry, how a target will be met, "
        "     a preference) is a courtesy, never a reason to withhold a submission. When the user "
        "     tells you to proceed with standard defaults, or that everything needed is already "
        "     stated, every such courtesy question is ANSWERED: note the assumption in request_txt "
        "     and move on. And never ask again for a value the user has already given - exact port "
        "     coordinates ARE their locations.\n"
        "Once the engine is settled, ask your follow-up questions in THAT ENGINE'S native concepts "
        "- the way its users think - not through a generic abstraction:\n" + native_qs + "\n"
        "FALLBACK - only when the user is unsure or ambiguous: ask objective-"
        "level questions (external body in a far-field vs coupled multi-region "
        "vs image-derived internal passage), infer the best fit, and PROPOSE it "
        "openly (e.g. 'this sounds like external aero - I would suggest "
        "snappyHexMesh; proceed with that, or pick another?'). The inferred "
        "choice is ALWAYS shown and overridable, never silently applied: submit "
        "only after the user explicitly agrees (engine_source="
        "suggested_confirmed). If their choice looks ill-suited (judge against "
        "the catalog gists), say so ONCE with your reasoning and ask them to "
        "confirm (do NOT name a replacement engine unless they ask); if they confirm, their word "
        "is final - the pipeline honors it.\n"
        "Each engine DECLARES the parameters you must settle with the user "
        "before submitting (engine_params in submit_requirements - validated "
        "mechanically against the chosen engine; answers for an abandoned "
        "engine choice do not carry over):\n" + declared_params
        + "\n\nSOFT LIMITATIONS - a HEADS-UP, never a gate. Distinct from the hard "
        "impossibilities above (a mesher that physically cannot produce the purpose's "
        "mesh - those are REJECTED). These are things the chosen engine CAN hit but does "
        "not always hit; whether they bite depends on the specific geometry. When the "
        "user's engine + purpose + the geometry they describe fall into one of these - "
        "judge it yourself, using these declared tendencies AND your own meshing "
        "knowledge (web_search if unsure) - raise it ONCE, briefly, BEFORE submitting: "
        "say what may fall short and why, note it MIGHT be fine for their case, and that they can "
        "switch engines if they prefer - but recommend a SPECIFIC alternative engine only if they "
        "ask. Then let them decide - "
        "you do NOT block, downgrade their choice, or refuse to submit. Do NOT recite a "
        "limitation that is irrelevant to their geometry. The declared tendencies:\n"
        + advisories
    )


# (name, purpose, builder) - order is the order blocks appear in the prompt
def _block_geometry_check() -> str:
    return (
        "\n\nGEOMETRY CHECK - FACTS SETTLED ON A PICTURE, NOT IN CHAT:\n"
        "When an assistant message in the conversation begins 'GEOMETRY CHECK (confirmed by the "
        "user):', the application has already shown the user their part with every opening "
        "marked on a picture, and they confirmed it. Everything in that message is DECLARED by "
        "the user: input_kind, whether the fluid flows through the part or around it, each "
        "opening's name, role, size and position, and a point inside the flow. Do not ask about "
        "any of it again, and do not read it back for confirmation. Carry every opening into "
        "`patches` exactly as named, with its role, and put its diameter_mm (or width_mm and "
        "height_mm) and near_mm on the patch entry as the message states them "
        "(port_details_note = 'captured'). A sticker the message calls 'not an opening' is no "
        "patch at all. For a body the fluid flows AROUND, the message states the flow axis, the "
        "reference length in mm and the far-field margins in reference lengths: the user "
        "confirmed them on the picture, so they are user-stated - carry them into flow_axis, "
        "reference_length_m (mm / 1000) and requested_extents exactly as written. Ask only what "
        "the message does not contain - typically the purpose, the fluid and its speed, and the "
        "engine when it is not settled.\n"
        "The message also says which unit the file is in and how long the part is; its sizes are "
        "already converted to millimetres for that unit. A LATER CHANGE OF UNIT IS THE "
        "APPLICATION'S, NOT YOURS: when the user says the file is in a different unit, the "
        "application records it, re-reads every size in that message in the new unit (the message "
        "is re-worded where it stands), withdraws any run proposed with the old sizes, and tells "
        "the user so in a line beginning 'Noted: the file is in'. Take the sizes exactly as the "
        "message now states them - never relabel, multiply or divide them yourself, and never "
        "convert a number the user quoted from the old reading. The picture's openings, axes and "
        "flow direction stand - they do not depend on the unit. That is not a conflict, and never "
        "a reason to ask the flow axis, the unit, or anything else the check settled, again; "
        "re-propose with the new sizes when a run was withdrawn.\n"
        "An assistant message beginning 'GEOMETRY CHECK (drawing your part):' is a holding line "
        "while the picture is made: nothing in it is declared, and you never repeat it. A user "
        "message saying they confirmed the geometry check is your cue to continue with the next "
        "question, not to summarise.\n"
        "A part that STANDS ON THE GROUND: the ground is produced by the domain - the floor of "
        "the far-field box, laid under the part at its lowest z. Declare it as ONE patch named "
        "exactly `ground` with type wall, beside the body's own wall and the farfield (e.g. car "
        "wall, ground wall, farfield). It is never a region of the geometry, so never ask the "
        "user to name, split or supply it in their file, and never count it against the parts "
        "the file distinguishes - a one-region body on the ground is still one body wall. "
        "The name `ground` is kept for that floor: do not give it to anything else. A part the "
        "message calls free in the flow gets no ground patch."
    )


def _block_propose_first() -> str:
    return (
        "\n\nPROPOSE, THEN ASK - every question carries your best proposal:\n"
        "A user who has uploaded a part and said what it is for should be able to answer 'ok' or "
        "correct one value, not write everything out. So every question you ask comes with the "
        "answer you would give yourself, read from what is already known - the file, the geometry "
        "check, the stated purpose, earlier answers, the engine's own defaults - and ends by asking "
        "whether it is right. For example: 'Fluid and conditions? I would go with air at 15 C and "
        "sea-level pressure, 10 m/s, k-omega SST with wall functions at y+ 30 to 300 - ok, or tell "
        "me what differs.' A proposal is NOT a recorded value: record only what the user states or "
        "confirms; an 'ok' confirms the proposal exactly as you stated it, and you then carry those "
        "values as user-given. The courtesy follow-ups that usually share one answer - prism layers, "
        "patch names, refinement zones - go into ONE question with ONE proposal, not four turns. "
        "Two things are never proposed: the ENGINE (rule 5 above stands - offer the menu, do not "
        "recommend unless asked) and the file's UNIT (units are asked, never guessed).\n"
        "HOW A REPLY IS READ: 'ok', 'yes', 'fine', 'sure', 'sensible default', 'you decide', "
        "'whatever is standard' and 'I do not know' all ACCEPT the proposal exactly as you stated "
        "it - take those values and move on. Any other reply is still the user's ONE answer to "
        "that question: if it changes a value, take the change; if it is unclear or answers "
        "something else, take your own proposal, note it in request_txt as an assumption the user "
        "did not state, and move on. Never ask the same question twice, in any wording, and never "
        "re-ask what the user has already answered - the application watches for a repeated "
        "question and sends it back to you to move on. Near-wall treatment (the y+ band, the "
        "first-layer thickness, the layer count), patch names and refinement zones are courtesy "
        "questions: one question, one proposal, and never a reason to hold a submission. The same "
        "holds after an admission refusal: whatever you must ask the user carries your proposed "
        "revision, so 'ok' answers it.\n"
        "A VALUE THE USER LEAVES TO YOU is yours to choose. 'I do not know', 'use a sensible "
        "default' or 'you decide' answers even a question you had no proposal for - which of two "
        "openings is the second inlet, say: pick the sensible default (the first candidate you "
        "listed, unless the geometry says otherwise), say which in one line, record it as an "
        "assumption, and continue. Never reply that a value is required and cannot be defaulted: "
        "required means it must be in the submission, not that the user must type it. The two "
        "exceptions stand - the ENGINE is proposed and confirmed, never defaulted, and the UNIT is "
        "asked, never guessed."
    )


INTAKE_PROMPT_BLOCKS: tuple = (
    ("quality_criteria", "evidence-backed production-grade bars the intake can cite",
     _block_quality_criteria),
    ("engine_first", "engine as direct user input; native follow-ups; propose+confirm fallback",
     _block_engine_first),
    ("geometry_check", "facts the user confirmed on the geometry-check picture are declared; never re-asked",
     _block_geometry_check),
    ("propose_first", "every question carries a proposed answer read from what is known; 'ok' or 'I do not know' accepts it; one unclear reply and the model moves on; no question twice; engine and unit never proposed",
     _block_propose_first),
)


def compose_intake_system() -> str:
    system = polcfg.prompts.intake_system
    for _name, _purpose, build in INTAKE_PROMPT_BLOCKS:
        system += build()
    return system


def _execute_intake_tool(name: str, args: dict,
                         search_events: list | None = None) -> str:
    if name == "web_search":
        query = args.get("query", "")
        if not query:
            return "No query provided."
        import asyncio as _asyncio
        try:
            from meshpipeline.agent_tools.shared.web_search import web_search
            # no job exists during intake - the collector carries the search
            # record through the intake-events channel into events.jsonl
            result = _asyncio.run(web_search(query, collector=search_events))
            return result or "No relevant examples found."
        except Exception as exc:
            logger.warning("Intake: web_search tool failed: %s", exc)
            return f"web_search failed: {exc}"
    return f"Unknown tool: {name}"



def _extract_reply(result: dict) -> str:
    msgs = result.get("messages") or []
    assistant_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]
    if not assistant_msgs:
        logger.warning("_extract_reply: no assistant messages in intake result")
        return ""
    content = assistant_msgs[-1].get("content", "")
    if not content:
        logger.warning("_extract_reply: assistant message has empty content")
    return content



async def node_intake(state: PipelineState) -> dict:
    job_id = state.get("job_id", "unknown")

    # `awaiting_confirmation` is the CHAT turn after submit_requirements, where the user answers
    # "shall I proceed?". Intake must run then: its reply may agree, may agree AND change
    # something, may ask a question, may refuse. Only a model that reads the whole conversation can
    # tell those apart. (The pipeline GRAPH calls this node with request_txt already set and no
    # flag - it still short-circuits, as intake runs once per job.)
    _awaiting_confirmation = bool(state.get("awaiting_confirmation"))
    if state.get("request_txt") and not _awaiting_confirmation:
        logger.debug("Intake: request_txt already set - skipping - job_id=%s", job_id)
        return {}

    logger.info("Intake: starting - job_id=%s session_id=%s confirming=%s",
                job_id, state.get("session_id", ""), _awaiting_confirmation)

    system = compose_intake_system()
    if _awaiting_confirmation:
        system += _confirmation_block(state)
    elif state.get("previous_run"):
        # the conversation's run has ended and this turn starts another; once the new
        # requirements are submitted the confirmation block above takes over
        system += _previous_run_block(state)
    state_messages = state.get("messages", [])
    llm_messages = _build_llm_messages(system=system, state_messages=state_messages)

    _budget = turn.assess_budget(state_messages, awaiting_confirmation=_awaiting_confirmation)
    if _budget.exhausted:
        logger.warning("Intake: MAX_TURNS=%d reached - nudging toward closure (fail-safe, never "
                       "forcing a submit) - job_id=%s", turn.MAX_TURNS, job_id)
        llm_messages, state_messages = turn.apply_budget_nudge(llm_messages, state_messages)
    if turn.defers_to_default(turn.latest_user_text(state_messages)):
        # "I do not know, use a sensible default and continue" is an answer: the value is the
        # model's to choose. Said here, in code, before the model can ask the question again or
        # reply that the value "cannot be selected by default".
        logger.info("Intake: the user left the open question to the model - take the default "
                    "- job_id=%s", job_id)
        llm_messages, state_messages = turn.apply_default_nudge(llm_messages, state_messages)

    _ctx = turn.hydrate(state, state_messages)
    # A plain yes to the application's own engine question ("Do you want to select X?") is read
    # HERE, before the model runs. The question named the engine, so "yes", "ok" or "go with
    # that" binds to it and needs no quote; the model used to have to quote the user to confirm,
    # and a "yes" it paraphrased was refused as words they never wrote - then asked again.
    _assented = es.confirm_by_assent(
        _ctx.selection, session_id=_ctx.session_id, owner_id=_ctx.owner_id,
        revision=_ctx.revision, latest_user_message=_ctx.latest_user_msg,
        user_msg_count=_ctx.user_msg_count)
    if _assented is not None:
        logger.info("Intake: engine selection CONFIRMED by the user's plain yes engine=%s - "
                    "job_id=%s", _assented["engine"], job_id)
        _ctx = dataclasses.replace(_ctx, selection=_assented)
    # A JobPublisher only exists once a job does - and Intake runs BEFORE one. Rather than mint a
    # fake job id or instantiate a worker-owned publisher in the request path, the turn collects
    # the same typed events through a sink and returns them with the response.
    _trace_publisher = state.get("publish") or state.get("_publisher") or PublicTraceSink()

    _exec_state = IntakeExecutionState(
        session_id=_ctx.session_id, owner_id=_ctx.owner_id, revision=_ctx.revision,
        user_msg_count=_ctx.user_msg_count, latest_user_msg=_ctx.latest_user_msg,
        source_ref=_ctx.source_ref, rec_authorized=_ctx.rec_authorized,
        pending=_ctx.pending, selection=_ctx.selection, approval=_ctx.approval)
    _executor = IntakeToolExecutor(
        state=_exec_state, job_id=str(job_id), implemented_engines=_IMPLEMENTED_ENGINES,
        search_tool=_execute_intake_tool, trace=_trace_publisher)
    _policy = IntakeLoopPolicy(
        exec_state=_exec_state, executor=_executor,
        # INTAKE_MAX_ROUNDS is the ONLY Intake bound. No tool-call cap, no category cap, no
        # deadline, and no no-progress threshold: progress is counted and reported, never
        # enforced, because no measured Intake distribution justifies a value.
        limits_=_LoopLimits(max_rounds=icfg.INTAKE_MAX_ROUNDS),
        # What has already been asked, so a reply that asks it again is caught inside the loop.
        prior_questions=turn.prior_assistant_texts(state_messages))

    async def _intake_provider(*, messages, tools, job_id, user_id, tool_choice="auto"):
        # No on_reasoning here on purpose: intake's route is not streamed, so it has no reasoning
        # deltas to forward. Declaring the argument to ignore it would tell the round that this
        # provider streams, and the round only offers the sink to one that says it does.
        return await llm_router.call_intake_model(
            messages=messages, job_id=job_id, user_id=user_id, name="Intake", tools=tools)

    # THE CANONICAL INTAKE LOOP
    # One loop, shared with Builder and Reviewer. It owns rounds, tool-call accounting, malformed
    # representation, the round limit and the run record; everything about REQUIREMENTS and
    # AUTHORIZATION belongs to the Intake policy and executor.
    _loop_result = await run_agent_loop(
        driver=_policy, provider_call=_intake_provider, messages=llm_messages, tools=INTAKE_TOOLS,
        job_id=str(job_id), user_id=_ctx.owner_id,
        append_tool_result=lambda m, cid, c: m.append(
            {"role": "tool", "tool_call_id": cid, "content": c}),
        on_round=_policy.note_round,
        # PUBLIC TRACE. Intake runs in the API request path and has no job publisher of its own
        # until a job exists, so the trace is published only when one was handed in - the
        # conversation itself is already the user's account of this stage.
        trace=TraceContext(publisher=_trace_publisher, job_id=str(job_id),
                           role="intake", attempt=1),
        # Intake runs in the API request path, where the worker is the sole corpus writer. The
        # record is captured here and transported as its own `agent_run` event.
        record_sink=lambda _r: None)

    if _loop_result.exit is _LoopExit.provider_failed:
        return turn.provider_failure_patch(_loop_result.failure_marker,
                                           _sanitize_run_record(_loop_result.record))

    # A terminal action carries the APPLICATION's own rendered text; a completed turn carries the
    # model's conversational reply. Round exhaustion carries neither - it says nothing at all,
    # and a turn that says nothing is given one honest sentence below, after the refusal rewrite
    # has had its chance (a refusal is never silent, so the two cannot collide).
    assistant_text = str(_loop_result.payload or "")

    # An admission refusal the model did not repair ends with the model's OWN reply - it had the
    # finding as a tool result and wrote with it in hand. That reply is checked before it goes
    # out: one that names another engine, asks nothing, or does not exist (the loop ran out of
    # rounds) is replaced by the rendered finding. Not when the user asked for a comparison (the
    # engines named are the answer), and not when the turn ended on the application's own text.
    if (_exec_state.admission_refusal is not None and not _exec_state.recommended_this_turn
            and _loop_result.exit is not _LoopExit.terminal_action):
        _settled = refusal.settle(assistant_text, _exec_state.admission_refusal)
        assistant_text = _settled.text
        logger.info("Intake: refusal delivered %s - job_id=%s", _settled.source, job_id)
    _in_tokens, _out_tokens = _policy.input_tokens, _policy.output_tokens
    if not assistant_text.strip():
        # The loop ran out of rounds (or time, or progress) without a reply, or the model
        # returned empty content. A blank assistant message is not a turn: the user saw an empty
        # bubble and the next call handed the model an empty assistant turn as history.
        assistant_text = turn.unsettled_reply(_exec_state, _loop_result.exit)
    _had_usage = bool(_in_tokens or _out_tokens)
    _record = turn.TurnRecord(
        finish_reason=_policy.finish_reason or "unknown",
        usage=({"prompt_tokens": _in_tokens,
                "completion_tokens": _out_tokens,
                "total_tokens": _in_tokens + _out_tokens}
               if _had_usage else None),
        agent_run=_sanitize_run_record(_loop_result.record),
        search_events=_exec_state.search_events,
        public_trace=_public_trace_of(_trace_publisher),
        budget=_budget, assistant_text=assistant_text,
        transcript=turn.serialise_transcript(llm_messages, assistant_text),
        system_snapshot=system)
    # The gate sub-records the executor may have advanced during the loop.
    _ctx = dataclasses.replace(_ctx, selection=_exec_state.selection,
                               pending=_exec_state.pending, approval=_exec_state.approval)

    logger.info("Intake: response turn=%d finish_reason=%s chars=%d - job_id=%s",
                _budget.turn_number, _record.finish_reason, len(assistant_text), job_id)

    if _exec_state.submit_args:
        _requirements = normalise_submission(_exec_state.submit_args)
        logger.info(
            "Intake: submit_requirements complete - domain=%r engine=%r params=%r "
            "dimensionality=%s patches=%d request=%d chars review_brief=%d chars - job_id=%s",
            _requirements.domain, _requirements.mesh_engine, _requirements.engine_params,
            _requirements.dimensionality, len(_requirements.intake_patches),
            len(_requirements.request_txt), len(_requirements.review_brief_txt), job_id)
        return turn.completed_patch(_ctx, _record, _requirements)

    logger.info("Intake: asking question turn=%d - job_id=%s: %s",
                _budget.turn_number, job_id, assistant_text[:120])
    return turn.continuing_patch(_ctx, _record)


def _public_trace_of(publisher) -> list:
    drain = getattr(publisher, "session_events", None)
    if drain is None:
        return []
    try:
        return drain()
    except Exception:
        return []
