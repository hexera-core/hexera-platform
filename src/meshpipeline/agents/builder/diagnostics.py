# Responsibility: Extend a builder run record with the facts diagnostics need.
# Boundaries: the builder's slice of the shared accountability vocabulary.
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from meshpipeline.contracts.agent_loop import DiagnosticValue


@dataclass(frozen=True)
class BuilderRunExtension:

    engine: str = ""
    mode: str = ""                            # "initial" | "retry" | "rebuild"
    strategy: str = ""                        # which Builder STRATEGY produced this invocation:
                                              # the interactive canonical loop, or an engine-owned
                                              # deterministic driver. Consumers must not have to
                                              # infer it from which counters happen to be zero.
    plan_calls: int = 0                       # REAL planning/model rounds, never synthesized
    authored_spec: bool = False
    run_mesh_calls: int = 0
    run_mesh_successes: int = 0
    submitted: bool = False
    auto_submitted: bool = False              # the APPLICATION performed the terminal action
    forced_tools: tuple[str, ...] = ()        # which tools the workflow state forced, in order
    repeated_tool_signature: str = ""         # the stuck signature, hashed by the detector
    truncated_rounds: int = 0
    checkpoint_recoveries: int = 0
    #: The prompt-token count each recovery fired at, in order. The count alone says a run hit the
    #: ceiling; the figures say WHERE, which is what a threshold can be tuned against later.
    checkpoint_recovery_tokens: tuple[int, ...] = ()

    def sanitized(self) -> Mapping[str, DiagnosticValue]:
        return {
            "engine": self.engine,
            "mode": self.mode,
            "strategy": self.strategy,
            "plan_calls": self.plan_calls,
            "authored_spec": self.authored_spec,
            "run_mesh_calls": self.run_mesh_calls,
            "run_mesh_successes": self.run_mesh_successes,
            "submitted": self.submitted,
            "auto_submitted": self.auto_submitted,
            "forced_tools": self.forced_tools,
            "repeated_tool_signature": self.repeated_tool_signature,
            "truncated_rounds": self.truncated_rounds,
            "checkpoint_recoveries": self.checkpoint_recoveries,
            "checkpoint_recovery_tokens": self.checkpoint_recovery_tokens,
        }


__all__ = ["BuilderRunExtension"]
