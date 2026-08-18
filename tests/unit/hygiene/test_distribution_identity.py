# Responsibility: Verify the distribution's identity: its name, its schema versions and its migration baseline.
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).parents[3]


def test_the_distribution_is_named_meshpipeline():
    # The distribution name is what the images `pip install` and what every provenance test
    # imports; a rename here silently breaks the Dockerfile's wheel install.
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert cfg["project"]["name"] == "meshpipeline"


def test_the_final_result_schema_version_is_independent():
    from meshpipeline.application.final_result import FINAL_RESULT_SCHEMA_VERSION
    assert isinstance(FINAL_RESULT_SCHEMA_VERSION, int)
    assert FINAL_RESULT_SCHEMA_VERSION == 4   # v3: the four mesh-detail fields, three tiers


def test_the_alembic_baseline_is_one_canonical_revision():

    roots: list[str] = []
    for p in (REPO / "alembic" / "versions").glob("*.py"):
        if p.name.startswith("__"):
            continue
        assert re.match(r"^\d{4}_[a-z0-9_]+\.py$", p.name), f"non-canonical migration filename: {p.name}"
        for node in ast.walk(ast.parse(p.read_text())):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "down_revision" for t in node.targets)
                    and isinstance(node.value, ast.Constant) and node.value.value is None):
                roots.append(p.name)
    assert len(roots) == 1, f"exactly one canonical baseline (root) revision expected, got {roots}"
