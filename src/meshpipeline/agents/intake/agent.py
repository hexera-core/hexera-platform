# Responsibility: Run the intake conversation and produce a typed, approved job.
# Owns: system-prompt composition, the conversation turn, and the node's state patch.
# Boundaries: it establishes intent; it never meshes, and it never proceeds past an unconfirmed geometry scale.
# Collaborates with: agents/intake/approval.py, engine_selection.py and unit_clarification.py.

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING

import meshpipeline.settings.policy as polcfg
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
                "the application APPENDS its own deterministic 'Selected engine: X' question to whatever "
                "you wrote this turn, so write your proposal and let that question land under it. Do "
                "not write your own version of it. NEVER call this because an engine was recommended, "
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
                "Record the user's EXPLICIT confirmation of the proposed engine, given after they "
                "saw the application's 'Selected engine: X' question. The application checks your "
                "quote against the user's actual message and refuses if it is not there, so quote "
                "them exactly. If they answered with a different engine, call "
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
                "returns for the EXACT payload previewed. If the verdict is 'impossible' the turn "
                "ENDS with the application's own message - do not compose or expand it, and never add "
                "an engine suggestion. NEVER silently drop, merge, rename or re-role a declared patch "
                "to make it fit."
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
                            "circular bore diameter), OR 'area_mm2', OR 'width_mm' plus "
                            "'height_mm' (rectangular); 'near_mm' as [x, y, z] in the "
                            "geometry's millimetre coordinates when the user located the port; "
                            "'interchangeable_with' as a list of other port names ONLY when the "
                            "user explicitly confirmed those same-size ports carry no distinct "
                            "streams; and 'opening_id' - the id of the measured opening this port "
                            "IS ('o1', 'o4', ...), which you know whenever you have named the port "
                            "against the measured table. GIVE opening_id WHENEVER YOU KNOW IT: it "
                            "binds the name to that exact mouth, so the size and position are taken "
                            "from the measurement and two same-size mouths never need telling "
                            "apart. Never invent an interchangeability the user did not state. "
                            "A MEASURED SIZE THE CUSTOMER HAS CONFIRMED IS NOT AN INVENTION: this "
                            "line used to say only 'never invent a dimension the user did not "
                            "state', and a model holding four measured bores read that as a ban, "
                            "recited them in prose and submitted patches with no numbers - so the "
                            "submission was refused for missing sizes it had been told four times. "
                            "Carry a confirmed measured number into the patch. What stays banned is "
                            "a number from nowhere: neither measured nor stated. "
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
                            "(flow THROUGH a cavity/duct - and its input_kind is 'body-surface' whenever "
                            "the file is the part around the passage; 'solid-body' with internal_cfd is "
                            "impossible for EVERY engine, because the only engine that takes solid-body "
                            "is gmsh and it produces a structural solid-volume from it, never a fluid "
                            "volume), 'conjugate_heat_transfer' (coupled fluid + "
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
                            "as a cavity (the usual external/internal CFD input). FOR internal_cfd ON A "
                            "FILE THAT IS THE METAL AROUND A PASSAGE, THE ANSWER IS 'body-surface', NOT "
                            "'solid-body'. This trips every time: the part IS a solid, so 'solid-body' "
                            "reads as the honest answer - but in this enum it means MESH THE METAL "
                            "ITSELF, which is a structural job, and every engine refuses it for internal "
                            "flow. 'body-surface' is what says 'use these surfaces to fill the cavity'. A "
                            "real conversation reached a complete, correct setup three times and was "
                            "refused each time for exactly this, then told to re-export a file that was "
                            "never wrong. Use 'solid-body' ONLY when the metal itself is what gets "
                            "meshed. 'solid-assembly' - a "
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


