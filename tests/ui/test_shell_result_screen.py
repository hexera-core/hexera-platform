# Responsibility: Verify SHSTAGE-7 in a real browser: the result screen of a run the user asked to mesh as a shell IS
# the shell - the viewer draws the plates on the mid-surface (one part per mid-face, no volume cells), the key
# numbers are the shell's element count and worst element shape with no "Walls OK", the status line says in one
# plain line it is a mid-surface shell with each element's wall thickness in the file, and nothing on the screen
# speaks of tetrahedra or elements through a wall.
# Boundaries: the shell made here by the real FEA driver and shell run on a small T-joint, finalized and turned into
# the viewer payload by the backend's own code; only the network is stood in for.
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(scope="module")
def shell_payload(tmp_path_factory):
    pytest.importorskip("OCP.BRepPrimAPI")
    pytest.importorskip("gmsh")
    from tests import shell_fixtures as sf
    from tests.unit.engines.test_shell_delivery import _drive, _stage

    from meshpipeline.application.viewer_payload import build_viewer_payload
    from meshpipeline.engines import shell_run
    from meshpipeline.engines.gmsh.gmsh_runner import finalize

    mp = pytest.MonkeyPatch()
    try:
        ws = _stage(sf.t_joint(), tmp_path_factory.mktemp("shell_result") / "ws")
        ok, _m, calls = _drive(ws, mp, {"purpose": "structural", "fea_mesh_as": "shell"})
    finally:
        mp.undo()
    assert ok and calls == []
    assert finalize(str(ws), [], "gmsh", domain="structural FEA")["success"]
    return build_viewer_payload(ws)["surface"], shell_run.read(ws)


def _open(live, job: str, surf: dict) -> None:
    live.evaluate("""(async () => {
      const PAYLOAD = __P__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        const json = (b) => Promise.resolve(new Response(JSON.stringify(b), {status: 200, headers: {'Content-Type': 'application/json'}}));
        if (s.includes('/simulation/__J__/surface')) return json(PAYLOAD);
        if (s.includes('/client-config')) return json({});
        return real(u, o);
      };
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__J__', document.getElementById('stage'), {metrics: {pass: true}, files: [], notes: [],
        createdAt: '2026-11-07T10:00:00Z', endedAt: '2026-11-07T10:02:10Z'});
      document.getElementById('viewer-__J__').scrollIntoView();
    })()""".replace("__P__", json.dumps(surf)).replace("__J__", job), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{job}:tools'] && document.querySelector('#v-keys-{job} dl')",
                  timeout=90, what="the result screen")


def test_the_result_screen_of_a_shell_run_is_the_shell(live, shell_payload):
    surf, rec = shell_payload
    assert surf["is_mesh"] and surf["fea"]["title"] == "FEA shell mesh"
    names = [p["name"] for p in surf["patches"]]
    # THE PARTS BY NAME (this file's bodies have none: Part_k), never the mid-faces' ids or an empty "free" group
    assert names and all(n.startswith("Part_") for n in names)
    assert not any(str(p.get("name")) == "free" for p in surf.get("parts") or [])
    # every plate drawn as itself: a quad one polygon, never two triangles
    assert sum(p["face_count"] for p in surf["patches"]) == rec["quads"] + rec["trias"]
    assert not any("positions_b64" in p for p in surf["patches"])
    job = "shellres"
    _open(live, job, surf)
    drawn = live.evaluate(f"""(() => ({{shown: window._vdbg['{job}:tools'].visible().slice().sort(),
      cells: document.querySelector('#viewer-{job} .v-count').textContent}}))()""")
    assert drawn["shown"] == sorted(names), drawn
    assert f"{rec['elements']:,} cells" in drawn["cells"], drawn
    got = live.evaluate(f"""(() => {{
      const bar = document.querySelector('#viewer-{job} .v-bar');
      return {{keys: [...bar.querySelectorAll('.rp-keys dt')].map(d => d.textContent),
               vals: [...bar.querySelectorAll('.rp-keys dd')].map(d => d.textContent),
               status: document.getElementById('v-status-{job}').textContent,
               verdict: bar.querySelector('.rp-verdict').innerText.trim(),
               text: document.getElementById('viewer-{job}').innerText}}; }})()""")
    assert got["keys"] == ["Elements", "Worst quality", "Time"], got
    assert got["status"].startswith("your delivered FEA mesh · A mid-surface shell: ")
    assert "each element carries its wall thickness in the file" in got["status"]
    assert f"{rec['elements']:,} plate elements" in got["status"]
    assert got["verdict"].startswith("Passed"), got
    low = got["text"].lower()
    assert "tetra" not in low and "through the thinnest wall" not in low and "walls ok" not in low
    assert_clean(live, "a shell run's result screen")
