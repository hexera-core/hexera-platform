# Responsibility: Prove recipe memory (design §5.10) on fakes: the signature is id-free and stable when ids change,
#                 a clean path becomes an id-free recipe that replays onto another site's own entities, retrieval is
#                 deterministic, writes are DEV-ONLY (held-out and clean-control can never write), and the flag-off
#                 search and prompt are exactly what they were.
# Boundaries: hermetic. No kernel, no model.
from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import recipes as R
from meshpipeline.cad.repair_v4.proposer import RECIPES_PARAGRAPH, system_prompt
from meshpipeline.cad.repair_v4.search import DetGenerator
from meshpipeline.cad.repair_v4.types import Site

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64


def open_site(sid: str = "S@open", *, loop: tuple[str, ...] = ("E21", "E22", "E23", "E24"), span: float = 4.24,
              diag: float = 123.0, faces: tuple[str, ...] = (), meshers=("gmsh_prod", "netgen_occ")) -> Site:
    return Site(id=sid, kind="open", entities=list(faces), xyz=[], meshers_failing=list(meshers), dist_over_h=None,
                graph={"free_loops": [{"edges": list(loop), "span_mm": span},
                                      {"edges": ["E90", "E91", "E92"], "span_mm": span * 3}],
                       "diagonal_mm": diag, "nodes": [{"id": f, "surface": "plane", "angular_span_deg": 0.0}
                                                      for f in faces]},
                view_png=None)  # type: ignore[arg-type]


def solidless_site(sid: str = "S@invalid") -> Site:
    return Site(id=sid, kind="invalid", entities=[], xyz=[], meshers_failing=["gmsh_prod", "netgen_occ"],
                dist_over_h=None, graph={"hard_codes": ["no_solids"]}, view_png=None)  # type: ignore[arg-type]


def hole_box_path(loop=("E21", "E22", "E23", "E24")) -> list[dict]:
    """The winning path of the 3x3 mm hole box: fill_loop on the loop, then make_volume."""
    return [{"via": {"op": "fill_loop", "targets": list(loop), "params": {"continuity": "G0"}, "source": "det"},
             "sites": [open_site(loop=loop)]},
            {"via": {"op": "make_volume", "targets": [], "params": {}, "source": "det"},
             "sites": [solidless_site()]}]


def renamed(site: Site, mapping: dict[str, str], new_id: str = "S7") -> Site:
    """`site` with every entity id renamed (as a renumbering op would) and a new site id."""
    def m(x: Any) -> Any:
        if isinstance(x, str):
            return mapping.get(x, x)
        if isinstance(x, list):
            return [m(v) for v in x]
        if isinstance(x, dict):
            return {k: m(v) for k, v in x.items()}
        return x
    return Site(id=new_id, kind=site.kind, entities=m(site.entities), xyz=site.xyz,
                meshers_failing=site.meshers_failing, dist_over_h=site.dist_over_h, graph=m(site.graph),
                view_png=None)


# --- the signature ------------------------------------------------------------------------------------------------


def test_the_signature_is_stable_when_every_id_changes() -> None:
    s = open_site(faces=("F3", "F4"))
    t = renamed(s, {"E21": "E101", "E22": "E102", "E23": "E103", "E24": "E104", "F3": "F77", "F4": "F78",
                    "E90": "E5", "E91": "E6", "E92": "E7"})
    assert R.signature(s) == R.signature(t) and R.signature_key(R.signature(s)) == R.signature_key(R.signature(t))
    assert "S@open" not in json.dumps(R.signature(s)) and "E21" not in json.dumps(R.signature(s))


def test_the_signature_is_ratios_not_millimetres() -> None:
    small = open_site(span=4.24, diag=123.0)
    big = open_site(span=42.4, diag=1230.0)                        # the same part, ten times larger
    assert R.signature(small) == R.signature(big)
    assert R.signature(open_site(span=10.0, diag=123.0))["loop"] != R.signature(small)["loop"]


def test_different_kinds_are_never_similar() -> None:
    assert R.similarity(R.signature(open_site()), R.signature(solidless_site())) is None


# --- a clean path becomes a recipe that replays elsewhere ------------------------------------------------------------


