# Responsibility: A 2D object may be flat on its plane's axis - every one of them, the grille too.
# Boundaries: the reader's size rule only; what the placement then does with a flat face is
# tests/unit/cad/test_ingest_ecxml.py's business.
"""A grille in Flotherm is a face on a wall. It has no thickness, and an export says so by writing
zero for the size on the plane's axis.

The reader kept two lists: the objects whose plane IS the object, and the objects allowed a zero
size. The grille was in the first and not the second, so a flat vent was refused - although the
placement puts a grille through the same branch as the 2D fan listed beside it. The two lists are
now one name, and these tests hold them equal, hold the grille readable flat, and hold the rule
that a 3D object is still not allowed to be flat.
"""
from __future__ import annotations

import pytest

from meshpipeline.cad.ingest.ecxml import (
    TWO_D,
    ZERO_SIZE_ALLOWED,
    EcxmlError,
    read_ecxml,
)
from meshpipeline.cad.ingest.ecxml_place import place

from tests.unit.cad.ecxml_models import Ecxml, standard_materials

MM = 1e-03


#: The wall the vent sits in. A grille as thick as the wall spans it; a flat one is a face on it.
WALL = 2.0 * MM


def _vent(tmp_path, thickness_m: float, *, kind: str = "grille"):
    """A 50 mm enclosure with one opening in its +yz wall, standing in a domain 10 mm bigger on
    every side so the opening has an outside to open onto."""
    d = standard_materials(Ecxml("a case with a vent", "FloTHERM"))
    d.domain((-10 * MM, -10 * MM, -10 * MM), (70 * MM, 70 * MM, 70 * MM))
    d.enclosure("case", (0, 0, 0), (50 * MM, 50 * MM, 50 * MM), "Steel sheet", WALL)
    where = ((0.0, 10 * MM, 10 * MM), (thickness_m, 20 * MM, 20 * MM), "+yz")
    if kind == "grille":
        d.grille("vent", *where, 2.5, 0.45)
    elif kind == "fan":
        d.fan2d("vent", *where, 0.004)
    elif kind == "plate":
        d.plate("vent", *where, "Aluminium 6063", 0.0)
    elif kind == "source":
        d.source2d("vent", *where, 1.5)
    else:  # pragma: no cover - a typo in a test argument
        raise AssertionError(kind)
    p = tmp_path / f"{kind}.ecxml"
    p.write_bytes(d.xml())
    return p


def test_the_two_lists_are_one_list_so_they_cannot_come_apart_again():
    """The grille went missing from one of two lists that say the same thing. They are now the
    same object, which is the only way a future element cannot be added to one and not the other."""
    assert ZERO_SIZE_ALLOWED is TWO_D
    assert "grille" in ZERO_SIZE_ALLOWED


@pytest.mark.parametrize("kind", ["grille", "fan", "plate", "source"])
def test_every_two_dimensional_object_may_be_flat(tmp_path, kind):
    """All four of them, not three: this is the test that fails before the fix, on `grille`."""
    model = read_ecxml(_vent(tmp_path, 0.0, kind=kind))
    vent = next(o for o in model.objects if o.name == "vent")
    assert vent.size is not None
    assert min(vent.size) == 0.0, "the export wrote a flat face and the reader kept it flat"


def test_a_flat_grille_reaches_the_placement_like_any_other_face(tmp_path):
    """Reading it is not enough; it has to come out the far side as an opening in the wall."""
    flat = place(read_ecxml(_vent(tmp_path, 0.0)))
    thick = place(read_ecxml(_vent(tmp_path, WALL)))
    assert len(flat.records["grilles"]) == len(thick.records["grilles"]), (
        "a flat grille and a thick one are the same kind of object to the placement")
    assert flat.records["grilles"], "the vent is an opening in the wall, flat or not"


def test_a_three_dimensional_object_is_still_not_allowed_to_be_flat(tmp_path):
    """The fix widens one set and no more. A solid block with no thickness is not a face, it is a
    broken file, and the refusal still names the object, the axis and the line."""
    d = standard_materials(Ecxml("a flat block", "FloTHERM"))
    d.domain((0, 0, 0), (40 * MM, 40 * MM, 40 * MM))
    d.block("slab", (2 * MM, 2 * MM, 2 * MM), (0.0, 8 * MM, 8 * MM), "Silicon", 1.0)
    p = tmp_path / "flat_block.ecxml"
    p.write_bytes(d.xml())
    with pytest.raises(EcxmlError) as got:
        read_ecxml(p)
    said = str(got.value)
    assert "slab" in said and "<x>" in said and "greater than 0" in said
