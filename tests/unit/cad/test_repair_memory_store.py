"""Task 24: the cross-run, fingerprint-keyed outcome store.

The non-negotiable this file exists to prove: PRODUCTION READS ARE NULL BY CONSTRUCTION. Not
"empty results" - no store. `NullOutcomeStore` is the default, and nothing under `src/` is allowed
to construct the file-backed one.
"""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest

from meshpipeline.cad.repair import memory

SRC = Path(memory.__file__).resolve().parents[3]


def _outcome(**over):
    fields = {"part_sha256": "a" * 64, "fingerprint": "f-1", "defect_code": "open_shell",
              "mesh_kind": "tet", "stage": "regions", "op": "sew_faces", "outcome": "kept",
              "measured": {"cleared": 1, "deviation_mm": 0.5}}
    fields.update(over)
    return memory.outcome(**fields)


# --------------------------------------------------------------------------- null by construction

def test_null_store_is_the_default():
    assert isinstance(memory.default_store(), memory.NullOutcomeStore)


def test_the_null_store_answers_nothing_for_every_query():
    store = memory.NullOutcomeStore()
    assert store.exact("a" * 64, "f-1", "sew_faces") == ()
    assert store.family("open_shell", "tet", "regions", "sew_faces") == {}


def test_a_null_store_reads_nothing_even_live(monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "live", raising=False)
    assert memory.history_turn(memory.default_store(), part_sha256="a" * 64,
                               fingerprints=("f-1",), defect_code="open_shell",
                               mesh_kind="tet", stage="regions", ops=("sew_faces",)) == {}
    assert memory.history_turn(None, part_sha256="a" * 64, fingerprints=("f-1",),
                               defect_code="open_shell") == {}


