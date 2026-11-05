# Responsibility: Verify the shared prompt files name no engine, and the prompt directory is genuinely populated.
from __future__ import annotations

from pathlib import Path

import pytest

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
PROMPTS_DIR = APP / "prompts"


def _engine_names() -> list[str]:
    from meshpipeline.engines.registry import engine_names
    return list(engine_names())


# Engine identities (the roster, live from the registry) + engine-owned MESHER COMMAND
# vocabulary that must arrive via bundle fragments, not shared prose. File extensions are
# deliberately NOT here: .msh/.vtu are cross-engine pipeline artifacts (the review surface
# preview, VTK exports) that shared prose may legitimately describe data-conditionally.
_ENGINE_VOCAB = (
    "snappyhexmesh", "cartesianmesh", "cartesian2dmesh",
    "splitmeshregions", "tetgen", "vmtkmeshgenerator",
)


@pytest.mark.parametrize("prompt_path", sorted(PROMPTS_DIR.glob("**/*.txt")))
def test_shared_prompt_names_no_engine(prompt_path):
    text = prompt_path.read_text(encoding="utf-8").lower()
    hits = [n for n in _engine_names() if n.lower() in text]
    hits += [tok for tok in _ENGINE_VOCAB if tok in text]
    assert not hits, (
        f"{prompt_path.name} contains engine identity/vocabulary {hits} - engine prose "
        "belongs in the engine bundle (briefing/pack/rubric), composed at runtime")


def test_the_prompt_dir_is_actually_populated():
    assert len(list(PROMPTS_DIR.glob("**/*.txt"))) >= 2
