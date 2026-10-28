# Responsibility: Verify the case guard still refuses a case that would execute code, on a field
# file far too large to hold in memory, instead of refusing the case for being big.
# Boundaries: the guard only. No mesher, no OpenFOAM, no workspace beyond a tmp tree.
#
# WHY THIS FILE EXISTS. OpenFOAM's own tools write field data into constant/ that is one number per
# cell and carries no suffix and no polyMesh/ parent - `cellToRegion`, which checkMesh and
# splitMeshRegions both write - so it fell to the AUTHORED-dictionary size rule and the whole case
# was refused. Measured on mesh-bench 2026-10-28: a 5,686,631-cell conjugate build died on
# "constant/fluid/cellToRegion is larger than the 8388608 byte scan cap - refusing", the solid
# region came back unreadable and the run reported "solid: no cells". Any mesh past about a million
# cells hit it, on every engine.
import pytest

from meshpipeline.sandbox import foam_case_guard as G

HEADER = """FoamFile { version 2.0; format ascii; class labelList; object cellToRegion; }

"""


def _case(tmp_path, body: bytes, name: str = "cellToRegion"):
    d = tmp_path / "constant" / "fluid"
    d.mkdir(parents=True)
    (tmp_path / "system").mkdir()
    (d / name).write_bytes(HEADER.encode() + body)
    return tmp_path


def _big(filler: bytes = b"7\n") -> bytes:
    """Past the authored-dictionary cap, so the chunked path is the one taken."""
    return filler * ((G._MAX_BYTES // len(filler)) + 4096)


def test_a_field_file_past_the_cap_is_scanned_not_refused(tmp_path):
    """The whole point: being big is not a reason to refuse a case."""
    ws = _case(tmp_path, _big())
    assert (ws / "constant" / "fluid" / "cellToRegion").stat().st_size > G._MAX_BYTES
    assert G.scan_case_dicts(ws) is None


def test_a_directive_in_a_big_file_is_still_caught(tmp_path):
    """And it is still a guard. Buried past the first chunk, where a prefix scan would miss it."""
    body = _big() + b'#include "evil"\n' + _big()
    reason = G.scan_case_dicts(_case(tmp_path, body))
    assert reason and "preprocessor directive" in reason


def test_a_library_directive_in_a_big_file_is_still_caught(tmp_path):
    body = _big() + b"libs (\"libmine.so\");\n" + _big()
    reason = G.scan_case_dicts(_case(tmp_path, body))
    assert reason and "libs" in reason


def test_a_directive_straddling_a_chunk_boundary_is_caught(tmp_path):
    """The reason chunks overlap. Placed so the directive spans the read boundary exactly."""
    pad = b"7\n" * ((G._MAX_BYTES - len(HEADER) - 4) // 2)
    body = pad + b'#include "evil"\n' + b"7\n" * 100
    reason = G.scan_case_dicts(_case(tmp_path, body))
    assert reason and "preprocessor directive" in reason


def test_a_big_binary_file_is_still_refused(tmp_path):
    body = _big() + b"\x00\x01\x02" + _big()
    reason = G.scan_case_dicts(_case(tmp_path, body))
    assert reason and "binary" in reason


def test_a_file_past_the_ceiling_is_refused_rather_than_read(tmp_path):
    """The read stays bounded whatever is on disk. Checked without writing 4 GiB."""
    ws = _case(tmp_path, b"7\n")
    f = ws / "constant" / "fluid" / "cellToRegion"

    class _Stat:
        st_size = G._HUGE_BYTES + 1

    real = type(f).stat
    try:
        type(f).stat = lambda self, *a, **k: _Stat()           # noqa: ARG005
        reason = G._scan_one(f, ws)
    finally:
        type(f).stat = real
    assert reason and "ceiling" in reason


def test_a_small_authored_dictionary_is_unchanged(tmp_path):
    """The 8 MiB rule still applies to everything it always applied to."""
    ws = _case(tmp_path, b'#include "evil"\n', name="meshDict")
    reason = G.scan_case_dicts(ws)
    assert reason and "preprocessor directive" in reason


def test_the_ceiling_is_past_anything_this_platform_meshes():
    """A cellToRegion is about ten bytes a cell, so the ceiling has to clear the cell budget."""
    assert G._HUGE_BYTES >= 4 * 1024 ** 3
    assert G._HUGE_BYTES / 10 > 300_000_000, (
        "the ceiling is below a cellToRegion for a 300M-cell mesh, which is inside the range this "
        "platform is being asked to reach")