def test_production_reads_nothing():
    """AST: only `devtools/` may name `JsonlOutcomeStore`. `src/` may define it and nothing else.

    A runtime check cannot prove this - a construction on a path no test walks would pass - so the
    proof is static, over every module that ships.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            name = ""
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else "")
            if name == "JsonlOutcomeStore":
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], f"src/ constructs the file-backed store: {offenders}"


def test_the_shipping_default_injection_is_the_null_one():
    """Runtime companion to the AST sweep: the module-level default, as a caller would get it."""
    first = memory.default_store()
    assert type(first) is memory.NullOutcomeStore
    assert not hasattr(first, "write"), "the null store must not even offer a write path"


def test_memory_imports_no_database_no_network_no_cloud():
    tree = ast.parse(Path(memory.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("sqlalchemy", "psycopg", "sqlite3", "requests", "httpx", "google", "boto3",
                   "redis", "urllib"):
        assert banned not in imported, f"memory.py imports {banned}"


def test_cad_memory_imports_no_agents_and_no_pipeline():
    text = Path(memory.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        mod = (node.module if isinstance(node, ast.ImportFrom) and node.module
               else "")
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [mod]
        for name in names:
            assert "meshpipeline.agents" not in name
            assert "meshpipeline.pipeline" not in name


# --------------------------------------------------------------------------- the file store

def test_store_round_trips_without_geometry(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    store = memory.JsonlOutcomeStore(path)
    assert store.write([_outcome()]) == 1
    text = path.read_text(encoding="utf-8")
    assert "brep" not in text and "/" not in text
    again = memory.JsonlOutcomeStore(path)
    got = again.exact("a" * 64, "f-1", "sew_faces")
    assert len(got) == 1
    assert got[0].op == "sew_faces"
    assert got[0].measured == {"cleared": 1, "deviation_mm": 0.5}


def test_a_hostile_outcome_cannot_smuggle_a_path_or_geometry(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    rogue = memory.Outcome(part_sha256="a" * 64, fingerprint="f-1", defect_code="open_shell",
                           mesh_kind="tet", stage="regions",
                           op="/Users/someone/parts/secret.step", outcome="kept",
                           measured={"brep": b"SOLID", "stl": [1, 2], "seconds": math.inf,
                                     "nested": {"geometry": "x"}})
    memory.JsonlOutcomeStore(path).write([rogue])
    text = path.read_text(encoding="utf-8")
    for leak in ("/Users", "secret.step", "brep", "SOLID", "stl", "geometry"):
        assert leak not in text, f"{leak!r} reached the file: {text}"
    assert json.loads(text)["measured"] == {"seconds": None}


def test_malformed_line_is_counted_not_raised(tmp_path):
    path = tmp_path / "outcomes.jsonl"
    good = json.dumps(_outcome().to_dict(), allow_nan=False)
    path.write_text("\n".join(["{not json", good, "[]", '{"op": 3}', "", good]) + "\n",
                    encoding="utf-8")
    store = memory.JsonlOutcomeStore(path)
    assert store.malformed == 3, "the two good lines and the blank one are not malformed"
    assert len(store.exact("a" * 64, "f-1", "sew_faces")) == 2


def test_a_missing_file_is_an_empty_store_not_an_error(tmp_path):
    store = memory.JsonlOutcomeStore(tmp_path / "nope.jsonl")
    assert store.exact("a" * 64, "f-1", "sew_faces") == ()
    assert store.malformed == 0


def test_same_part_twice_hits_exact(tmp_path):
    """The fingerprints are content-derived, so the same part uploaded twice keys the same way."""
    path = tmp_path / "o.jsonl"
    memory.JsonlOutcomeStore(path).write([_outcome(outcome="rolled_back")])
    store = memory.JsonlOutcomeStore(path)
    assert [o.outcome for o in store.exact("a" * 64, "f-1", "sew_faces")] == ["rolled_back"]
    assert store.exact("b" * 64, "f-1", "sew_faces") == (), "a different part is a different key"
    assert store.exact("a" * 64, "f-2", "sew_faces") == (), "a different face is a different key"
    assert store.exact("a" * 64, "f-1", "fill_hole") == (), "a different op is a different key"


def test_family_counts_each_outcome(tmp_path):
    path = tmp_path / "o.jsonl"
    rows = ([_outcome(fingerprint=f"f-{i}", outcome="kept") for i in range(3)]
            + [_outcome(fingerprint=f"g-{i}", outcome="rolled_back") for i in range(2)]
            + [_outcome(defect_code="tiny_edge", outcome="kept")])
    memory.JsonlOutcomeStore(path).write(rows)
    store = memory.JsonlOutcomeStore(path)
    assert store.family("open_shell", "tet", "regions", "sew_faces") == {"kept": 3,
                                                                        "rolled_back": 2}
    assert store.family("tiny_edge", "tet", "regions", "sew_faces") == {"kept": 1}
    assert store.family("open_shell", "hex", "regions", "sew_faces") == {}


def test_write_appends_rather_than_truncating(tmp_path):
    path = tmp_path / "o.jsonl"
    memory.JsonlOutcomeStore(path).write([_outcome()])
    memory.JsonlOutcomeStore(path).write([_outcome(fingerprint="f-2")])
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_a_loaded_store_is_read_only_until_write(tmp_path):
    path = tmp_path / "o.jsonl"
    store = memory.JsonlOutcomeStore(path)
    store.write([_outcome()])
    assert store.exact("a" * 64, "f-1", "sew_faces") == (), (
        "a store is a snapshot of the file it loaded; a write does not retroactively feed reads")


# --------------------------------------------------------------------------- notes -> outcomes

def test_outcomes_of_fans_a_note_out_over_its_fingerprints():
    notes = memory.RunNotes()
    notes.add(memory.note(region_index=0, fingerprints=("f-1", "f-2"), op="sew_faces",
                          defect_code="open_shell", outcome="kept", measured={"cleared": 2}))
    got = memory.outcomes_of(notes, part_sha256="a" * 64, mesh_kind="tet", stage="regions")
    assert [o.fingerprint for o in got] == ["f-1", "f-2"]
    assert {o.part_sha256 for o in got} == {"a" * 64}
    assert all(o.outcome == "kept" for o in got)


def test_outcomes_of_nothing_is_nothing():
    assert memory.outcomes_of(None, part_sha256="a" * 64) == ()
    assert memory.outcomes_of(memory.RunNotes(), part_sha256="a" * 64) == ()


# --------------------------------------------------------------------------- the history turn

def test_off_adds_no_history_turn(tmp_path, monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "off", raising=False)
    path = tmp_path / "o.jsonl"
    memory.JsonlOutcomeStore(path).write([_outcome()])
    assert memory.history_turn(memory.JsonlOutcomeStore(path), part_sha256="a" * 64,
                               fingerprints=("f-1",), defect_code="open_shell",
                               mesh_kind="tet", stage="regions", ops=("sew_faces",)) == {}


def test_live_history_turn_carries_exact_and_family(tmp_path, monkeypatch):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "live", raising=False)
    path = tmp_path / "o.jsonl"
    memory.JsonlOutcomeStore(path).write([_outcome(), _outcome(fingerprint="f-9",
                                                              outcome="rolled_back")])
    turn = memory.history_turn(memory.JsonlOutcomeStore(path), part_sha256="a" * 64,
                               fingerprints=("f-1",), defect_code="open_shell",
                               mesh_kind="tet", stage="regions", ops=("sew_faces",))
    assert list(turn) == ["history"]
    assert turn["history"]["family"] == {"sew_faces": {"kept": 1, "rolled_back": 1}}
    assert [e["fingerprint"] for e in turn["history"]["exact"]] == ["f-1"]
    assert json.dumps(turn, allow_nan=False)


#: PINNED LITERALLY, not derived from `memory.EXACT_PER_TURN`. A table written as
#: `(EXACT_PER_TURN + 1, EXACT_PER_TURN)` moves with the constant and passes any mutation of it -
#: which is how a cap test on this plan passed vacuously once already.
@pytest.mark.parametrize("rows, kept", [(0, 0), (1, 1), (11, 11), (12, 12), (13, 12), (32, 12)])
def test_history_turn_clips_exact(tmp_path, monkeypatch, rows, kept):
    import meshpipeline.settings.cad_repair as rcfg
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MEMORY", "live", raising=False)
    path = tmp_path / "o.jsonl"
    if rows:
        memory.JsonlOutcomeStore(path).write([_outcome() for _ in range(rows)])
    turn = memory.history_turn(memory.JsonlOutcomeStore(path), part_sha256="a" * 64,
                               fingerprints=("f-1",), defect_code="open_shell",
                               mesh_kind="tet", stage="regions", ops=("sew_faces",))
    assert len(turn.get("history", {}).get("exact", [])) == kept


def test_exact_per_turn_is_twelve():
    assert memory.EXACT_PER_TURN == 12
