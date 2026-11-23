#!/usr/bin/env python3
"""Write a lab of real .ecxml files.

Three groups, because "works with any file" means three different things:

  shipped/   the sample models the reader's own tests use - a vented set-top box, a ducted board,
             a tiny board, a wirebond package with and without a cap gap.
  awkward/   valid files that are hard: all sixteen geometry elements at once, overlapping boxes
             under both producers' precedence rules, four levels of assembly, inactive objects,
             float32 noise padded to fifteen places, a ten-nanometre film beside a ten-millimetre
             block, faces that touch exactly, a 2D object on each of the six planes, non-ASCII
             names, and a file with no geometry at all.
  hostile/   files that must be REFUSED in words: a DTD, an internal entity, an external entity,
             three hundred levels of nesting, a zero-size box, a material that was never declared.

A refusal is a pass. A traceback is not.
"""
from __future__ import annotations

import os
import pathlib as _pl

#: The repository this lab is part of, and the lab directory beside it. HEXERA_ECXML_LAB moves the
#: lab; the default sits under the repository so two checkouts do not share one.
REPO = _pl.Path(os.environ.get("HEXERA_REPO") or
                _pl.Path(__file__).resolve().parents[3])
LAB_ROOT = _pl.Path(os.environ.get("HEXERA_ECXML_LAB") or (REPO / ".ecxml_lab"))

import pathlib
import sys

ROOT = REPO
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "unit" / "cad"))

import ecxml_models as M  # noqa: E402
from ecxml_models import Ecxml, standard_materials  # noqa: E402

LAB = LAB_ROOT / "models"
for sub in ("shipped", "awkward", "hostile"):
    (LAB / sub).mkdir(parents=True, exist_ok=True)

MM = 1e-03


def put(group: str, name: str, data) -> None:
    body = data.xml() if hasattr(data, "xml") else data
    if isinstance(body, str):
        body = body.encode("utf-8")
    p = LAB / group / f"{name}.ecxml"
    p.write_bytes(body)
    print(f"{group:8s} {p.stat().st_size:>9,} B  {name}")


# ================================================================== shipped =====================
for n, fn in sorted(M.MODELS.items()):
    put("shipped", n, fn())
put("shipped", "wirebond", M.wirebond_package())
put("shipped", "wirebond_gap", M.wirebond_package(cap_gap_m=50e-06))


# ================================================================== awkward =====================
def everything(producer: str = "FloTHERM") -> Ecxml:
    """All sixteen geometry elements in one file, inside a domain big enough to hold them."""
    d = standard_materials(Ecxml("every element", producer))
    d.domain((0, 0, 0), (200 * MM, 120 * MM, 80 * MM))
    d.enclosure("case", (0, 0, 0), (200 * MM, 120 * MM, 80 * MM), "Steel sheet", 1.0 * MM)
    d.pcb("board", (10 * MM, 10 * MM, 20 * MM), (180 * MM, 100 * MM, 1.6 * MM), "+xy",
          "FR4 board")
    d.block("die", (40 * MM, 40 * MM, 20 * MM), (10 * MM, 10 * MM, 0.5 * MM), "Silicon", 12.0)
    d.plate("lid", (38 * MM, 38 * MM, 22 * MM), (14 * MM, 14 * MM, 0), "+xy", "Aluminium 6063", 0.0)
    d.cylinder("post", (120 * MM, 60 * MM, 20 * MM), (8 * MM, 8 * MM, 30 * MM), "+xy",
               "Aluminium 6063", 0.0)
    d.source("hot air", (150 * MM, 20 * MM, 40 * MM), (10 * MM, 10 * MM, 10 * MM), 3.0)
    d.source2d("hot face", (150 * MM, 80 * MM, 40 * MM), (10 * MM, 10 * MM, 0), "+xy", 1.5)
    d.grille("vent", (0, 20 * MM, 20 * MM), (0, 80 * MM, 40 * MM), "-yz", 2.5, 0.45)
    d.resistance("filter", (180 * MM, 20 * MM, 20 * MM),
                 (10 * MM, 80 * MM, 40 * MM), (1.0, 0.2, 0.2), (0.5, 0.9, 0.9))
    d.heatsink("sink", [("base", (36 * MM, 36 * MM, 22.5 * MM), (18 * MM, 18 * MM, 2 * MM),
                         "Aluminium 6063", 0.0)]
               + [(f"fin {i}", (36 * MM + i * 3 * MM, 36 * MM, 24.5 * MM),
                   (1.2 * MM, 18 * MM, 12 * MM), "Aluminium 6063", 0.0) for i in range(6)])
    d.two_resistor("cpu", (80 * MM, 40 * MM, 20 * MM), (12 * MM, 12 * MM, 1.5 * MM), "+xy",
                   8.0, 0.35, 4.2)
    d.fan2d("exhaust", (200 * MM, 30 * MM, 30 * MM), (0, 60 * MM, 40 * MM), "+yz", 0.004)
    d.fan3d("intake", (100 * MM, 0, 30 * MM), (60 * MM, 15 * MM, 40 * MM), "-xz", 12 * MM,
            ((0.0, 60.0), (0.004, 30.0), (0.008, 0.0)))
    d.monitor("probe", (100 * MM, 60 * MM, 40 * MM))
    d.mcad("bracket", (160 * MM, 90 * MM, 20 * MM), "bracket.step", "Aluminium 6063", 0.0)
    return d


