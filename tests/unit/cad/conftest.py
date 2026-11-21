# Responsibility: Keep the hermetic cad unit tier hermetic about the DELIVERED-FILE check.
# Boundaries: unit tests only. The real check (write STEP, read back in a child, BRepCheck) is
#             proved in tests/native/; here sessions run on stub shapes that cannot be written.
from __future__ import annotations

import pytest


@pytest.fixture
def real_export_check():
    """Request this fixture to run `delivery_check.export_check` for real in a unit test."""
    return True


@pytest.fixture(autouse=True)
def _stub_export_check(request, monkeypatch):
    """`session.try_package` re-reads every delivered file (delivery_check.export_check). On the
    stub shapes this tier uses that read can never succeed, and an unmeasured answer is - by
    design - a refusal, so every package would revert. Stubbed to "passes" here unless a test asks
    for `real_export_check`."""
    if "real_export_check" in request.fixturenames:
        return
    from meshpipeline.cad.repair import delivery_check

    monkeypatch.setattr(delivery_check, "export_check", lambda *args, **kwargs: "")
