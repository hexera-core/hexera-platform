# The same ECXML, read twice, told two different stories.
#
# MEASURED, 2026-11-25, on a four-block file: `ecxml_place`'s round-off report lists the gaps it
# closed, sorted on the gap size alone. Four 1 mm blocks whose faces sit 50 nm apart on one plane
# make four pairs all exactly 4.99999999999199e-08 apart, so all four tie - and the list they are
# sorted from is a SET OF TUPLES HOLDING NAMES, which iterates in str-hash order. Eight runs of the
# same bytes gave eight different orders. Only the first four survive the slice, so the order
# decides WHICH gaps are reported, and that list is written into the stored `.ecxml.json` sidecar
# and into the physics brief the intake agent reads: a different record and a different prompt on
# every run, from one file. The mesh never moved - the placed STL was byte-identical every time.
#
# It was pinned in production only by `ENV PYTHONHASHSEED=0` in the Dockerfile's pipeline stage.
# The api stage (api, beat, worker-utility) does not set it and was measurably unpinned. Pinning it
# there is the wrong fix: on the API hash randomisation is a DoS mitigation on untrusted input. A
# total sort key needs no seed, so that is what these tests hold.
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from meshpipeline.cad.ingest import ecxml
from meshpipeline.cad.ingest.ecxml import read_ecxml
from meshpipeline.cad.ingest.ecxml_place import place, roundoff_lines

#: The gap four tied pairs come out at, to the last bit, on the fixture below.
TIED_GAP_M = 4.99999999999199e-08

_BLOCK = """                <solid3dBlock>
                    <name>{name}</name>
                    <active>true</active>
                    <location>
                        <x>{x:.8f}</x>
                        <y>0.00000000</y>
                        <z>{z:.8f}</z>
                    </location>
                    <size>
                        <x>0.00100000</x>
                        <y>0.00100000</y>
                        <z>0.00100000</z>
                    </size>
                    <material>copper</material>
                    <powerDissipation>0</powerDissipation>
                </solid3dBlock>
"""

_HEAD = """<?xml version="1.0" encoding="utf-8"?>
<neutralXML>
    <name>tied round-off steps</name>
    <producer>Icepak</producer>
    <materials>
        <material>
            <name>copper</name>
            <density>8933</density>
            <specific_heat>385</specific_heat>
            <surfaceEmissivity>0</surfaceEmissivity>
            <thermalConductivity>
                <isotropic>
                    <conductivity>387.6</conductivity>
                </isotropic>
            </thermalConductivity>
        </material>
    </materials>
    <geometry>
        <assembly>
            <name>TIE</name>
            <active>true</active>
            <geometry>
"""

_TAIL = """            </geometry>
        </assembly>
    </geometry>
</neutralXML>
"""


def _fixture_text():
    """Two blocks whose TOP face is at z=0.00100000 and two whose BOTTOM face is at 0.00100005.

    Every number is written to eight decimals, which is what a thermal tool with a fixed print
    format exports, and the 50 nm step is round-off rather than a real gap. That makes one plane
    cluster carrying four (high, low) pairs with an identical gap - the tie.
    """
    blocks = "".join([
        _BLOCK.format(name="A1", x=0.0, z=0.0),
        _BLOCK.format(name="A2", x=0.002, z=0.0),
        _BLOCK.format(name="B1", x=0.0, z=0.00100005),
        _BLOCK.format(name="B2", x=0.002, z=0.00100005),
    ])
    return _HEAD + blocks + _TAIL


def _gaps(placement):
    return [(g["between"][0], g["between"][1], g["gap_m"])
            for m in placement.roundoff for g in m["gaps_closed"]]


@pytest.fixture
def tie_file(tmp_path):
    p = tmp_path / "tie.ecxml"
    p.write_text(_fixture_text(), encoding="utf-8")
    return p


def test_the_fixture_really_does_tie(tie_file):
    # Without this, every test below could pass on an empty list.
    gaps = _gaps(place(read_ecxml(tie_file)))
    assert len(gaps) == 4, gaps
    assert {g for _a, _b, g in gaps} == {TIED_GAP_M}, gaps