def test_the_hole_box_path_becomes_an_id_free_recipe() -> None:
    recipe = R.recipe_from_path(hole_box_path(), diagonal_mm=123.0)
    assert recipe is not None
    fill, vol = recipe.steps
    assert (fill.op, fill.role, fill.loop_rank, fill.replayable) == ("fill_loop", "free_loop", 0, True)
    assert (vol.op, vol.role, vol.replayable) == ("make_volume", "none", True)
    assert fill.site["kind"] == "open" and vol.site["hard"] == ["no_solids"]
    blob = json.dumps(recipe.to_json())
    assert "E21" not in blob and "S@open" not in blob
    assert R.Recipe.from_json(json.loads(blob)).id == recipe.id


def test_the_recipe_is_the_same_whatever_the_ids_were() -> None:
    a = R.recipe_from_path(hole_box_path(), diagonal_mm=123.0)
    b = R.recipe_from_path(hole_box_path(loop=("E1", "E2", "E3", "E4")), diagonal_mm=123.0)
    assert a is not None and b is not None and a.id == b.id


def test_a_step_replays_on_another_sites_own_entities_and_scales_millimetres() -> None:
    path = [{"via": {"op": "sew", "targets": ["F3", "F4"], "params": {"tol_ladder": [0.01, 0.1]}},
             "sites": [open_site(faces=("F3", "F4"))]}]
    [step] = R.recipe_from_path(path, diagonal_mm=100.0).steps
    assert step.role == "site_entities" and step.params["tol_ladder"] == {"rel": [1e-4, 1e-3]}
    other = open_site(faces=("F40", "F41"), diag=200.0)
    targets, params = R.replay(step, other, diagonal_mm=200.0)
    assert targets == ["F40", "F41"] and params["tol_ladder"] == pytest.approx([0.02, 0.2])


def test_a_loop_step_replays_on_the_same_ranked_loop() -> None:
    [fill, _] = R.recipe_from_path(hole_box_path(), diagonal_mm=123.0).steps
    other = open_site(loop=("E7", "E8", "E9", "E10"))
    assert R.replay(fill, other, diagonal_mm=123.0) == (["E7", "E8", "E9", "E10"], {"continuity": "G0"})


def test_an_unexpressible_step_is_kept_for_the_model_but_never_replayed() -> None:
    path = [{"via": {"op": "remove_duplicate_faces", "targets": [], "params": {"pairs": [["F1", "F2"]]}},
             "sites": [open_site(faces=("F1", "F2"))]}]
    [step] = R.recipe_from_path(path, diagonal_mm=100.0).steps
    assert not step.replayable and R.replay(step, open_site(faces=("F1", "F2")), diagonal_mm=100.0) is None


# --- the store: DEV ONLY ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("split", ["heldout", "clean", "", "Dev", "dev "])
def test_only_the_dev_split_may_write(tmp_path: Path, split: str) -> None:
    store = R.RecipeStore(R.DirBackend(tmp_path))
    recipe = R.recipe_from_path(hole_box_path(), diagonal_mm=123.0)
    with pytest.raises(R.RecipeWriteRefused):
        store.record(recipe, split=split, wave_type="normal", file_sha256=SHA_A)
    assert list(tmp_path.rglob("*.json")) == []


def test_the_held_out_wave_never_writes_even_if_mislabelled_dev(tmp_path: Path) -> None:
    store = R.RecipeStore(R.DirBackend(tmp_path))
    with pytest.raises(R.RecipeWriteRefused):
        store.record(R.recipe_from_path(hole_box_path(), diagonal_mm=123.0), split="dev", wave_type="held_out",
                     file_sha256=SHA_A)
    assert list(tmp_path.rglob("*.json")) == []


def test_a_dev_write_is_versioned_json_with_no_name_or_path(tmp_path: Path) -> None:
    store = R.RecipeStore(R.DirBackend(tmp_path))
    recipe = R.recipe_from_path(hole_box_path(), diagonal_mm=123.0)
    key = store.record(recipe, split="dev", wave_type="normal", file_sha256=SHA_A,
                       provenance={"wave_id": "w1", "path": "/secret/a.step", "name": "a.step"})
    assert key == f"recipes/v1/{R.signature_key(recipe.steps[0].site)}/{recipe.id}/{SHA_A}.json"
    body = json.loads((tmp_path / key).read_text())
    assert body["schema"] == R.RECIPE_SCHEMA and body["witness"] == {"file_sha256": SHA_A, "split": "dev",
                                                                       "wave_id": "w1"}
    with pytest.raises(ValueError):
        store.record(recipe, split="dev", wave_type="normal", file_sha256="a.step")
    store.record(recipe, split="dev", wave_type="normal", file_sha256=SHA_A)   # idempotent: never overwrites
    assert len(list(tmp_path.rglob("*.json"))) == 1


