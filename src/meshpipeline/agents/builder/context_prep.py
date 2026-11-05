# Responsibility: Gather and bound the per-attempt context before the loop starts.
# Boundaries: preparation only, so the loop begins with everything it needs and no I/O of its own.
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from meshpipeline.agents.builder.context import (
    _BUILDER_CONTEXT_WINDOW,
    _CHECKPOINT_INJECT_THRESHOLD,
    _CHECKPOINT_RECOVER_THRESHOLD,
    _CHECKPOINT_REINJECTION_GAP,
    _count_message_tokens,
)
from meshpipeline.agents.builder.tools import get_spec_run_files
from meshpipeline.contracts.model_inference import Conversation

logger = logging.getLogger(__name__)


@dataclass
class BuilderContextPreparer:

    workspace: Path
    job_id: str
    engine: str
    last_prompt_tokens: int = 0
    checkpoint_pending: bool = False
    #: What the last hard recovery fired at. Kept because _recover zeroes the running count, so
    #: the caller that records the event would otherwise see 0 and report a ceiling nobody hit.
    last_recovery_tokens: int = 0
    last_checkpoint_tokens: int = 0

    @property
    def knowledge_block_path(self) -> Path:
        return self.workspace / "knowledge_block.txt"

    def note_provider_tokens(self, prompt_tokens: int) -> None:
        if prompt_tokens > 0:
            self.last_prompt_tokens = prompt_tokens

    def note_knowledge_block_written(self) -> None:
        self.checkpoint_pending = False
        self.last_checkpoint_tokens = self.last_prompt_tokens

    def prepare(self, round_index: int, messages: list[dict]) -> list[dict] | None:
        local = _count_message_tokens(cast("Conversation", messages))
        if local > self.last_prompt_tokens:
            self.last_prompt_tokens = local
        logger.info(
            "Builder [%s]: round=%d prompt_tokens local=%d effective=%d "
            "(%.0f%% of %dk window) - inject_thr=%d recover_thr=%d",
            self.job_id, round_index, local, self.last_prompt_tokens,
            100 * self.last_prompt_tokens / _BUILDER_CONTEXT_WINDOW,
            _BUILDER_CONTEXT_WINDOW // 1024,
            _CHECKPOINT_INJECT_THRESHOLD, _CHECKPOINT_RECOVER_THRESHOLD)

        if self.last_prompt_tokens >= _CHECKPOINT_RECOVER_THRESHOLD:
            return self._recover(round_index, messages)
        if (self.last_prompt_tokens >= _CHECKPOINT_INJECT_THRESHOLD
                and not self.checkpoint_pending
                and self.last_prompt_tokens >= self.last_checkpoint_tokens
                + _CHECKPOINT_REINJECTION_GAP):
            self._inject(round_index, messages)
        return None

    # the two paths
    def _recover(self, round_index: int, messages: list[dict]) -> list[dict]:
        kb_text = ""
        if self.knowledge_block_path.exists():
            try:
                kb_text = self.knowledge_block_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        # Reconstruct from the authored mesh spec on disk. The file names are the ENGINE's
        # declared run_policy.required_files - no artifact filename is hardcoded here (a
        # hardcoded per-engine branch once left snappy recovery with no spec at all).
        artifact_label = "the authored mesh spec (verbatim from disk)"
        script_text = ""
        for rel in get_spec_run_files(self.engine):
            path = self.workspace / rel
            if not path.exists():
                continue
            try:
                script_text += f"--- {rel} ---\n" + path.read_text(encoding="utf-8") + "\n"
            except Exception:  # noqa: BLE001
                pass

        parts: list[str] = [
            "[Context recovered - previous messages compressed to fit context window]"]
        if kb_text:
            parts.append(f"## Your Knowledge Block (self-authored)\n{kb_text}")
        else:
            parts.append("## Knowledge Block\n(not yet written - reconstruct from "
                         f"{artifact_label} below)")
        if script_text:
            parts.append(f"## Current {artifact_label}\n{script_text}")
        parts.append("Continue from where you left off.")

        original_system = next(
            (m for m in messages if isinstance(m, dict) and m.get("role") == "system"), None)
        rebuilt: list[dict] = ([original_system] if original_system else []) + [
            {"role": "user", "content": "\n\n".join(parts)}]

        prev = self.last_prompt_tokens
        self.last_recovery_tokens = prev
        self.last_prompt_tokens = 0
        self.checkpoint_pending = False
        logger.info(
            "Builder [%s]: hard context recovery at %d prompt tokens (%.0f%% of window) "
            "- knowledge_block=%s mesh_gen=%s",
            self.job_id, prev, 100 * prev / _BUILDER_CONTEXT_WINDOW,
            bool(kb_text), bool(script_text))
        return rebuilt

    def _inject(self, round_index: int, messages: list[dict]) -> None:
        messages.append({"role": "user", "content": (
            f"[SYSTEM] Context checkpoint required - you are using "
            f"{self.last_prompt_tokens:,} of {_BUILDER_CONTEXT_WINDOW:,} input tokens "
            f"({100 * self.last_prompt_tokens / _BUILDER_CONTEXT_WINDOW:.0f}% of context "
            "window). Write your knowledge block to knowledge_block.txt NOW using write_file "
            "before any other tool call. See your system prompt for the required format. "
            "After writing the file, continue your work normally.")})
        self.checkpoint_pending = True
        logger.info(
            "Builder [%s]: knowledge block checkpoint injected at %d prompt tokens "
            "(%.0f%% of context window) - round %d",
            self.job_id, self.last_prompt_tokens,
            100 * self.last_prompt_tokens / _BUILDER_CONTEXT_WINDOW, round_index)

    @staticmethod
    def wrote_knowledge_block(tool: str, args: dict, raw_result: Any) -> bool:
        if tool != "write_file" or "knowledge_block" not in str(args.get("path", "")).lower():
            return False
        try:
            return "written" in json.loads(raw_result)
        except Exception:  # noqa: BLE001
            return False


__all__ = ["BuilderContextPreparer"]
