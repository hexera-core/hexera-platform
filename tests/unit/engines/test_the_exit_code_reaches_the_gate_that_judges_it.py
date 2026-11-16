"""`rc` and `timed_out` are declared gating bars, so they have to be measurable.

WHAT WAS WRONG. `quality_criteria.BUILD_TIME_KEYS` exempts both keys from
`gate_declared_criteria`, and the comment beside it asserts they "CANNOT be measured after the
build ... the native process has already exited by the time a manifest is written". That is false.
The process has exited; its RECORD has not. `contracts.mesh_execution` keeps every native run of
the attempt in `_NATIVE_RUNS` and publishes it through `native_runs(workspace)`, which outlives
the process by design. Nothing on the manifest path ever asked.

The consequence is that two bars every flow engine DECLARES as gating can never fail. A mesher
that exits non-zero, or one the budget stopped part way, reaches `gate_declared_criteria` with
both keys absent, is skipped by the exemption, and ships.

THE SHAPE OF THE FIX. `finalize` is reached through one fixed contract that carries no native
result (`executor.py` calls it with workspace, patches, engine, domain, internal_flow, params,
topology), and each engine's finalize rebuilds `quality` by re-reading checkMesh output or its own
report from disk. So the values are threaded in at the single place every engine already funnels
through - `engines.manifest.write_manifest` - rather than at seven finalize sites, where the first
one to forget would revert to today's behaviour silently.

OLD MANIFESTS MUST NOT START FAILING. An absent key reads as not-evaluated, and
`gate_declared_criteria` turns not-evaluated into a REFUSAL ("is missing from the quality report").
So simply emptying the exemption would fail every manifest written before this change. The skip is
therefore kept and made conditional on the manifest's own schema version.
"""
from __future__ import annotations

import json

import pytest

from meshpipeline.contracts.mesh_execution import (
    _note_native_run,
    native_runs,
    open_native_record,
)
from meshpipeline.engines.quality_criteria import (
    BUILD_TIME_KEYS,
    criteria_for,
    gate_declared_criteria,
)
from meshpipeline.engines.registry import engine_names


def _engines_that_declare(key: str) -> list[str]:
    """Every engine naming `key` as a GATING bar, discovered rather than hardcoded.

    The chip that filed this said six engines; counting them by hand is exactly the step that
    produced that number, so the test counts them itself.
    """
    out = []
    for name in engine_names():
        try:
            bars = criteria_for(name)
        except Exception:  # noqa: BLE001 - an engine with no spec simply declares nothing
            continue
        if any(c.key == key and c.gating for c in bars):
            out.append(name)
    return out


RC_ENGINES = _engines_that_declare("rc")
TO_ENGINES = _engines_that_declare("timed_out")


def _healthy(engine: str, *, schema: str = "2.2") -> dict:
    """A manifest that clears every OTHER declared bar, so only the key under test can fail it."""
    q: dict = {"fatal": [], "cells": 1000}
    for c in criteria_for(engine):
        if not c.gating or c.key in BUILD_TIME_KEYS:
            continue
        if c.op == "empty":
            q[c.key] = []
        elif c.op == "==":
            q[c.key] = c.threshold
        elif c.op == "<=":
            q[c.key] = c.threshold / 2 if isinstance(c.threshold, float) else c.threshold
        elif c.op == ">":
            q[c.key] = c.threshold + 1 if isinstance(c.threshold, int) else c.threshold * 10 + 1
    q.pop("wall_faces", None)
    if engine == "snappy_multiregion":
        q |= {"regions": [{"name": "fluid", "type": "fluid", "cells": 600},
                          {"name": "solid", "type": "solid", "cells": 400}],
              "regions_missing": [], "regions_undeclared": [], "interface_ok": True}
    return {
        "schema_version": schema, "mesh_mode": engine, "mesh_written": True,
        "cell_count": 1000, "mesh_units": "m", "domain": "d",
        "patch_types": {"body": "wall"}, "patch_face_counts": {"body": 500},
        "flow_topology": "", "quality": q,
    }


# # the bars go live


def test_at_least_one_engine_declares_each_bar():
    """If this fails the rest of the file is vacuous, so it is asserted rather than assumed."""
    assert RC_ENGINES, "no engine declares `rc` as gating; the premise of this file is gone"
    assert TO_ENGINES, "no engine declares `timed_out` as gating"


