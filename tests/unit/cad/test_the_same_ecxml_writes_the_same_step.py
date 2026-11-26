# The same ECXML gave a different STEP every time, by one field.
#
# MEASURED 2026-11-25: two runs of `ecxml_to_step` on the real customer chip.ecxml gave two
# 732,887-byte files with 15,704 entities each, identical region names and an identical
# .ecxml.json sidecar, differing ONLY on the FILE_NAME time_stamp. Grepping the whole 732 KB body
# for DATE|TIME|CALENDAR returns nothing, so that is the file's only clock.
#
# No Interface_Static key reaches it - the complete `write.step.*` set is assembly, nonmanifold,
# product.name, resource.name, schema, sequence, tessellated, unit, vertex.mode. The one API that
# does is APIHeaderSection_MakeHeader(model).SetTimeStamp, and it has to be applied AFTER Transfer,
# which is what creates the header, and BEFORE Write. Pinned a line earlier it is silently
# overwritten and the file still looks right - which is why these tests assert the VALUE in the
# written file and not merely that the call happened.
import hashlib
import re
import time

import pytest

from meshpipeline.cad.ingest.ecxml_build import STEP_TIME_STAMP, write_named_step

FILE_NAME_STAMP = re.compile(rb"FILE_NAME\s*\(\s*'(?:[^']|'')*'\s*,\s*'([^']*)'", re.S)


def _a_named_solid():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return [("block", BRepPrimAPI_MakeBox(1.0, 2.0, 3.0).Shape()),
            ("coolant", BRepPrimAPI_MakeBox(0.5, 0.5, 0.5).Shape())]


def _write(tmp_path, name):
    dest = tmp_path / name
    write_named_step(_a_named_solid(), dest)
    return dest.read_bytes()


def test_two_writes_of_the_same_shapes_are_byte_identical(tmp_path):
    # ACROSS A CLOCK TICK, deliberately. Measured at ba728d85, before the pin: two writes of this
    # same box inside one second were already byte-identical, because the stamp only moves when
    # the second does. A guard that passes whenever the test is fast enough is not a guard.
    a = _write(tmp_path, "a.step")
    time.sleep(1.1)
    b = _write(tmp_path, "b.step")
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest(), (
        "the STEP still carries something that changes between writes")


def test_the_stamp_in_the_file_is_the_pinned_one(tmp_path):
    # The assertion that catches a pin applied in the wrong place: before Transfer the header does
    # not exist yet, the wall clock wins, and only the file itself shows it.
    got = FILE_NAME_STAMP.search(_write(tmp_path, "c.step"))
    assert got, "no FILE_NAME time_stamp in the written STEP"
    assert got.group(1).decode() == STEP_TIME_STAMP


def test_the_pinned_stamp_is_obviously_not_a_real_date():
    # It is a provenance field. A synthetic epoch cannot be mistaken for an event; a plausible
    # date would be a false one, because nothing in the input carries a date to derive from.
    assert STEP_TIME_STAMP == "1970-01-01T00:00:00"


def test_the_file_is_still_a_step_file_the_product_can_read_back(tmp_path):
    from meshpipeline.cad.regions import components_of

    dest = tmp_path / "d.step"
    write_named_step(_a_named_solid(), dest)
    assert dest.read_bytes().startswith(b"ISO-10303-21;")
    # `components_of` is THE read of a file's component structure in this product, and it hands
    # back the document that owns the labels - hold it, or every name goes stale.
    _doc, _tool, found, _roots = components_of(dest)
    assert sorted(name for _label, name in found) == ["block", "coolant"], found


def test_the_entity_count_is_not_changed_by_the_pin(tmp_path):
    # The pin edits one header field. If it ever started rewriting the body this would move.
    body = _write(tmp_path, "e.step")
    assert sum(1 for line in body.splitlines() if line.startswith(b"#")) > 100


@pytest.mark.parametrize("which", ["a", "b"])
def test_a_second_write_in_a_fresh_file_name_does_not_change_the_bytes(tmp_path, which):
    # Guards the OTHER nondeterminism this does NOT fix, so nobody is surprised by it later:
    # `cad.write_step` and `repair/conservative._write_step` use a plain STEPControl_Writer whose
    # PRODUCT name carries a process-global write ordinal ('… 7.9 3', '… 7.9 4'). The XCAF writer
    # used here does not, which is why pinning the clock is enough for THIS path.
    assert hashlib.sha256(_write(tmp_path, f"{which}1.step")).hexdigest() == \
           hashlib.sha256(_write(tmp_path, f"{which}2.step")).hexdigest()
