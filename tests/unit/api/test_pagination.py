# tests/unit/api/test_pagination.py
# Responsibility: Verify the cursor round-trips and refuses anything it did not mint.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from meshpipeline.api import pagination

ROW = uuid.UUID("33333333-3333-3333-3333-333333333333")
AT = datetime(2026, 9, 10, 12, 30, 45, tzinfo=UTC)


def test_a_cursor_round_trips_to_the_values_it_was_built_from():
    assert pagination.decode_cursor(pagination.encode_cursor(AT, ROW)) == (AT, ROW)


def test_an_absent_cursor_decodes_to_none_rather_than_raising():
    assert pagination.decode_cursor(None) is None
    assert pagination.decode_cursor("") is None


def test_a_cursor_this_module_did_not_mint_decodes_to_none():
    # A caller can put anything in a query string. A malformed cursor must read as "start at the
    # beginning" rather than 500 a proven caller -- the same narrowing tenant_scope applies to a
    # malformed organisation id.
    for junk in ("not-base64", "!!!!", "YWJj", pagination.encode_cursor(AT, ROW)[:-4]):
        assert pagination.decode_cursor(junk) is None


def test_the_limit_clamps_to_the_documented_bounds():
    assert pagination.clamp_limit(25) == 25
    assert pagination.clamp_limit(0) == 1
    assert pagination.clamp_limit(-5) == 1
    assert pagination.clamp_limit(1000) == 100