@pytest.mark.parametrize("engine", RC_ENGINES)
def test_a_mesher_that_exited_non_zero_fails_the_gate(engine):
    m = _healthy(engine)
    m["quality"] |= {"rc": 1, "timed_out": False}
    ok, why = gate_declared_criteria(engine, m)
    assert ok is False, f"{engine}: a mesher that exited 1 passed the quality gate"
    assert "rc" in why or "exit" in why.lower(), f"{engine}: the refusal does not name the bar: {why}"


@pytest.mark.parametrize("engine", TO_ENGINES)
def test_a_build_the_budget_stopped_fails_the_gate(engine):
    m = _healthy(engine)
    m["quality"] |= {"rc": 0, "timed_out": True}
    ok, why = gate_declared_criteria(engine, m)
    assert ok is False, f"{engine}: a build stopped by the budget passed the quality gate"


@pytest.mark.parametrize("engine", sorted(set(RC_ENGINES) | set(TO_ENGINES)))
def test_a_clean_build_still_passes(engine):
    """The negative control: the bar must not be simply always-on."""
    m = _healthy(engine)
    m["quality"] |= {"rc": 0, "timed_out": False}
    ok, why = gate_declared_criteria(engine, m)
    assert ok is True, f"{engine}: a clean build was refused: {why}"


# # the regression guard, which passes today and must keep passing


@pytest.mark.parametrize("engine", sorted(set(RC_ENGINES) | set(TO_ENGINES)))
def test_a_manifest_written_before_this_change_still_passes(engine):
    """An old manifest carries neither key, and must not be read as unmeasured-and-therefore-bad.

    This is the test that justifies keeping BUILD_TIME_KEYS instead of deleting it. Against a fix
    that simply empties the exemption, this fails with the "is missing from the quality report"
    refusal - which is every mesh ever delivered, retroactively failed.
    """
    m = _healthy(engine, schema="2.1")
    m["quality"].pop("rc", None)
    m["quality"].pop("timed_out", None)
    ok, why = gate_declared_criteria(engine, m)
    assert ok is True, f"{engine}: a pre-2.2 manifest was refused for keys it never carried: {why}"


# # the values reach the manifest


def _write(ws, engine="cfmesh_conjugate", quality=None):
    from meshpipeline.engines.manifest import write_manifest
    return write_manifest(
        ws, patch_types={"body": "wall"}, patch_entities={},
        bbox=(0.0, 0.0, 0.0, 1.0, 1.0, 1.0), quality=dict(quality or {"cells": 10, "fatal": []}),
        mesh_units="m", mesh_mode=engine)


def test_the_manifest_carries_the_exit_code_of_the_run_that_built_it(tmp_path):
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": 3, "timed_out": False})
    m = _write(tmp_path)
    assert m["quality"].get("rc") == 3, "the manifest does not carry the mesher's exit code"
    assert m["quality"].get("timed_out") is False
    assert m["schema_version"] == "2.2", "a manifest carrying the build keys says so"


def test_it_takes_the_last_run_that_actually_launched(tmp_path):
    """A refusal before launch is not the build, and the last LAUNCHED run is the one judged.

    This is the same selector the executor classifies failures with, so the manifest and the
    executor can never disagree about which run this was.
    """
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": 0, "timed_out": False})
    _note_native_run(tmp_path, {"rc": -1, "timed_out": True})
    _note_native_run(tmp_path, {"rc": 0, "timed_out": False}, refused=True)
    m = _write(tmp_path)
    assert m["quality"].get("rc") == -1
    assert m["quality"].get("timed_out") is True, (
        "identity, not equality: `0 == False` in Python and a `0` here would read as a clean build")


def test_an_unopened_record_is_unknown_and_not_a_clean_exit(tmp_path):
    """Nothing opened the record, so nothing is known - and the gate must not invent a pass.

    Writing rc 0 here would be the worst outcome of this change: a manifest asserting a clean
    build on no evidence at all.
    """
    assert native_runs(tmp_path) is None
    # every OTHER bar cleared, so only the two keys under test can decide this
    m = _write(tmp_path, quality=dict(_healthy("cfmesh_conjugate")["quality"]))
    assert "rc" not in m["quality"], "an unknown exit code was published as a measurement"
    assert "timed_out" not in m["quality"]
    assert m["schema_version"] == "2.1", "a manifest without the build keys must not claim 2.2"
    ok, why = gate_declared_criteria("cfmesh_conjugate", m)
    assert ok is True, f"such a manifest is not failed for the keys it could not carry: {why}"


