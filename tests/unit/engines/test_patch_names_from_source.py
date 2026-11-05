# Responsibility: Pin that an engine which writes the file's own patches lets the user set their roles but refuses a rename or a merge before anything is built.
# Boundaries: the declared admission and the intake's preview, with the confirmed patches given; plus the reader of the stored confirmation.
from __future__ import annotations

import json

from meshpipeline.agents.intake.validation import preview_admission
from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
from meshpipeline.engines.registry import engine_names, get_spec

# THE DEFECT (dev-areen set_top_box job 4b173bf0, 2026-10-11): the user set domain_xmin inlet,
# domain_xmax outlet and merged the other four sides into "closed_walls". The intake accepted it,
# the summary was approved, and the snap-grid mesh - which writes the file's six sides under the
# file's names - failed the patch contract after meshing.

SIDES = ("domain_xmin", "domain_xmax", "domain_ymin", "domain_ymax", "domain_zmin", "domain_zmax")
CHT = ("conjugate_heat_transfer", "solid-assembly")


def _admit(patches):
    ev = AdmissionEvidence(engine="snapgrid", purpose=CHT[0], input_kind=CHT[1], dimensionality="3D",
                           patches=tuple(PatchSummary(name=n, type=t) for n, t in patches),
                           geometry_form="cad", source_patches=SIDES)
    return [r for r in get_spec("snapgrid").admit(ev) if r.code == "patch_names_fixed_by_source"]


def test_only_engines_that_say_so_keep_the_files_names():
    assert get_spec("snapgrid").patch_names_from_source is True
    assert [n for n in engine_names() if get_spec(n).patch_names_from_source] == ["snapgrid"]


def test_roles_on_the_files_own_patches_are_free():
    roles = [("domain_xmin", "inlet"), ("domain_xmax", "outlet")] + [(s, "wall") for s in SIDES[2:]]
    assert _admit(roles) == []
    assert _admit([(s, "outlet") for s in SIDES] + [("wall", "wall")]) == []


def test_a_merge_or_a_rename_is_refused_with_the_way_out():
    merged = [("domain_xmin", "inlet"), ("domain_xmax", "outlet"), ("closed_walls", "wall")]
    found = _admit(merged)
    assert found, "four sides merged into one patch must be refused before meshing"
    msg = found[0].message
    assert "keeps the file's 6 domain sides as separate patches" in msg
    assert "set each one's role" in msg and "merging or renaming them isn't possible" in msg
    renamed = [("front", "inlet")] + [(s, "outlet") for s in SIDES[1:]]
    assert _admit(renamed)


def test_the_intake_preview_refuses_it_as_impossible():
    merged = [{"name": "domain_xmin", "type": "inlet"}, {"name": "domain_xmax", "type": "outlet"},
              {"name": "closed_walls", "type": "wall"}]
    prev = preview_admission("snapgrid", *CHT, dimensionality="3D", patches=merged,
                             geometry_form="cad", source_patches=SIDES)
    assert prev["verdict"] == "impossible"
    assert "patch_names_fixed_by_source" in prev["blocking_rule_codes"]
    # unknown file patches claim nothing
    prev2 = preview_admission("snapgrid", *CHT, dimensionality="3D", patches=merged, geometry_form="cad")
    assert "patch_names_fixed_by_source" not in (prev2.get("blocking_rule_codes") or [])


def test_the_confirmed_openings_are_read_from_the_stored_confirmation(monkeypatch):
    import meshpipeline.contracts.object_storage as osm
    from meshpipeline.application.geometry_confirmation import confirmed_opening_names

    record = {"openings": [{"name": s, "role": "outlet"} for s in SIDES] + [{"name": "lid", "role": "not_an_opening"}]}

    class Store:
        def get_bytes(self, *, object_key):
            assert object_key.endswith("confirmed.json")
            return json.dumps(record).encode()

    monkeypatch.setattr(osm, "get_object_store", lambda *a, **k: Store())
    assert confirmed_opening_names("s1") == SIDES

    class Broken:
        def get_bytes(self, *, object_key):
            raise OSError("down")

    monkeypatch.setattr(osm, "get_object_store", lambda *a, **k: Broken())
    assert confirmed_opening_names("s1") == ()
