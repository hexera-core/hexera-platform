# Responsibility: Verify the context checkpoint recovers a long attempt and records what it cost.
# Boundaries: the checkpoint seam - what the builder writes into its knowledge block is its own business.
from __future__ import annotations

from pathlib import Path

from meshpipeline.agents.builder.context import (
    _CHECKPOINT_INJECT_THRESHOLD,
    _CHECKPOINT_RECOVER_THRESHOLD,
)
from meshpipeline.agents.builder.context_prep import BuilderContextPreparer

_SYSTEM = {"role": "system", "content": "the builder system prompt"}


def _preparer(tmp_path: Path) -> BuilderContextPreparer:
    return BuilderContextPreparer(job_id="job", workspace=tmp_path, engine="snappy")


def test_a_short_attempt_is_left_alone(tmp_path):
    prep = _preparer(tmp_path)
    prep.note_provider_tokens(1_000)
    messages = [_SYSTEM, {"role": "user", "content": "start"}]
    assert prep.prepare(1, messages) is None
    assert len(messages) == 2, "an attempt well inside the window was interfered with"


def test_crossing_the_inject_threshold_asks_for_the_knowledge_block(tmp_path):
    prep = _preparer(tmp_path)
    prep.note_provider_tokens(_CHECKPOINT_INJECT_THRESHOLD + 1)
    messages = [_SYSTEM]
    assert prep.prepare(5, messages) is None, "injection must not rebuild the conversation"
    assert "knowledge_block.txt" in messages[-1]["content"]
    assert prep.checkpoint_pending is True


def test_the_ask_is_not_repeated_until_the_gap_has_passed(tmp_path):
    # Re-asking every round would spend the window on the instruction to save the window.
    prep = _preparer(tmp_path)
    prep.note_provider_tokens(_CHECKPOINT_INJECT_THRESHOLD + 1)
    messages = [_SYSTEM]
    prep.prepare(5, messages)
    before = len(messages)
    prep.prepare(6, messages)
    assert len(messages) == before


def test_recovery_replaces_the_history_and_keeps_the_written_block(tmp_path):
    (tmp_path / "knowledge_block.txt").write_text("what I have established so far", encoding="utf-8")
    prep = _preparer(tmp_path)
    prep.note_provider_tokens(_CHECKPOINT_RECOVER_THRESHOLD + 1)
    rebuilt = prep.prepare(9, [_SYSTEM, {"role": "user", "content": "a very long history"}])
    assert rebuilt is not None and len(rebuilt) == 2
    assert rebuilt[0]["role"] == "system", "the system prompt did not survive recovery"
    assert "what I have established so far" in rebuilt[1]["content"]
    assert "a very long history" not in rebuilt[1]["content"]


def test_recovery_still_happens_when_no_block_was_ever_written(tmp_path):
    # The builder can ignore the instruction. Recovery is what keeps the attempt alive either way,
    # so it must not depend on a file that may not exist.
    prep = _preparer(tmp_path)
    prep.note_provider_tokens(_CHECKPOINT_RECOVER_THRESHOLD + 1)
    rebuilt = prep.prepare(9, [_SYSTEM, {"role": "user", "content": "history"}])
    assert rebuilt is not None
    assert "not yet written" in rebuilt[1]["content"]


def test_the_token_figure_survives_for_the_run_record(tmp_path):
    # _recover zeroes the running count, so a caller reading it afterwards would record a ceiling
    # nobody hit. The figure is what a threshold can be tuned against later.
    prep = _preparer(tmp_path)
    prep.note_provider_tokens(_CHECKPOINT_RECOVER_THRESHOLD + 5_000)
    prep.prepare(9, [_SYSTEM])
    assert prep.last_prompt_tokens == 0
    assert prep.last_recovery_tokens == _CHECKPOINT_RECOVER_THRESHOLD + 5_000


def test_the_run_record_carries_the_figures_not_only_the_count():
    from meshpipeline.agents.builder.diagnostics import BuilderRunExtension

    record = BuilderRunExtension(checkpoint_recoveries=2,
                                 checkpoint_recovery_tokens=(104_900, 110_200))
    sanitized = record.sanitized()
    assert sanitized["checkpoint_recoveries"] == 2
    assert sanitized["checkpoint_recovery_tokens"] == (104_900, 110_200)