put("awkward", "everything", everything())
put("awkward", "everything_icepak", everything("Icepak"))


def overlap_chain(producer: str) -> Ecxml:
    """Five blocks each overlapping the last by half. Flotherm resolves later-wins, Icepak
    embedded-solid-wins, so the SAME file is a different model under each producer."""
    d = standard_materials(Ecxml(f"overlap {producer}", producer))
    d.domain((0, 0, 0), (60 * MM, 20 * MM, 20 * MM))
    for i in range(5):
        d.block(f"b{i}", (i * 5 * MM, 5 * MM, 5 * MM), (10 * MM, 10 * MM, 10 * MM),
                ("Silicon", "Aluminium 6063")[i % 2], float(i))
    return d


put("awkward", "overlap_flotherm", overlap_chain("FloTHERM"))
put("awkward", "overlap_icepak", overlap_chain("Icepak"))


def nested() -> Ecxml:
    """Four levels of assembly. An assembly only groups, so the model must be identical to the
    same blocks written flat - which is a determinism claim the harness checks."""
    d = standard_materials(Ecxml("nested", "FloTHERM"))
    d.domain((0, 0, 0), (40 * MM, 40 * MM, 40 * MM))
    with d.assembly("L1"):
        d.block("a", (2 * MM, 2 * MM, 2 * MM), (4 * MM, 4 * MM, 4 * MM), "Silicon", 1.0)
        with d.assembly("L2"):
            d.block("b", (10 * MM, 2 * MM, 2 * MM), (4 * MM, 4 * MM, 4 * MM), "Silicon", 2.0)
            with d.assembly("L3"):
                d.block("c", (18 * MM, 2 * MM, 2 * MM), (4 * MM, 4 * MM, 4 * MM), "Silicon", 3.0)
                with d.assembly("L4"):
                    d.block("d", (26 * MM, 2 * MM, 2 * MM), (4 * MM, 4 * MM, 4 * MM),
                            "Silicon", 4.0)
    return d


def flat() -> Ecxml:
    d = standard_materials(Ecxml("nested", "FloTHERM"))
    d.domain((0, 0, 0), (40 * MM, 40 * MM, 40 * MM))
    for i, n in enumerate("abcd"):
        d.block(n, (2 * MM + i * 8 * MM, 2 * MM, 2 * MM), (4 * MM, 4 * MM, 4 * MM),
                "Silicon", float(i + 1))
    return d


put("awkward", "nested_assembly", nested())
put("awkward", "nested_flat", flat())


