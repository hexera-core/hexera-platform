# Responsibility: Hold the corpus harness to its honesty about the model: an AI posture (the
#                 investigator or the tournament live) with no model router installed silently
#                 runs the rules author, so the run must say so loudly and mark every row.
# Boundaries: hermetic - the settings module and the router seam are patched; no model, no CAD.
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load():
    spec = importlib.util.spec_from_file_location(
        "repair_corpus_no_router_under_test",
        REPO / "devtools" / "validation" / "repair_corpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


corpus = _load()


class _Settings:
    def __init__(self, investigator="off", tournament="off"):
        self.CAD_REPAIR_INVESTIGATOR = investigator
        self.CAD_REPAIR_TOURNAMENT = tournament


@pytest.mark.parametrize("investigator,tournament", [("live", "off"), ("off", "live"),
                                                     ("live", "live")])
def test_a_live_ai_posture_with_no_router_is_named(monkeypatch, investigator, tournament):
    from meshpipeline.contracts import model_inference

    monkeypatch.setattr(model_inference, "_router", None)
    gap = corpus.ai_posture_without_router(_Settings(investigator, tournament))
    assert gap and "no model router" in gap


@pytest.mark.parametrize("investigator,tournament", [("off", "off"), ("shadow", "off"),
                                                     ("off", "shadow")])
def test_no_live_ai_posture_needs_no_router(monkeypatch, investigator, tournament):
    from meshpipeline.contracts import model_inference

    monkeypatch.setattr(model_inference, "_router", None)
    assert corpus.ai_posture_without_router(_Settings(investigator, tournament)) == ""


def test_an_installed_router_closes_the_gap(monkeypatch):
    from meshpipeline.contracts import model_inference

    monkeypatch.setattr(model_inference, "_router", object())
    assert corpus.ai_posture_without_router(_Settings("live", "live")) == ""


def test_every_row_of_a_routerless_ai_run_is_marked(monkeypatch, tmp_path):
    def one(path, scratch, **_):
        return {"file": path.name, "outcome": "repaired"}

    monkeypatch.setattr(corpus, "_one", one)
    files = []
    for name in ("a.step", "b.step"):
        p = tmp_path / name
        p.write_bytes(b"x")
        files.append(p)
    written = []

    class _Bundle:
        def write_row(self, row):
            written.append(dict(row))

    rows = corpus.run_files(files, tmp_path, bundle=_Bundle(),
                            row_marks={"investigator_status": "no_router"})
    assert all(r["investigator_status"] == "no_router" for r in rows)
    # streamed rows carry the mark too: a run that dies half way is still marked
    assert all(r["investigator_status"] == "no_router" for r in written)
    unmarked = corpus.run_files(files[:1], tmp_path)
    assert "investigator_status" not in unmarked[0]