# THE SURVEYOR'S TWO TOOLS. Offered ONLY when the survey is switched on and this upload has a
# successful measurement: `INTAKE_TOOLS` above is never edited, so with the survey off the model is
# offered exactly the list it has always been offered. They are the channel from intake to the
# platform for the two things the chain needs a structured answer for: what the part is FOR (step 1,
# which the composition of the measurement depends on), and what the customer said to each question
# (step 4 and step 6), with their own words quoted so the application can check they said it.
SURVEY_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "survey_the_part",
            "description": (
                "Call this ONCE, as soon as the customer has said what the analysis is for (their first "
                "message usually does). The application composes the measurement of their file for that "
                "purpose and their own words, and returns the questions the measurement and the look "
                "could not settle. Those are the ONLY questions you ask about the geometry. Call it again "
                "only if the customer changes what the part is for, or names ports they had not named."),
            "parameters": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string", "enum": _PURPOSE_CHOICES,
                                "description": "What the customer said the analysis is for."},
                    "customer_words_verbatim": {
                        "type": "string",
                        "description": ("The customer's own words that said what it is for, quoted exactly "
                                        "from one of their messages. Never paraphrased.")},
                    "ports": {
                        "type": "array",
                        "description": ("The ports the customer has ALREADY named in their own words, "
                                        "exactly as stated, and nothing you inferred. Leave it out if they "
                                        "named none."),
                        "items": {"type": "object", "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": _ALL_BOUNDARY_ROLES},
                            "diameter_mm": {"type": "number"},
                            "near_mm": {"type": "array", "items": {"type": "number"}}},
                            "required": ["name", "type"]},
                    },
                },
                "required": ["purpose", "customer_words_verbatim"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_survey_question",
            "description": (
                "Record the customer's answer to ONE Surveyor question, in their own words. The "
                "application checks the quote against their latest message and the option against the "
                "question's own options, and refuses anything else. Never record an answer they did not "
                "If they decline, set skipped. If they defer or merely agree - \"go\", \"you decide\", "
                "\"sure\" - set took_default and quote that word: it is recorded as NOT an answer, the "
                "builder is told nobody confirmed it, and the question is PUT ONCE and never asked again. "
                "Do not keep asking it, and never hand the customer a sentence to type back."),
            "parameters": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "string", "description": "The id in square brackets."},
                    "option": {"type": "string",
                               "description": "The option they chose, exactly as listed. Omit when skipped."},
                    "mouth": {"type": "string",
                              "description": ("Only for the role question: WHICH mouth they gave the role in "
                                              "`option` to, by its id. One call per mouth, and the question is "
                                              "not settled until every mouth it names has one.")},
                    "role": {"type": "string",
                             "description": ("Only where the role question lists the mouths as its options: the "
                                             "role they gave the mouth in `option`.")},
                    "customer_words_verbatim": {
                        "type": "string",
                        "description": "Their words, quoted exactly from their latest message."},
                    "skipped": {"type": "boolean", "description": "They declined to answer."},
                    "took_default": {"type": "boolean",
                                     "description": "They told you to go with the default."},
                },
                "required": ["question_id", "customer_words_verbatim"],
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


# WHAT THE FILE IS, for this turn
#
# Intake has been blind since the product shipped, and not for want of data: the bytes are in object
# storage for thirty days, the measurement is stored against their digest, and `cad/regions.py` was
# pointed at a staging directory `api/v1/upload.py:253` deletes at the end of every upload. Of the
# 601 questions intake writes for itself across the 287 stored conversations, 321 name something the
# file already answers.
#
# Both halves of the fix are read here, once, together. A second read inside a tool call could
# disagree with the table the model is already holding, and a turn where the prompt says one bore and
# the admission check uses another is worse than either alone.

@dataclasses.dataclass(frozen=True)
class _GeometryReading:

    #: The keys the engines read off `surface_analysis`, or None when the measured phase did not run.
    reading: dict | None = None
    #: The whole stored measurement document, or None. What the conversation says comes from this.
    document: dict | None = None
    #: The Surveyor has something to compose for this upload: a successful measurement of these bytes.
    #: False everywhere else, and then nothing below changes.
    armed: bool = False
    #: The stored survey state for this upload, or None when none has been composed yet.
    survey: dict | None = None
    #: The upload these belong to, for the two survey tools. None when not armed.
    source_ref: object | None = None