def inactive() -> Ecxml:
    """An inactive assembly switches off everything inside it (5.1), an inactive object only
    itself. Both are written here so a reader that honours one and not the other shows up."""
    d = standard_materials(Ecxml("inactive", "FloTHERM"))
    d.domain((0, 0, 0), (40 * MM, 20 * MM, 20 * MM))
    d.block("on", (2 * MM, 2 * MM, 2 * MM), (6 * MM, 6 * MM, 6 * MM), "Silicon", 1.0)
    d.block("off", (12 * MM, 2 * MM, 2 * MM), (6 * MM, 6 * MM, 6 * MM), "Silicon", 1.0,
            active=False)
    with d.assembly("off group", active=False):
        d.block("inside off", (22 * MM, 2 * MM, 2 * MM), (6 * MM, 6 * MM, 6 * MM), "Silicon", 1.0)
    return d


put("awkward", "inactive", inactive())


def noisy() -> Ecxml:
    """What a real export looks like: float32 values padded to fifteen decimal places. The reader
    has a print-limit estimator for exactly this, and it must not move a single face."""
    import struct

    def f32(x: float) -> float:
        return struct.unpack("<f", struct.pack("<f", x))[0]

    d = standard_materials(Ecxml("noisy", "FloTHERM", places=15))
    d.domain((0, 0, 0), (f32(40 * MM), f32(20 * MM), f32(20 * MM)))
    for i in range(6):
        d.block(f"b{i}", (f32(2 * MM + i * 6 * MM), f32(2 * MM), f32(2 * MM)),
                (f32(5 * MM), f32(5 * MM), f32(5 * MM)), "Silicon", 1.0)
    return d


put("awkward", "noisy_float32", noisy())


def spans() -> Ecxml:
    """A ten-nanometre film beside a ten-millimetre block: six decades of length in one file."""
    d = standard_materials(Ecxml("spans", "FloTHERM"))
    d.domain((0, 0, 0), (30 * MM, 20 * MM, 20 * MM))
    d.block("big", (2 * MM, 2 * MM, 2 * MM), (10 * MM, 10 * MM, 10 * MM), "Aluminium 6063", 0.0)
    d.block("film", (14 * MM, 2 * MM, 2 * MM), (10 * MM, 10 * MM, 10e-09), "Silicon", 0.1)
    d.block("thin", (14 * MM, 2 * MM, 6 * MM), (10 * MM, 10 * MM, 1e-06), "Silicon", 0.1)
    return d


put("awkward", "spans_six_decades", spans())


def touching() -> Ecxml:
    """Faces that coincide exactly. A snap grid must put one plane there, not two."""
    d = standard_materials(Ecxml("touching", "FloTHERM"))
    d.domain((0, 0, 0), (40 * MM, 20 * MM, 20 * MM))
    for i in range(4):
        d.block(f"b{i}", (i * 8 * MM, 4 * MM, 4 * MM), (8 * MM, 8 * MM, 8 * MM),
                ("Silicon", "Aluminium 6063")[i % 2], 1.0)
    return d


put("awkward", "touching_faces", touching())


def planes() -> Ecxml:
    """A 2D plate on each of the six planes. The sign is the direction it points (4.2.4)."""
    d = standard_materials(Ecxml("planes", "FloTHERM"))
    d.domain((0, 0, 0), (60 * MM, 60 * MM, 60 * MM))
    spec = [("+xy", (5 * MM, 5 * MM, 10 * MM), (10 * MM, 10 * MM, 0)),
            ("-xy", (20 * MM, 5 * MM, 10 * MM), (10 * MM, 10 * MM, 0)),
            ("+yz", (5 * MM, 20 * MM, 10 * MM), (0, 10 * MM, 10 * MM)),
            ("-yz", (20 * MM, 20 * MM, 10 * MM), (0, 10 * MM, 10 * MM)),
            ("+xz", (5 * MM, 35 * MM, 10 * MM), (10 * MM, 0, 10 * MM)),
            ("-xz", (20 * MM, 35 * MM, 10 * MM), (10 * MM, 0, 10 * MM))]
    for pl, loc, sz in spec:
        d.plate(f"plate {pl}", loc, sz, pl, "Aluminium 6063", 0.0)
    return d


put("awkward", "all_six_planes", planes())


