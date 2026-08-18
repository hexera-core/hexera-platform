# Responsibility: Turn a captured run into the episode shape training data is built from.
# Boundaries: a derived view over the canonical record; it changes nothing it reads.
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# tool definitions per agent
# capture is neutral: it does NOT import the agents. The static intake/reviewer tool
# definitions are INJECTED by the caller (build_episodes' `agent_tools` registry, supplied
# by the export task, which is allowed to know the agents). Builder tools ride the event
# payload. Best-effort: an absent registry entry just yields an empty tools sidecar.
def _agent_tools(agent: str, builder_tools: list | None, registry: dict | None = None) -> list:
    if agent == "builder":
        return list(builder_tools or [])
    return list((registry or {}).get(agent) or [])


def _sys(content: str) -> dict:
    return {"role": "system", "content": content or ""}


def _params_for(agent: str, model_configs: dict) -> tuple[str, dict]:
    cfg_row = (model_configs or {}).get(agent)
    if isinstance(cfg_row, str):
        return cfg_row, {}
    if not isinstance(cfg_row, dict):
        return "", {}
    model = cfg_row.get("model", "")
    params = {k: cfg_row[k] for k in ("temperature", "top_p", "top_k", "max_tokens")
              if k in cfg_row}
    return model, params


# per-agent episode builders
def _intake_episode(payload: dict, model_configs: dict, agent_tools: dict | None = None) -> dict:
    turns = payload.get("intake_turns") or []
    messages = [_sys(payload.get("intake_system_snapshot", ""))] + [
        t for t in turns if isinstance(t, dict) and t.get("role")
    ]
    model, params = _params_for("intake", model_configs)
    return {
        "agent": "intake", "kind": "llm", "model": model, "params": params,
        "tools": _agent_tools("intake", None, agent_tools),
        "messages": messages,
        "output": {
            "domain":         payload.get("domain", ""),
            "request_txt":    payload.get("request_txt", ""),
            "review_brief_txt": payload.get("review_brief_txt", ""),
            "purpose":        payload.get("purpose", ""),
            "input_kind":     payload.get("input_kind", ""),
            "mesh_engine":    payload.get("mesh_engine", ""),
            "dimensionality": payload.get("dimensionality", ""),
            "engine_params":  payload.get("engine_params", {}),
            "patches":        payload.get("intake_patches", []),
        },
        "outcome": {"submitted": bool(payload.get("request_txt")),
                    "max_turns_reached": bool(payload.get("max_turns_reached"))},
    }


def _planner_episode(payload: dict, model_configs: dict) -> dict:
    model, params = _params_for("planner", model_configs)
    messages = [
        _sys(payload.get("system_snapshot", "")),
        {"role": "user", "content": payload.get("user_message", "")},
        {"role": "assistant", "content": payload.get("response_text", "")},
    ]
    return {
        "agent": "planner", "kind": "llm", "model": model, "params": params, "tools": [],
        "messages": messages,
        "output": {"plan": payload.get("plan", {}), "is_revision": payload.get("is_revision", False)},
        "outcome": {"status": payload.get("status", "")},
    }


def _builder_llm_calls(records: dict, attempt_index: int) -> int | None:
    return records.get(attempt_index)


def _builder_episode(payload: dict, attempt: int, model_configs: dict,
                     llm_calls: int | None = None) -> dict:
    model, params = _params_for("builder", model_configs)
    histories = payload.get("builder_message_histories") or []
    # builder_message_histories is the full OpenAI conversation for the attempt
    messages = histories if isinstance(histories, list) and histories and \
        isinstance(histories[0], dict) else []
    if not messages:
        # older/partial capture: reconstruct a minimal shell from the snapshot + response
        messages = [_sys(payload.get("builder_system_message_snapshots", ""))]
    return {
        "agent": "builder", "kind": "llm", "attempt": attempt,
        "model": model, "params": params,
        "tools": _agent_tools("builder", payload.get("builder_tools_definition")),
        "messages": messages,
        "output": {"final_response": payload.get("builder_full_responses", ""),
                   "mode": payload.get("mode", "")},
        "outcome": {"spec_authored": bool(payload.get("spec_authored", False)),
                    "llm_calls": llm_calls,          # None = unknown, never a fabricated 0
                    "noop_count": payload.get("noop_count", 0)},
    }


def _reviewer_episode(payload: dict, review_n: int, model_configs: dict,
                      read_conversation: Callable[[str], list],
                      agent_tools: dict | None = None) -> dict:
    model, params = _params_for("reviewer", model_configs)
    dirs = payload.get("reviewer_review_dirs")
    review_dir = dirs[review_n - 1] if isinstance(dirs, list) and review_n - 1 < len(dirs) \
        else (dirs if isinstance(dirs, str) else "")
    # The reviewer's full OpenAI conversation (system + user + assistant[content=THOUGHT +
    # tool_calls] + tool observations) lives in the review dir's conversation.jsonl - the
    # thinking model's per-round deliberation is the assistant `content`.
    messages = read_conversation(review_dir) if review_dir else []
    if not messages:
        messages = [_sys(payload.get("reviewer_system_snapshots", ""))]
    verdict = payload.get("verdict", "")
    return {
        "agent": "reviewer", "kind": "llm", "review": review_n,
        "model": model, "params": params, "tools": _agent_tools("reviewer", None, agent_tools),
        "messages": messages,
        "output": {
            "verdict":          verdict,
            "axis_findings":    payload.get("axis_findings", {}),
            "patch_checks":     payload.get("patch_checks", {}),
            "rebuild_required": bool(payload.get("rebuild_required", False)),
        },
        "outcome": {"verdict": verdict, "score": 1 if verdict == "PASS" else 0,
                    "tool_calls": payload.get("tool_calls", 0),
                    "tool_limit_reached": bool(payload.get("tool_limit_reached", False))},
    }