@pytest.mark.parametrize("split,wave_type", [("heldout", "held_out"), ("clean", "normal"), ("dev", "held_out")])
def test_a_memory_for_a_held_out_or_clean_control_shard_is_read_only(tmp_path: Path, split: str,
                                                                     wave_type: str) -> None:
    R.RecipeStore(R.DirBackend(tmp_path)).record(R.recipe_from_path(hole_box_path(), diagonal_mm=123.0),
                                                 split="dev", wave_type="normal", file_sha256=SHA_A)
    memory = R.RecipeMemory.for_shard(str(tmp_path), split=split, wave_type=wave_type)
    assert memory.store is None and len(memory.book) == 1            # it reads what dev learned
    clean = SimpleNamespace(clean=True, path_steps=hole_box_path(("E1", "E2", "E3", "E4")))
    assert memory.learn(clean, file_sha256=SHA_B, diagonal_mm=123.0) is None
    assert len(list(tmp_path.rglob("*.json"))) == 1 and memory.describe()["writes"] is False


def test_a_dev_memory_learns_only_from_clean_results(tmp_path: Path) -> None:
    memory = R.RecipeMemory.for_shard(str(tmp_path), split="dev", wave_type="normal")
    assert memory.learn(SimpleNamespace(clean=False, path_steps=hole_box_path()), file_sha256=SHA_A,
                        diagonal_mm=123.0) is None
    assert memory.learn(SimpleNamespace(clean=True, path_steps=hole_box_path()), file_sha256=SHA_A,
                        diagonal_mm=123.0) is not None
    assert memory.counts["written"] == 1


def test_the_gcs_backend_lists_reads_and_never_overwrites() -> None:
    from google.api_core.exceptions import PreconditionFailed

    objects: dict[str, str] = {}

    class Blob:
        def __init__(self, name: str) -> None:
            self.name = name

        def exists(self) -> bool:
            return self.name in objects

        def download_as_text(self) -> str:
            return objects[self.name]

        def upload_from_string(self, text: str, content_type: str, if_generation_match: int) -> None:
            assert if_generation_match == 0
            if self.name in objects:
                raise PreconditionFailed("exists")
            objects[self.name] = text

    bucket = SimpleNamespace(blob=Blob, list_blobs=lambda prefix: [Blob(n) for n in sorted(objects)
                                                                   if n.startswith(prefix)])
    store = R.RecipeStore(R.GcsBackend("gs://eval-bucket/crs", bucket=bucket))
    recipe = R.recipe_from_path(hole_box_path(), diagonal_mm=123.0)
    key = store.record(recipe, split="dev", wave_type="normal", file_sha256=SHA_A)
    assert f"crs/{key}" in objects
    assert store.backend.put_if_absent(key, "{}") is False
    assert list(store.load().recipes) == [recipe.id]


# --- retrieval ------------------------------------------------------------------------------------------------------


def _book_from(paths: list[tuple[list[dict], str]], tmp: Path) -> R.RecipeBook:
    store = R.RecipeStore(R.DirBackend(tmp))
    for path, sha in paths:
        store.record(R.recipe_from_path(path, diagonal_mm=123.0), split="dev", wave_type="normal", file_sha256=sha)
    return store.load()


def _variants() -> list[tuple[list[dict], str]]:
    sew = [{"via": {"op": "sew", "targets": [], "params": {"tol_ladder": [0.01]}}, "sites": [open_site()]}]
    other_mesher = [{"via": {"op": "fill_loop", "targets": ["E90", "E91", "E92"], "params": {"continuity": "G0"}},
                     "sites": [open_site(meshers=("gmsh_alt",))]}]
    return [(hole_box_path(), SHA_A), (hole_box_path(("E1", "E2", "E3", "E4")), SHA_B), (sew, SHA_C),
            (other_mesher, SHA_C)]


def test_retrieval_is_deterministic_whatever_the_order_things_were_written(tmp_path: Path) -> None:
    variants = _variants()
    books = []
    for i in range(4):
        order = variants[:]
        random.Random(i).shuffle(order)
        books.append(_book_from(order, tmp_path / str(i)))
    query = open_site(loop=("E500", "E501", "E502", "E503"))
    results = [[(h.recipe_id, h.step_index, h.score, h.support) for h in b.lookup(query, k=3)] for b in books]
    assert all(r == results[0] for r in results) and len({b.snapshot for b in books}) == 1
    assert results[0] == [(h.recipe_id, h.step_index, h.score, h.support) for h in books[0].lookup(query, k=3)]