def unicode_names() -> Ecxml:
    """Names a real export carries: accents, CJK, and the three characters XML must escape."""
    d = standard_materials(Ecxml("Résumé <&> 温度", "FloTHERM"))
    d.domain((0, 0, 0), (30 * MM, 20 * MM, 20 * MM))
    d.block("Kühlkörper & <Basis>", (2 * MM, 2 * MM, 2 * MM), (8 * MM, 8 * MM, 8 * MM),
            "Aluminium 6063", 0.0)
    d.block("チップ", (14 * MM, 2 * MM, 2 * MM), (8 * MM, 8 * MM, 8 * MM), "Silicon", 5.0)
    return d


put("awkward", "unicode_names", unicode_names())

d = Ecxml("empty", "FloTHERM")
d.domain((0, 0, 0), (10 * MM, 10 * MM, 10 * MM))
put("awkward", "empty_geometry", d)

d = standard_materials(Ecxml("no domain", "FloTHERM"))
d.block("solo", (0, 0, 0), (8 * MM, 8 * MM, 8 * MM), "Silicon", 1.0)
put("awkward", "no_domain", d)


# ================================================================== hostile =====================
HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
BODY = ("<name>x</name><producer>FloTHERM</producer>"
        "<solutionDomain><location><x>0.0</x><y>0.0</y><z>0.0</z></location>"
        "<size><x>0.01</x><y>0.01</y><z>0.01</z></size></solutionDomain><geometry/>")

put("hostile", "dtd_internal_entity",
    HEAD + '<!DOCTYPE neutralXML [ <!ENTITY a "AAAAAAAAAA"> ]>\n'
    + f"<neutralXML>{BODY}</neutralXML>\n")

put("hostile", "dtd_billion_laughs",
    HEAD + '<!DOCTYPE neutralXML [\n<!ENTITY a "aaaaaaaaaa">\n'
    + "".join(f'<!ENTITY b{i} "&{"a" if i == 0 else f"b{i - 1}"};'
              f'&{"a" if i == 0 else f"b{i - 1}"};">\n' for i in range(12))
    + " ]>\n" + f"<neutralXML><name>&b11;</name>{BODY[BODY.index('<producer>'):]}</neutralXML>\n")

put("hostile", "external_entity",
    HEAD + '<!DOCTYPE neutralXML [ <!ENTITY x SYSTEM "file:///etc/passwd"> ]>\n'
    + f"<neutralXML><name>&x;</name>{BODY[BODY.index('<producer>'):]}</neutralXML>\n")

put("hostile", "deep_nesting",
    HEAD + "<neutralXML><name>deep</name><producer>FloTHERM</producer>"
    + "<geometry>" + "<assembly><name>a</name><active>true</active><geometry>" * 300
    + "</geometry></assembly>" * 300 + "</geometry></neutralXML>\n")

d = standard_materials(Ecxml("zero size", "FloTHERM"))
d.domain((0, 0, 0), (10 * MM, 10 * MM, 10 * MM))
d.block("flat", (1 * MM, 1 * MM, 1 * MM), (0.0, 4 * MM, 4 * MM), "Silicon", 1.0)
put("hostile", "zero_size_block", d)

d = Ecxml("undeclared material", "FloTHERM")
d.domain((0, 0, 0), (10 * MM, 10 * MM, 10 * MM))
d.block("b", (1 * MM, 1 * MM, 1 * MM), (4 * MM, 4 * MM, 4 * MM), "Unobtainium", 1.0)
put("awkward", "undeclared_material", d)

d = standard_materials(Ecxml("negative size", "FloTHERM"))
d.domain((0, 0, 0), (10 * MM, 10 * MM, 10 * MM))
d.block("b", (1 * MM, 1 * MM, 1 * MM), (-4 * MM, 4 * MM, 4 * MM), "Silicon", 1.0)
put("hostile", "negative_size", d)

put("hostile", "wrong_root",
    HEAD + "<someOtherFormat><name>x</name></someOtherFormat>\n")
put("hostile", "truncated", HEAD + "<neutralXML><name>x</name><produc")
put("hostile", "not_xml", b"\x00\x01\x02 this is not xml at all \xff\xfe")

print("\nlab written to", LAB)