def _final_result_episode(payload: dict) -> dict:
    return {
        "agent": "final_result", "kind": "deterministic",
        "decision": {"final_result": payload.get("final_result", {}) or {}},
        "output": {"terminal_message": payload.get("terminal_message", "")},
    }


def _engine_select_episode(payload: dict) -> dict:
    return {"agent": "engine_select", "kind": "deterministic",
            "decision": {"chosen": payload.get("chosen", ""),
                         "source": payload.get("source", "")}}


def _executor_episode(payload: dict, attempt: int) -> dict:
    _stdout = payload.get("executor_stdouts", "") or ""
    _stderr = payload.get("executor_stderrs", "") or ""
    return {"agent": "executor", "kind": "deterministic", "attempt": attempt,
            "decision": {
                "success":     bool(payload.get("success", False)),
                "stdout_tail": _stdout[-2000:],
                "stderr_tail": _stderr[-2000:],
            }}


def _classifier_episode(payload: dict, attempt: int) -> dict:
    return {"agent": "classifier", "kind": "deterministic", "attempt": attempt,
            "decision": {
                "section":      payload.get("section", ""),
                "summary":      payload.get("summary", ""),
                "failed_gate":  payload.get("failed_gate", ""),
                "failed_axes":  payload.get("failed_axes", []),
                "error_source": payload.get("error_source", ""),
                "builder_mode": payload.get("builder_mode", ""),
            }}


# orchestration
def build_episodes(events: list, *, model_configs: dict,
                   read_conversation: Callable[[str], list],
                   agent_tools: dict | None = None) -> list[dict]:
    episodes: list[dict] = []
    n_builder = n_executor = n_classifier = n_review = 0

    # Intake can emit intake_complete more than once (an upload greeting, then the real
    # submit; a revise-then-confirm turn). Only the FINAL one is the canonical requirements
    # - emit exactly one intake episode, at that event's position.
    _last_intake = max((i for i, e in enumerate(events)
                        if e.event_type == "intake_complete"), default=-1)

    # Canonical Builder run records, keyed by the attempt they belong to. Attempts with no
    # record (deterministic engine driver, superseded generation) stay absent on purpose.
    _builder_rounds: dict[int, int] = {}
    for e in events:
        if e.event_type != "agent_run":
            continue
        p_ = e.payload if isinstance(e.payload, dict) else {}
        if p_.get("role") != "builder":
            continue
        rounds = (p_.get("tally") or {}).get("rounds")
        attempt = p_.get("pipeline_attempt")
        if isinstance(rounds, int) and isinstance(attempt, int):
            _builder_rounds[attempt + 1] = rounds        # episodes number attempts from 1

    for i, e in enumerate(events):
        et, p = e.event_type, (e.payload or {})
        ep: dict | None = None
        if et == "intake_complete":
            if i != _last_intake:
                continue                      # skip the superseded intake submissions
            ep = _intake_episode(p, model_configs, agent_tools)
        elif et == "engine_select_run":
            ep = _engine_select_episode(p)
        elif et == "planner_run":
            ep = _planner_episode(p, model_configs)
        elif et == "builder_attempt":
            n_builder += 1
            ep = _builder_episode(p, n_builder, model_configs,
                                  llm_calls=_builder_llm_calls(_builder_rounds, n_builder))
        elif et == "executor_run":
            n_executor += 1
            ep = _executor_episode(p, n_executor)
        elif et == "classifier_run":
            n_classifier += 1
            ep = _classifier_episode(p, n_classifier)
        elif et == "reviewer_run":
            n_review += 1
            ep = _reviewer_episode(p, n_review, model_configs, read_conversation, agent_tools)
        elif et == "final_result_built":
            ep = _final_result_episode(p)
        # intake_turn events are folded into the intake episode; skip standalone
        if ep is not None:
            episodes.append(ep)

    for seq, ep in enumerate(episodes, 1):
        ep["seq"] = seq
        ep["name"] = _episode_name(ep)
    return episodes


def _episode_name(ep: dict) -> str:
    a = ep["agent"]
    if "attempt" in ep:
        return f"{a}.attempt_{ep['attempt']}"
    if "review" in ep:
        return f"{a}.review_{ep['review']}"
    return a


def episode_filename(ep: dict) -> str:
    return f"{ep['seq']:02d}_{ep['name']}.json"


def episode_to_sft(ep: dict) -> dict | None:
    if ep.get("kind") != "llm":
        return None
    msgs = ep.get("messages") or []
    if len(msgs) < 2:            # a system prompt alone is not a training example
        return None
    line: dict[str, Any] = {"messages": msgs}
    if ep.get("tools"):
        line["tools"] = ep["tools"]
    return line


def read_conversation_jsonl(review_dir: str) -> list[dict]:
    try:
        p = Path(review_dir) / "conversation.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("trajectory: could not read %s/conversation.jsonl: %s", review_dir, exc)
        return []