def test_retrieval_ranks_by_similarity_then_support(tmp_path: Path) -> None:
    book = _book_from(_variants(), tmp_path)
    hits = book.lookup(open_site(loop=("E500", "E501", "E502", "E503")), k=3)
    assert hits[0].step.op == "fill_loop" and hits[0].support == 2  # two clean files witnessed the hole-box recipe
    assert hits[0].score == R.MAX_SIMILARITY
    assert all(a.score >= b.score for a, b in zip(hits, hits[1:]))
    assert book.lookup(solidless_site())[0].step.op == "make_volume"


def test_a_site_of_another_kind_retrieves_nothing(tmp_path: Path) -> None:
    book = _book_from(_variants(), tmp_path)
    proximity = Site(id="S1", kind="proximity", entities=["F1", "F2"], xyz=[], meshers_failing=["gmsh_prod"],
                     dist_over_h=0.3, graph={}, view_png=None)  # type: ignore[arg-type]
    assert book.lookup(proximity) == [] and book.advice([proximity]) == []


# --- the generators --------------------------------------------------------------------------------------------------


def _state(sites: list[Site]) -> Any:
    from meshpipeline.cad.repair_v4.workspace import Workspace
    ws = Workspace(brep_path="/x.brep", ids={"E7": "a", "E8": "b", "E9": "c", "E10": "d", "F1": "e"}, labels={},
                   history=[], original="/x.brep")
    return SimpleNamespace(ws=ws, sites=sites, lineage=("S0",))


def test_det_generator_replays_recipes_first_on_the_sites_own_entities(tmp_path: Path) -> None:
    book = _book_from(_variants(), tmp_path)
    site = open_site(loop=("E7", "E8", "E9", "E10"))
    with_book = DetGenerator(recipes=book, diagonal=lambda ws: 123.0).propose(_state([site]), k=12)
    assert with_book[0].op == "fill_loop" and with_book[0].targets == ["E7", "E8", "E9", "E10"]
    assert "recipe" in with_book[0].rationale and "2 similar clean dev file" in with_book[0].rationale


def test_without_recipes_the_det_generator_is_unchanged(tmp_path: Path) -> None:
    site = open_site(loop=("E7", "E8", "E9", "E10"))
    plain = DetGenerator(diagonal=lambda ws: 123.0).propose(_state([site]), k=12)
    empty = DetGenerator(recipes=R.RecipeBook(), diagonal=lambda ws: 123.0).propose(_state([site]), k=12)
    assert [(c.op, c.targets, c.params, c.rationale) for c in plain] == \
           [(c.op, c.targets, c.params, c.rationale) for c in empty]
    assert not any("recipe" in c.rationale for c in plain)


# --- the AI proposer -------------------------------------------------------------------------------------------------


def test_the_flag_off_prompt_is_unchanged_and_the_flag_on_prompt_explains_the_advice() -> None:
    off = system_prompt(stage_names=("faces",))
    on = system_prompt(stage_names=("faces",), recipes=True)
    assert RECIPES_PARAGRAPH.strip() not in off and RECIPES_PARAGRAPH.strip() in on
    assert on.replace(RECIPES_PARAGRAPH, "") == off


def test_the_proposer_shows_what_worked_on_similar_sites(tmp_path: Path) -> None:
    from tests.unit.cad.repair_v4.test_proposer import Fake, make, reply

    book = _book_from(_variants(), tmp_path)
    fake = Fake([reply()])
    p = make(fake, recipes=book)
    st = _state([open_site(loop=("E7", "E8", "E9", "E10"))])
    st.verdict, st.budget = SimpleNamespace(clean=False), None
    p.propose(st, k=3)
    turn = fake.calls[0].messages[1]["content"]
    text = turn if isinstance(turn, str) else turn[0]["text"]
    body = json.loads(text.split("\n", 1)[1])
    [advice] = body["what_worked_on_similar_sites"]
    assert advice["site"] == "S@open" and advice["similar_recipes"][0]["sequence"][0]["targets"] == "free_loop[0]"
    assert "E21" not in text                                          # the model sees roles, never another part's ids
    assert RECIPES_PARAGRAPH.strip() in fake.calls[0].messages[0]["content"]
