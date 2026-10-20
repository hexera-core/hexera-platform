# Responsibility: Verify a boundary name the file or the stage carries reaches the case under the same spelling on both sides - the declared patch, the staged part and the matcher between them - even when a mesher keeps that name for itself.
# Boundaries: the name rule, admission's region matching, the engines' region staging and the stage's confirmation; no mesher runs.
from __future__ import annotations

from types import SimpleNamespace

import pytest

from meshpipeline.agents.intake.validation import _validate_patch_names
from meshpipeline.api.v1.geometry import ConfirmedOpening, ConfirmIn
from meshpipeline.application.geometry_confirmation import distinct_opening_names, patches_from
from meshpipeline.cad.ingest.surface import safe_solid_name
from meshpipeline.contracts.patch_names import (
    case_name,
    is_reserved,
    normalize_declaration,
    same_patch_name,
)
from meshpipeline.engines.base import _unassigned_regions, _unmatched_walls
from meshpipeline.engines.declared_boundary import stage_regions
from meshpipeline.engines.purposes import PURPOSES

_ROLES = PURPOSES["internal_cfd"].boundary_roles


def _declared_walls(parts: list[str]) -> list[str]:
    typed = [{"name": p, "type": "wall"} for p in parts] + [
        {"name": "in", "type": "inlet"}, {"name": "out", "type": "outlet"}]
    ch = normalize_declaration(typed, allowed_roles=_ROLES)
    return [p["name"] for p in ch.patches if p["type"] == "wall"]


# an annular duct's two walls, gmsh's volume name, the OpenFOAM header, in any capitals
_PARTS = [["outer", "inner"], ["solid", "fins"], ["Outer", "FoamFile"], ["seedZone", "hub"]]


@pytest.mark.parametrize("parts", _PARTS)
@pytest.mark.parametrize("as_file", ["raw", "canonical"])
def test_a_part_with_a_reserved_name_matches_the_wall_declared_for_it(parts, as_file):
    # the user names each wall after the file's part - the only names they can see
    walls = _declared_walls(parts)
    file_names = parts if as_file == "raw" else [safe_solid_name(p) for p in parts]
    evidence = SimpleNamespace(surface_analysis={"region_names": file_names})
    assert _unmatched_walls(evidence, walls) == []
    assert _unassigned_regions(evidence, walls) == []
    staged = stage_regions({n: [i] for i, n in enumerate(file_names)}, walls)
    # every part is written under the declared wall, so every declared wall gets its faces
    assert sorted(staged) == sorted(walls)
    assert not any(is_reserved(n) for n in staged)


def test_the_file_part_and_the_declared_wall_are_spelled_by_one_rule():
    for raw in ("outer", "Outer", "solid", "FoamFile", "thinZone3", "car body", "Inlet-1"):
        declared = _declared_walls([raw, "other"])[0]
        assert same_patch_name(declared, raw) and same_patch_name(declared, safe_solid_name(raw))
        assert not is_reserved(safe_solid_name(raw)) and not is_reserved(case_name(raw))
    assert safe_solid_name("outer") == "outer_patch" and safe_solid_name("inner") == "inner"


def test_a_part_no_wall_matches_is_never_staged_under_a_meshers_own_name():
    # several walls, none of them this part: it keeps its own name, but not snappy's "outer" box
    staged = stage_regions({"outer": [0], "a": [1], "b": [2]}, ["a", "b"])
    assert sorted(staged) == ["a", "b", "outer_patch"]


# the stage: a sticker cannot take the built-in wall's name

def _confirm(names_roles):
    ops = [ConfirmedOpening(id=i, name=n, role=r, diameter_mm=50.0, centroid_mm=[i * 100.0, 0, 0])
           for i, (n, r) in enumerate(names_roles)]
    return ConfirmIn(input_kind="fluid-domain", flow="internal", openings=ops,
                     size_mm=[300.0, 60.0, 60.0], scale_to_m=0.001, unit="mm")


@pytest.mark.parametrize("sticker", ["wall", "Wall", "WALL"])
@pytest.mark.parametrize("named_apart_at_the_stage", [True, False])
def test_a_sticker_called_wall_never_meets_the_built_in_wall(sticker, named_apart_at_the_stage):
    body = _confirm([(sticker, "inlet"), ("outlet", "outlet")])
    if named_apart_at_the_stage:
        body = body.model_copy(update={"openings": distinct_opening_names(body.openings)})
    patches = patches_from(body)
    names = [p["name"] for p in patches]
    for i, a in enumerate(names):
        assert not any(same_patch_name(a, b) for b in names[i + 1:]), names
    assert [p["type"] for p in patches].count("wall") == 1
    # and the intake reads it without a question
    ch = normalize_declaration(patches, allowed_roles=_ROLES)
    assert _validate_patch_names(ch.patches) == []
    assert not ch.renames
    if named_apart_at_the_stage:
        assert names[-1] == "wall"                       # the sticker moved, said on the stage
        assert body.openings[0].name.casefold().startswith("wall_")