async def _geometry_reading(state) -> _GeometryReading:
    """The stored measurement for this session's upload. Never raises, never blocks, never delays.

    Everything about this is an absence when it is not a success. There is no path here that fails a
    turn, and there is no path that makes the customer wait: the measurement already ran at upload,
    and this is a row read by primary key.
    """
    try:
        from meshpipeline.cad.regions import reading_for_source, stored_document_for_source
        from meshpipeline.pipeline.geometry_state import geometry_ref

        ref = geometry_ref(state)
        if ref is None:
            return _GeometryReading()
        document = await stored_document_for_source(ref)
        reading = await reading_for_source(ref)
        armed, survey = await _survey_for(ref, document)
        return _GeometryReading(reading=reading, document=document, armed=armed, survey=survey,
                                source_ref=ref if armed else None)
    except Exception as exc:                       # noqa: BLE001 - intake NEVER blocks on this
        logger.warning("Intake: the stored geometry reading was unavailable (%s) - the "
                       "conversation proceeds as it does with no measurement", exc)
        return _GeometryReading()


def _geometry_block(geometry: _GeometryReading) -> str:
    """The opening table, as a prompt block. Empty string when there is nothing measured.

    Empty is the contract: with no block the system prompt is character-for-character the prompt
    that ships today, so a missing, stale or failed measurement costs the conversation nothing.
    """
    try:
        from meshpipeline.agents.intake.geometry_brief import render_block

        if not geometry.armed:
            return render_block(geometry.document)
        return render_block(geometry.document, survey=geometry.survey, armed=True)
    except Exception as exc:                       # noqa: BLE001
        logger.warning("Intake: the geometry block could not be composed (%s)", exc)
        return ""


async def _survey_for(ref, document) -> tuple[bool, dict | None]:
    """Whether the Surveyor has anything for this upload, and its stored state. Never raises.

    Armed only by a SUCCESSFUL measurement of these bytes: there is nothing to compose without one, and
    a conversation with nothing to compose is a conversation the Surveyor stays out of.

    A MEASUREMENT THAT FAILED IS SILENCE HERE, and this docstring used to claim it was not: it said
    "the block says the file was not measured". It does not. `geometry_brief.render_block` returns the
    empty string for any document whose status is not `ok`, and an empty block is its documented
    fail-open - the system prompt is then character for character what it is with no measurement at all,
    so intake asks what it has always asked and the customer is told nothing.

    THAT IS RIGHT FOR A FAILURE OF OURS AND WRONG FOR A FACT ABOUT THE FILE. A broken STEP or a timeout
    is ours and the conversation should carry on; `unsupported_format` is the customer's to act on and no
    retry changes it, so today a .vtp upload holds a whole intake conversation with nothing measured
    behind it and hears about it only at submission (`pipeline/geometry_admission`). Saying it here means
    deciding what the customer reads in their first turn, which is a product decision and not a fix.
    """
    if not isinstance(document, dict) or document.get("status") != "ok":
        return False, None
    try:
        from meshpipeline.application import geometry_survey as gs

        return True, await gs.load(str(ref.owner_id), str(ref.source_id), sha256=str(ref.sha256))
    except Exception as exc:                       # noqa: BLE001 - never a turn
        logger.warning("Intake: the survey could not be read (%s); the conversation proceeds without it",
                       exc)
        return False, None


#: The panel's own header, as `geometry_brief.surveyor_panel` writes it. Only ever used to ask
#: "has this customer been shown one of these yet", never parsed.
_MARK = "- - - THE SURVEYOR - - -"


