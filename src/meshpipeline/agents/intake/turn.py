# Responsibility: Carry one intake turn: its budget, its transcript and the patch it produces.
# Boundaries: turn mechanics; it decides nothing about intent.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The conversation bound. Reaching it never forces a submit - see `budget_nudge`.
MAX_TURNS = 12

#: The fail-safe nudge. A blind "submit now" invited the model to INVENT the values it was still
#: missing: the validator would reject them, but the prompt should not ask for a fabricated
#: submission in the first place. So: submit only if genuinely complete, otherwise ask the single
#: most important remaining question - never loop, never invent.
BUDGET_NUDGE = (
    "This conversation is getting long. Do NOT invent, assume, or guess any value, and do "
    "NOT call submit_requirements unless every required field is genuinely established from "
    "what the user actually told you. If nothing required is still missing, submit now. "
    "Otherwise, identify the SINGLE most important piece of information still missing and "
    "ask the user exactly that one question - do not re-ask anything already answered. If "
    "the user cannot provide something that is genuinely required, explain precisely what "
    "is still needed and why. 'Required' means only the fields submit_requirements marks "
    "required: if the user has said to proceed with standard defaults, a question about anything "
    "else is answered by that instruction - record the assumption in request_txt and submit."
)


@dataclass(frozen=True)
class TurnBudget:

    turn_count: int
    exhausted: bool

    @property
    def turn_number(self) -> int:
        return self.turn_count + 1


def assess_budget(state_messages, *, awaiting_confirmation: bool) -> TurnBudget:
    turns = sum(1 for m in state_messages
                if isinstance(m, dict) and m.get("role") == "assistant")
    return TurnBudget(turns, turns >= MAX_TURNS and not awaiting_confirmation)


def apply_budget_nudge(llm_messages: list, state_messages: list) -> tuple[list, list]:
    return (
        list(llm_messages) + [{"role": "user", "content": BUDGET_NUDGE}],
        list(state_messages) + [{"role": "user", "content": BUDGET_NUDGE, "_synthetic": True}],
    )


@dataclass(frozen=True)
class TurnContext:

    job_id: str
    session_id: str
    owner_id: str
    revision: Any
    latest_user_msg: str
    user_msg_count: int
    source_ref: Any
    pending: dict | None
    selection: dict | None
    approval: dict | None
    rec_authorized: bool


def hydrate(state, state_messages) -> TurnContext:
    from meshpipeline.agents.intake import admission_token as at
    from meshpipeline.agents.intake import recommendation as rec
    from meshpipeline.pipeline.geometry_state import geometry_ref

    raw_gate = state.get("intake_gate")
    gate = dict(raw_gate) if isinstance(raw_gate, dict) and raw_gate else {}

    def _sub(key):
        value = gate.get(key)
        return dict(value) if isinstance(value, dict) and value else None

    user_msgs = [m for m in state_messages if isinstance(m, dict) and m.get("role") == "user"]
    latest = str(user_msgs[-1].get("content", "")) if user_msgs else ""
    return TurnContext(
        job_id=str(state.get("job_id", "unknown")),
        session_id=str(state.get("session_id", "")),
        owner_id=str(state.get("user_id", "")),
        revision=at.revision_of(state_messages),
        latest_user_msg=latest,
        user_msg_count=len(user_msgs),
        source_ref=geometry_ref(state),
        pending=_sub("admission"),
        selection=_sub("selection"),
        approval=_sub("approval"),
        rec_authorized=rec.recommendation_requested(latest),
    )


def serialise_transcript(llm_messages, assistant_text: str) -> list:
    messages = list(llm_messages) + (
        [{"role": "assistant", "content": assistant_text}] if assistant_text else [])
    out = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") == "system":
            continue
        entry: dict = {"role": m.get("role"), "content": str(m.get("content", ""))}
        for carried in ("tool_calls", "tool_call_id"):
            if m.get(carried):
                entry[carried] = m[carried]
        if m.get("_synthetic"):
            entry["_synthetic"] = True
        out.append(entry)
    return out


