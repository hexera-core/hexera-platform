# Responsibility: Verify engine, purpose and category come from declarations rather than being guessed from prose.
from __future__ import annotations

from pathlib import Path

from meshpipeline.application.maintenance.export import _derive_object_category  # noqa: E402
from meshpipeline.capture.source import events_to_state  # noqa: E402


class _Ev:
    def __init__(self, event_type, payload, attempt=0):
        self.event_type, self.payload, self.attempt = event_type, payload, attempt


# engine

def test_engine_is_reconstructed_from_the_engine_select_event():
    s = events_to_state([_Ev("engine_select_run", {"chosen": "snappy", "source": "user"})], {})
    assert s["engine"] == "snappy"


def test_engine_falls_back_to_the_users_intake_pin():
    s = events_to_state([_Ev("intake_complete", {"mesh_engine": "vmtk"})], {})
    assert s["engine"] == "vmtk"


def test_engine_select_wins_over_the_intake_pin():
    s = events_to_state([
        _Ev("intake_complete", {"mesh_engine": "cfmesh"}),
        _Ev("engine_select_run", {"chosen": "snappy", "source": "topology_internal"}),
    ], {})
    assert s["engine"] == "snappy"


def test_engine_is_never_silently_blank_when_an_engine_ran():
    s = events_to_state([_Ev("engine_select_run", {"chosen": "snappy", "source": "user"})], {})
    assert s["engine"], "an engine ran; the corpus must say which"
    assert (s.get("engine") or "unknown") != "unknown"


def test_engine_empty_only_when_nothing_declared_one():
    assert events_to_state([], {})["engine"] == ""


# purpose / input_kind

def test_purpose_and_input_kind_reach_the_corpus():
    s = events_to_state([_Ev("intake_complete", {
        "purpose": "internal_cfd", "input_kind": "body-surface", "mesh_engine": "snappy",
    })], {})
    assert s["purpose"] == "internal_cfd"
    assert s["input_kind"] == "body-surface"


def test_purpose_and_input_kind_fall_back_to_operational_state():
    s = events_to_state([], {"purpose": "structural", "input_kind": "solid-body"})
    assert s["purpose"] == "structural"
    assert s["input_kind"] == "solid-body"


def test_intake_event_payload_declares_purpose_and_input_kind():
    import ast
    src = (Path(__file__).parent.parent.parent.parent
           # the intake_complete payload is built by turn.completed_patch, which owns the
           # graph patches; the node returns one of them and constructs nothing itself
           / "src" / "meshpipeline" / "agents" / "intake" / "turn.py").read_text()
    tree = ast.parse(src)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            ks = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "intake_system_snapshot" in ks:   # the intake_complete payload dict
                keys = set(ks)
    assert {"purpose", "input_kind"} <= keys, (
        f"intake_complete payload is missing purpose/input_kind; has {sorted(keys)}")


# object_category

def test_carve_does_not_make_a_pipe_elbow_a_ground_vehicle():
    assert _derive_object_category(
        "input.step", "90-degree pipe elbow internal flow") == "piping"


def test_cartesian_does_not_make_anything_a_ground_vehicle():
    assert _derive_object_category("input.step", "cartesianMesh duct study") != "ground_vehicle"


def test_whole_words_only_no_substring_bleed():
    for word, wrong in (("carve", "ground_vehicle"), ("carbon", "ground_vehicle"),
                        ("swinging", "airfoil"), ("shipment", "marine_vessel"),
                        ("hulled", "marine_vessel"), ("pipeline", "piping")):
        cat = _derive_object_category("input.step", f"a {word} case")
        assert cat != wrong, f"{word!r} substring-matched into {cat!r}"


def test_plurals_still_match():
    assert _derive_object_category("x.step", "gas turbine blades") == "turbomachinery"
    assert _derive_object_category("x.step", "delta wings study") == "airfoil"
    assert _derive_object_category("x.step", "cooling ducts") == "piping"


def test_real_categories_still_match():
    assert _derive_object_category("Naca0012.STEP", "NACA 0012 airfoil external aero") == "airfoil"
    assert _derive_object_category("x.step", "sedan external aerodynamics") == "ground_vehicle"
    assert _derive_object_category("x.step", "gas turbine blade cooling") == "turbomachinery"
    assert _derive_object_category("model.vtp", "blood vessel lumen hemodynamics") == "vascular"
    assert _derive_object_category("DPW4-CRM.stp", "NASA CRM wing-body cruise drag") == "airfoil"


def test_filename_alone_still_classifies():
    assert _derive_object_category("Naca0012.STEP", "") == "airfoil"


def test_unknown_when_nothing_matches():
    assert _derive_object_category("input.step", "a thing") == "unknown"


def test_category_reads_the_declared_domain_not_the_request_prose():
    prose_that_would_misfire = (
        "snappyHexMesh will carve the cavity; cartesianMesh was rejected"
    )
    # passed as `domain`, the jargon still must not yield ground_vehicle
    assert _derive_object_category("input.step", prose_that_would_misfire) != "ground_vehicle"