def reply_with_the_receipt(panel: str, assistant_text: str, said_before: list[str]) -> str:
    """The turn's reply with the Surveyor's receipt above it, or without it when it is a repeat.

    A FUNCTION RATHER THAN FOUR LINES IN `node_intake`, so that the thing under test is the thing
    that ships. What went wrong here is a question about the conversation so far and nothing else -
    has this customer already been shown this panel - and it does not need a turn to ask.

    Containment is exact: the panel is prepended verbatim and never reflowed, so if it is already
    somewhere in what the customer has been told, it is a repeat. If it differs by so much as the
    cell forecast then the measurement or the reading has changed, and that is shown and said,
    because an unlabelled second copy is indistinguishable from the bug.
    """
    body = panel.strip()
    if not body:
        return assistant_text
    if any(body in prior for prior in said_before):
        return assistant_text
    if any(_MARK in prior for prior in said_before):
        panel = chr(10) + "(the measurement or the reading changed - here it is again)" + panel
    return panel + chr(10) + assistant_text

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
INTAKE_PROMPT_BLOCKS: tuple = (
    ("quality_criteria", "evidence-backed production-grade bars the intake can cite",
     _block_quality_criteria),
    ("engine_first", "engine as direct user input; native follow-ups; propose+confirm fallback",
     _block_engine_first),
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

    # WHAT THE FILE IS, read ONCE for this turn, before the prompt is composed and before the loop
    # starts. Two readers want it and they must agree: the system prompt states the opening table to
    # the model, and the admission preview hands the same reading to the engine's measured rules.
    # Off, or with nothing stored and nothing retrievable, both are `None` and every line below runs
    # exactly as it did before this existed.
    _geometry = await _geometry_reading(state)

    system = compose_intake_system()
    system += _geometry_block(_geometry)
    if _awaiting_confirmation:
        system += _confirmation_block(state)
    state_messages = state.get("messages", [])
    llm_messages = _build_llm_messages(system=system, state_messages=state_messages)

    _budget = turn.assess_budget(state_messages, awaiting_confirmation=_awaiting_confirmation)
    if _budget.exhausted:
        logger.warning("Intake: MAX_TURNS=%d reached - nudging toward closure (fail-safe, never "
                       "forcing a submit) - job_id=%s", turn.MAX_TURNS, job_id)
        llm_messages, state_messages = turn.apply_budget_nudge(llm_messages, state_messages)

    _ctx = turn.hydrate(state, state_messages)
    # A JobPublisher only exists once a job does - and Intake runs BEFORE one. Rather than mint a
    # fake job id or instantiate a worker-owned publisher in the request path, the turn collects
    # the same typed events through a sink and returns them with the response.
    _trace_publisher = state.get("publish") or state.get("_publisher") or PublicTraceSink()

    _exec_state = IntakeExecutionState(
        session_id=_ctx.session_id, owner_id=_ctx.owner_id, revision=_ctx.revision,
        user_msg_count=_ctx.user_msg_count, latest_user_msg=_ctx.latest_user_msg,
        source_ref=_ctx.source_ref, rec_authorized=_ctx.rec_authorized,
        choice_deferred=_ctx.choice_deferred,
        pending=_ctx.pending, selection=_ctx.selection, approval=_ctx.approval,
        geometry_reading=_geometry.reading, geometry_document=_geometry.document,
        survey_armed=_geometry.armed, geometry_survey=_geometry.survey,
        survey_source_ref=_geometry.source_ref,
        customer_messages=tuple(str(m.get("content") or "") for m in state_messages
                                if isinstance(m, dict) and m.get("role") == "user")
        if _geometry.armed else ())
    _executor = IntakeToolExecutor(
        state=_exec_state, job_id=str(job_id), implemented_engines=_IMPLEMENTED_ENGINES,
        search_tool=_execute_intake_tool, trace=_trace_publisher)
    _policy = IntakeLoopPolicy(
        exec_state=_exec_state, executor=_executor,
        # INTAKE_MAX_ROUNDS is the ONLY Intake bound. No tool-call cap, no category cap, no
        # deadline, and no no-progress threshold: progress is counted and reported, never
        # enforced, because no measured Intake distribution justifies a value.
        limits_=_LoopLimits(max_rounds=icfg.INTAKE_MAX_ROUNDS))

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
        driver=_policy, provider_call=_intake_provider, messages=llm_messages,
        # The Surveyor's two tools ride only when it is armed for this upload, so with the survey off
        # the model is offered exactly `INTAKE_TOOLS`, object for object.
        tools=(INTAKE_TOOLS + SURVEY_TOOLS) if _geometry.armed else INTAKE_TOOLS,
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
    # model's conversational reply. Round exhaustion carries neither - it says nothing at all.
    assistant_text = str(_loop_result.payload or "")

    # The rendered refusal is correct but concatenates whatever rules fired, so the model rewrites
    # it for the user. The call carries NO tool, so the turn stays terminal, and the result is
    # checked: wording that names another engine or drops a declared value is discarded for the
    # rendered text.
    _in_tokens, _out_tokens = _policy.input_tokens, _policy.output_tokens
    if _exec_state.admission_facts and assistant_text == _exec_state.admission_block:
        _refusal = await refusal.explain(
            _exec_state.admission_facts, provider_call=_intake_provider,
            job_id=str(job_id), user_id=_ctx.owner_id)
        assistant_text = _refusal.text
        # The loop counts its own rounds only, and this call happens after it. Without these the
        # turn reports less than it spent.
        _in_tokens += _refusal.input_tokens
        _out_tokens += _refusal.output_tokens
        logger.info("Intake: refusal delivered %s (+%d/%d tokens) - job_id=%s",
                    _refusal.source, _refusal.input_tokens, _refusal.output_tokens, job_id)

    # THE SURVEYOR'S RECEIPT, ABOVE THE MODEL'S WORDS AND NOT INSTEAD OF THEM. It measured the file
    # and read seventeen rendered views of it before this turn, and until now the customer saw none
    # of that - only questions, which read as an interrogation rather than as what is left over
    # after most of the work was already done. Shown once, on the turn the look lands.
    #
    # AND ONCE MEANS ACROSS TURNS, WHICH IT DID NOT. `_exec_state` is rebuilt every turn, so
    # `surveyor_panel` starts empty every turn and the guard on it only ever stopped a second copy
    # WITHIN one turn. The customer answered something that moved the purpose, intake called
    # `survey_the_part` again, and the whole 900-character receipt was reprinted verbatim above the
    # next question. MEASURED on ahmed_variant_001: turns 2 and 3 carried identical panels, and turn
    # 3's own content was one sentence.
    #
    # Containment is the test and it is exact, because the panel is prepended verbatim and never
    # reflowed: if this panel is already somewhere in what the customer has been told, it is a
    # repeat. If it differs by so much as the cell forecast then the measurement or the reading has
    # CHANGED, which is worth showing - so it is shown, and labelled, because an unlabelled second
    # copy is what this comment is about.
    if _exec_state.surveyor_panel and assistant_text:
        # THE UNTRIMMED HISTORY, not `state_messages`. `apply_budget_nudge` may rebind that name to a
        # shortened list, and a panel trimmed out of the model's context is still a panel the customer
        # has on their screen. Reading the trimmed list would have reprinted it and called it a change.
        _said_before = [str(m.get("content") or "") for m in (state.get("messages") or [])
                        if isinstance(m, dict) and m.get("role") == "assistant"]
        _with = reply_with_the_receipt(_exec_state.surveyor_panel, assistant_text, _said_before)
        if _with == assistant_text:
            logger.info("Intake: the Surveyor's receipt was already shown unchanged, not repeating "
                        "it - job_id=%s", job_id)
        assistant_text = _with
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
