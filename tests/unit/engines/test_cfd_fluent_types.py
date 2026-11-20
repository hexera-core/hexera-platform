# Responsibility: Verify that the Fluent mesh carries the boundary types the user confirmed - inlet with a speed a velocity-inlet, a mass flow a mass-flow-inlet, outlet a pressure-outlet, wall a wall - in both places the file states them (the zone section's word and the face section's bc code), from the confirmed role never the name; that an unconfirmed role is left and said; and that the roles travel to the mesher on snappyHexMesh, cfMesh and VMTK.
# Boundaries: pure over temporary files; no converter is run.
from __future__ import annotations

import json
import re
import zipfile

from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_exports as CE

# what foamMeshToFluent writes (the mesh image, 20 Nov): every non-wall zone "pressure-outlet" in its zone section and
# bc code 4 (pressure-inlet) in its face section; ids hex in the face headers, decimal in the zone sections
BODY = (b'(0 "foamMeshToFluent")\n(2 3)\n(10 (0 1 8 0 3))\n(13 (0 1 20 0 0))\n(12 (0 1 4 0 0))\n'
        b"(13 (2 1 4 2 0)(\n1 2 3 4 1 2\n))\n"
        b"(13 (a 5 6 4 0)(\n1 2 3 4 1 0\n))\n"
        b"(13 (b 7 8 4 0)(\n1 2 3 4 1 0\n))\n"
        b"(13 (c 9 20 3 0)(\n1 2 3 4 1 0\n))\n")
NAMES = (b"(39 (1 fluid fluid-1)())\n(39 (2 interior interior-1)())\n(39 (10 pressure-outlet inlet)())\n"
         b"(39 (11 pressure-outlet outlet)())\n(39 (12 wall wall)())\n")


def _ws(tmp_path, ports, speed="1 m/s"):
    (tmp_path / CE.PORTS_FILE).write_text(json.dumps(ports))
    (tmp_path / CE.REQUEST_FILE).write_text(
        "Internal flow through the pipe, confirmed by the user on the geometry stage. "
        + FC.sentence("water at 20 °C", speed, "functions", internal=True, patches=ports))
    return tmp_path


def _write(tmp_path, types):
    msh = tmp_path / "mesh.msh"
    msh.write_bytes(BODY + NAMES)
    _n, _renamed, written = CE._zip_fluent(msh, tmp_path / "f.zip", "mesh.msh", types=types)
    with zipfile.ZipFile(tmp_path / "f.zip") as zf:
        return zf.read("mesh.msh").decode(), written


def _agree(text: str) -> dict[str, tuple[str, int]]:
    """{zone name: (zone-section type, face-section code)} - the two places a Fluent file states a zone's type."""
    codes = {int(m.group(1), 16): int(m.group(2), 16)
             for m in re.finditer(r"(?m)^\(13 \(([0-9a-f]+) [0-9a-f]+ [0-9a-f]+ ([0-9a-f]+) [0-9a-f]+\)\(", text)}
    out = {}
    for m in re.finditer(r"\(39 \((\d+) (\S+) (\S+)\)\(\)\)", text):
        zid, kind, name = int(m.group(1)), m.group(2), m.group(3)
        if zid in codes and kind not in ("fluid", "interior"):
            out[name] = (kind, codes[zid])
    return out


def test_the_confirmed_roles_are_written_in_both_places_and_agree(tmp_path):
    ports = [{"name": "inlet", "type": "inlet", "diameter_mm": 50}, {"name": "outlet", "type": "outlet"},
             {"name": "wall", "type": "wall"}]
    types, left = CE.fluent_types(_ws(tmp_path, ports))
    assert types == {"inlet": "velocity-inlet", "outlet": "pressure-outlet", "wall": "wall"} and left == {}
    text, written = _write(tmp_path, types)
    got = _agree(text)
    assert got == {"inlet": ("velocity-inlet", 10), "outlet": ("pressure-outlet", 5), "wall": ("wall", 3)}
    assert all(CE.FLUENT_CODES[k] == c for k, c in got.values())          # the file agrees with itself
    assert written == types
    # the face data and the counts are untouched
    assert "1 2 3 4 1 0" in text and "(10 (0 1 8 0 3))" in text and "(13 (2 1 4 2 0)(" in text


def test_a_patch_named_inlet_confirmed_as_an_outlet_keeps_the_confirmed_role(tmp_path):
    ports = [{"name": "inlet", "type": "outlet"}, {"name": "outlet", "type": "inlet", "diameter_mm": 50},
             {"name": "wall", "type": "wall"}]
    types, _left = CE.fluent_types(_ws(tmp_path, ports))
    assert types["inlet"] == "pressure-outlet" and types["outlet"] == "velocity-inlet"
    got = _agree(_write(tmp_path, types)[0])
    assert got["inlet"] == ("pressure-outlet", 5) and got["outlet"] == ("velocity-inlet", 10)


def test_a_mass_flow_inlet_and_an_unstated_flow(tmp_path):
    ports = [{"name": "inlet", "type": "inlet", "diameter_mm": 50}, {"name": "outlet", "type": "outlet"}]
    types, _left = CE.fluent_types(_ws(tmp_path, ports, speed="2 kg/s"))
    assert types["inlet"] == "mass-flow-inlet"
    assert _agree(_write(tmp_path, types)[0])["inlet"] == ("mass-flow-inlet", 20)
    # no conditions sentence: the inlet's kind is not confirmed - left as the converter wrote it, said
    (tmp_path / CE.REQUEST_FILE).write_text("Internal CFD mesh of the pipe.")
    types, left = CE.fluent_types(tmp_path)
    assert "inlet" not in types and "do not state" in left["inlet"] and types["outlet"] == "pressure-outlet"
    # a far field has no confirmed Fluent type: left, said
    (tmp_path / CE.PORTS_FILE).write_text(json.dumps([{"name": "farfield", "type": "farfield"}]))
    assert "farfield" in CE.fluent_types(tmp_path)[1]


def test_no_declaration_leaves_the_file_as_the_converter_wrote_it(tmp_path):
    assert CE.fluent_types(tmp_path) == ({}, {})
    text, written = _write(tmp_path, {})
    assert text.encode() == BODY + NAMES and written == {}


def test_the_roles_travel_to_the_mesher_on_every_flow_engine(tmp_path):
    from meshpipeline.engines.snappy.drivers import REMOTE_WORKSPACE_FACTS
    from meshpipeline.engines.vmtk import vmtk_runner as V
    assert {"port_declaration.json", "request.txt"} <= set(REMOTE_WORKSPACE_FACTS)
    for n in ("vmtk_spec.json", "lumen.vtp", "port_declaration.json", "request.txt"):
        (tmp_path / n).write_text("x")
    assert {"port_declaration.json", "request.txt"} <= set(V._native_payload_members(tmp_path))
    # cfMesh declares no payload: its whole workspace travels (adapters/mesh_execution/cloud_run_client._tar_dir)
    from meshpipeline.contracts.mesh_execution import submission_payload_files
    assert submission_payload_files(tmp_path) is None
