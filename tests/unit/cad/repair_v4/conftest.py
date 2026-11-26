"""The engine's judges share one process-wide verdict cache (`default_verdict_cache`); a test must never see
another test's entries, so it is emptied around every test."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meshpipeline.cad.repair_v4.verdict.cache import reset_default_verdict_cache


@pytest.fixture(autouse=True)
def _fresh_default_verdict_cache() -> Iterator[None]:
    reset_default_verdict_cache()
    yield
    reset_default_verdict_cache()