def test_an_outage_is_not_stamped_as_the_build_s_verdict(tmp_path):
    """RC_INFRASTRUCTURE is a storage or launch outage, not a judgement on the mesh.

    It comes back as an ordinary result - not a launch refusal - so it survives the
    `refused_before_launch` filter. Stamped as the build's exit it would make the quality floor
    refuse with MESH_QUALITY, which the fallback ladder calls fixable: a blip on a perfectly good
    mesh would be told to the customer as cells too badly shaped to use, and would buy a second
    engine run. The executor does not trust it either - it branches on never_started/uncollected
    and returns an infrastructure result instead.
    """
    from meshpipeline.contracts.mesh_execution import RC_INFRASTRUCTURE

    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": RC_INFRASTRUCTURE, "timed_out": False})
    m = _write(tmp_path, quality=dict(_healthy("cfmesh_conjugate")["quality"]))
    assert "rc" not in m["quality"], "an infrastructure outage was published as the mesher's exit"
    assert m["schema_version"] == "2.1"
    ok, why = gate_declared_criteria("cfmesh_conjugate", m)
    assert ok is True, f"an outage was turned into a mesh-quality failure: {why}"


@pytest.mark.parametrize("flag", ["never_started", "uncollected"])
def test_a_run_that_never_ran_or_never_came_back_is_unknown(tmp_path, flag):
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": -3, "timed_out": False})
    # the recorder derives these from the log tail; set it directly so the test pins the GUARD
    from meshpipeline.contracts.mesh_execution import _NATIVE_RUNS, _key

    _NATIVE_RUNS[_key(tmp_path)][-1][flag] = True
    _NATIVE_RUNS[_key(tmp_path)][-1]["rc"] = 0          # a clean-looking exit on a run that did not
    m = _write(tmp_path, quality=dict(_healthy("cfmesh_conjugate")["quality"]))
    assert "rc" not in m["quality"], f"a run flagged {flag} was published as a completed build"
    assert m["schema_version"] == "2.1"


def test_an_rc_that_was_never_reported_is_not_published(tmp_path):
    """None reads as not-measured at 2.2, and the gate turns not-measured into a refusal.

    So an executor that returned no rc at all would fail an otherwise good mesh. Cheap guard.
    """
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"timed_out": False})      # no rc key at all
    m = _write(tmp_path, quality=dict(_healthy("cfmesh_conjugate")["quality"]))
    assert "rc" not in m["quality"]
    ok, why = gate_declared_criteria("cfmesh_conjugate", m)
    assert ok is True, f"a missing rc failed the mesh closed: {why}"


def test_the_quality_dict_handed_in_is_not_mutated(tmp_path):
    """The caller reuses its own dict after the call (cfmesh finalize reads `fatal` from it)."""
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": 0, "timed_out": False})
    q = {"cells": 10, "fatal": []}
    _write(tmp_path, quality=q)
    assert "rc" not in q, "write_manifest wrote into the caller's quality dict"


def test_the_manifest_on_disk_carries_them_too(tmp_path):
    """Not just the returned dict: the gate reads the file."""
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": 0, "timed_out": True})
    _write(tmp_path)
    on_disk = json.loads((tmp_path / "mesh_manifest.json").read_text())
    assert on_disk["quality"]["timed_out"] is True
    assert on_disk["schema_version"] == "2.2"


def test_a_timed_out_build_is_refused_end_to_end(tmp_path):
    """The whole point, in one test: record a stopped build, write the manifest, hit the gate."""
    open_native_record(tmp_path)
    _note_native_run(tmp_path, {"rc": 0, "timed_out": True})
    m = _write(tmp_path, quality=dict(_healthy("cfmesh_conjugate")["quality"]))
    ok, why = gate_declared_criteria("cfmesh_conjugate", m)
    assert ok is False, (
        "a conjugate build the budget stopped was written to the manifest and still passed the "
        f"quality gate: {why}")
