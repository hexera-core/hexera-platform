# Responsibility: Verify the corpus tool's catalogue tally and its triangle-check corroboration of heals.
# Boundaries: hermetic - the tessellator and the mesh checker are stubbed; no CAD is read.
from __future__ import annotations

import importlib.util
from pathlib import Path

from meshpipeline.cad import surface_checks

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_catalogue_under_test", REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load()
summarise_catalogue = corpus.summarise_catalogue


def test_an_operation_the_corpus_never_reached_is_listed_not_omitted():
    out = summarise_catalogue([{"operations": [{"op": "fill_planar_hole", "status": "kept"}]}])
    assert {"fill_nonplanar_hole", "heal_self_intersections"} <= set(out["never_exercised"])


def test_a_declined_only_new_op_is_reported_as_proposed_but_never_ran():
    rows = [{"operations": [{"op": "heal_self_intersections", "status": "rolled_back",
                             "reason": "declined:not_isolated"}]}]
    out = summarise_catalogue(rows)
    assert out["tally"]["heal_self_intersections"]["declined"] == 1
    assert "heal_self_intersections" in out["only_declined"]


def test_corroboration_reads_the_triangle_check(monkeypatch):
    monkeypatch.setattr(corpus, "_tessellate_to_stl", lambda shape, path: path)
    monkeypatch.setattr(surface_checks, "self_intersection_report", lambda p, **kw: None)
    assert corpus.heal_corroborated(object()) is True
    monkeypatch.setattr(surface_checks, "self_intersection_report", lambda p, **kw: {"pairs": 3})
    assert corpus.heal_corroborated(object()) is False


def test_a_check_that_cannot_run_is_none(monkeypatch):
    def boom(shape, path):
        raise RuntimeError("no kernel")
    monkeypatch.setattr(corpus, "_tessellate_to_stl", boom)
    assert corpus.heal_corroborated(object()) is None