_CHILD = (
    "import json, sys\n"
    "from meshpipeline.cad.ingest.ecxml import read_ecxml\n"
    "from meshpipeline.cad.ingest.ecxml_place import place\n"
    "pl = place(read_ecxml(sys.argv[1]))\n"
    "print(json.dumps([[g['between'][0], g['between'][1], g['gap_m']]\n"
    "                  for m in pl.roundoff for g in m['gaps_closed']]))\n"
)


def _child(tie_file, seed):
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    # NOT -I and NOT -E: both DISCARD PYTHONHASHSEED, so the child would run on a random seed and
    # the comparison below would measure nothing. Measured: PYTHONHASHSEED=0 with -I prints a
    # different hash of the same string on every run, with sys.flags.hash_randomization still 1.
    pkg_parent = str(Path(ecxml.__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join([pkg_parent, env.get("PYTHONPATH", "")]).strip(os.pathsep)
    out = subprocess.run([sys.executable, "-c", _CHILD, str(tie_file)],
                         capture_output=True, text=True, env=env, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_closed_gap_order_is_the_same_under_every_hash_seed(tie_file):
    seeds = ["0", "1", "42", "1000", "12345", "random"]
    runs = [_child(tie_file, s) for s in seeds]
    assert len(runs[0]) == 4, runs[0]
    for seed, got in zip(seeds[1:], runs[1:], strict=True):
        assert got == runs[0], f"seed {seed} reported a different order:\n{got}\n{runs[0]}"


def test_the_order_does_not_depend_on_which_order_the_pairs_arrive_in():
    # The property a total key has and -r[2] has not. Python's sort is stable, so with a key that
    # cannot separate the four ties, whatever order the set happened to yield SURVIVES.
    rows = [("TIE/A1", "TIE/B1", TIED_GAP_M), ("TIE/A1", "TIE/B2", TIED_GAP_M),
            ("TIE/A2", "TIE/B1", TIED_GAP_M), ("TIE/A2", "TIE/B2", TIED_GAP_M)]
    total = {tuple(sorted(p, key=lambda r: (-r[2], r[0], r[1])))
             for p in itertools.permutations(rows)}
    assert len(total) == 1, "the key leaves the arrival order showing"
    gap_only = {tuple(sorted(p, key=lambda r: -r[2])) for p in itertools.permutations(rows)}
    assert len(gap_only) == 24, (
        "this test would prove nothing: the gap-only key is already total on these rows")


def test_the_widest_gap_still_comes_first():
    # The key's first job is unchanged: widest gap first, because only the first four are kept.
    rows = [("a", "b", 1e-08), ("c", "d", 9e-08), ("e", "f", 5e-08)]
    assert [r[2] for r in sorted(rows, key=lambda r: (-r[2], r[0], r[1]))] == [9e-08, 5e-08, 1e-08]


def test_the_report_the_user_reads_is_ordered_too(tie_file):
    # roundoff_lines sorts the gaps again, on the gap alone - which is safe ONLY because the list
    # it is given is now ordered and Python's sort is stable. This pins that pairing.
    line = " ".join(roundoff_lines(place(read_ecxml(tie_file)).roundoff))
    assert "TIE/A1 and TIE/B1" in line, line
    assert line.index("TIE/A1 and TIE/B1") < line.index("TIE/A2 and TIE/B2"), line


def test_the_canonical_tag_map_has_no_case_only_collision():
    # The map is {t.lower(): t for t in sorted(SCHEMA_TAGS)}. sorted() makes the winner the same
    # every run; THIS test is what keeps there being no contest. Tags are matched
    # case-insensitively because vendors spell them differently (solid2DBlock, rectangular2dfan),
    # so two tags differing only in case would silently make one of them unreadable.
    lowered = [t.lower() for t in ecxml.SCHEMA_TAGS]
    dupes = {k for k in lowered if lowered.count(k) > 1}
    assert not dupes, f"these tags differ only in case: {sorted(dupes)}"
    assert len(ecxml._CANONICAL_TAG) == len(set(ecxml.SCHEMA_TAGS))