# #
# THE THREE PATCHES a turn can return. Each is the complete graph patch - the node returns one of
# them and constructs nothing itself, so a key cannot be added on one path and forgotten on another.
# #

@dataclass(frozen=True)
class TurnRecord:

    finish_reason: str
    usage: dict | None
    agent_run: dict
    search_events: Any
    public_trace: Any
    budget: TurnBudget
    assistant_text: str = ""
    transcript: list = field(default_factory=list)
    system_snapshot: str = ""


def _gate_patch(context: TurnContext) -> dict:
    return {"selection": context.selection, "admission": context.pending,
            "approval": context.approval}


def _common(context: TurnContext, record: TurnRecord) -> dict:
    return {
        "dispatch_confirmed": False,
        "messages": ([{"role": "assistant", "content": record.assistant_text}]
                     if record.assistant_text else []),
        "intake_gate": _gate_patch(context),
        "_intake_search_events": record.search_events,
        # The turn's PUBLIC trace, already projected for this deployment's mode - the caller
        # delivers it with the response and stores it on the session.
        "_public_trace": record.public_trace,
        "_intake_agent_run_event": {"type": "agent_run", "payload": record.agent_run},
    }


def provider_failure_patch(failure_marker: str, agent_run: dict) -> dict:
    return {"api_failure": failure_marker,
            "_intake_agent_run_event": {"type": "agent_run", "payload": agent_run}}


def continuing_patch(context: TurnContext, record: TurnRecord) -> dict:
    return {
        **_common(context, record),
        "_intake_training_event": {
            "type": "intake_turn",
            "payload": {
                "turn": record.budget.turn_number,
                "finish_reason": record.finish_reason,
                "max_turns_reached": record.budget.exhausted,
                "usage": record.usage,
            },
        },
    }


def completed_patch(context: TurnContext, record: TurnRecord, requirements) -> dict:
    llm_metadata = [{"turn": record.budget.turn_number, "finish_reason": record.finish_reason,
                     "usage": record.usage, "completed": True}]
    return {
        **_common(context, record),
        "domain": requirements.domain,
        "request_txt": requirements.request_txt,
        "review_brief_txt": requirements.review_brief_txt,
        "intake_patches": requirements.intake_patches,
        "dimensionality": requirements.dimensionality,
        "purpose": requirements.purpose,
        "input_kind": requirements.input_kind,
        "requested_mesh_fidelity": requirements.requested_mesh_fidelity,
        "effective_mesh_fidelity": requirements.effective_mesh_fidelity,
        "mesh_fidelity_source": requirements.mesh_fidelity_source,
        "mesh_engine": requirements.mesh_engine,
        "engine_source": requirements.engine_source,
        "engine_params": requirements.engine_params,
        "_intake_training_event": {
            "type": "intake_complete",
            "payload": {
                "domain": requirements.domain,
                "max_turns_reached": record.budget.exhausted,
                "finish_reason": record.finish_reason,
                "intake_turns": record.transcript,
                "intake_system_snapshot": record.system_snapshot,
                "intake_llm_metadata": llm_metadata,
                "request_txt": requirements.request_txt,
                "review_brief_txt": requirements.review_brief_txt,
                "intake_patches": requirements.intake_patches,
                "dimensionality": requirements.dimensionality,
                "mesh_engine": requirements.mesh_engine,
                "engine_source": requirements.engine_source,
                "engine_params": requirements.engine_params,
                # The two declarations the whole run keys off: `purpose` picks the boundary roles
                # and half the review rubric; `input_kind` says what the geometry IS. A sample that
                # cannot say what the mesh was FOR is a sample you cannot learn from.
                "purpose": requirements.purpose,
                "input_kind": requirements.input_kind,
            },
        },
    }
