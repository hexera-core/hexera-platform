# Responsibility: Verify a file event carries normalised paths and safe metadata, refusing traversal or a bad size.
from __future__ import annotations

from pathlib import Path

import pytest

import meshpipeline.events as E

APP = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"
UI = Path(__file__).parent.parent.parent.parent / "ui"


# the contract


def test_file_is_in_the_closed_vocabulary():
    assert E.FILE in E.EVENT_TYPES
    assert E.FILE_OPERATIONS == frozenset({"created", "updated", "generated", "packaged"})


def test_the_wire_carries_only_safe_metadata():
    w = E.file("builder", "system/meshDict", 2418, "created").wire()
    assert set(w) == {"type", "stage", "ts", "display_path", "byte_count",
                      "operation", "status"}
    assert w["type"] == "file" and w["stage"] == "builder"
    assert w["display_path"] == "system/meshDict"
    assert w["byte_count"] == 2418 and isinstance(w["byte_count"], int)
    assert w["operation"] == "created" and w["status"] == "ok"
    assert "content" not in w and "workspace" not in w


@pytest.mark.parametrize("given,expected", [
    ("system/meshDict", "system/meshDict"),
    ("/srv/workspaces/843f/attempt_1/system/meshDict",
     "srv/workspaces/843f/attempt_1/system/meshDict"),
    ("../../etc/passwd", "etc/passwd"),
    ("./system/./meshDict", "system/meshDict"),
    ("system\\meshDict", "system/meshDict"),
])
def test_paths_are_normalised_so_none_can_climb_out_or_be_absolute(given, expected):
    w = E.file("builder", given, 1).wire()
    assert w["display_path"] == expected
    assert not w["display_path"].startswith("/")
    assert ".." not in w["display_path"].split("/")


def test_a_path_that_is_only_traversal_is_refused():
    for bad in ("..", "../..", "/", "   ", ""):
        with pytest.raises(ValueError):
            E.file("builder", bad, 1)


def test_an_operation_outside_the_vocabulary_is_refused():
    with pytest.raises(ValueError):
        E.file("builder", "a", 1, "exfiltrated")


def test_a_negative_size_is_refused():
    with pytest.raises(ValueError):
        E.file("builder", "a", -1)


def test_the_publisher_can_emit_it_and_nothing_wider():
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    assert hasattr(JobPublisher, "file")
    assert not hasattr(JobPublisher, "file_contents")


# the producer


def test_a_successful_write_reports_whether_it_created_or_updated(tmp_path):
    from meshpipeline.agents.builder.tools import _tool_write_file
    first = _tool_write_file(tmp_path, "system/meshDict", "surfaceFile x;")
    assert first == {"written": "system/meshDict", "bytes": 14, "operation": "created"}
    again = _tool_write_file(tmp_path, "system/meshDict", "surfaceFile yy;")
    assert again["operation"] == "updated" and again["bytes"] == 15


def test_the_byte_count_is_the_bytes_actually_written(tmp_path):
    from meshpipeline.agents.builder.tools import _tool_write_file
    body = "naïve—unicode\n"
    r = _tool_write_file(tmp_path, "notes.txt", body)
    assert r["bytes"] == len(body.encode()) == (tmp_path / "notes.txt").stat().st_size


def test_a_blocked_write_carries_no_success_metadata(tmp_path):
    from meshpipeline.agents.builder.tools import _tool_write_file
    escaped = _tool_write_file(tmp_path, "../outside.txt", "x")
    assert "error" in escaped
    assert "written" not in escaped and "bytes" not in escaped, \
        "a refused write handed the caller something to report as a completed file"


# the browser
