# Responsibility: Set the typed product modes a test needs, the way the deployment sets them.
# Boundaries: test support only; it builds the same frozen object settings/modes.py builds.
from __future__ import annotations

import meshpipeline.settings.policy as polcfg
from meshpipeline.settings.modes import ProductModes, TraceDisclosure


def set_modes(monkeypatch, *, collection: bool | None = None,
              disclosure: TraceDisclosure | str | None = None) -> ProductModes:
    current = polcfg.MODES
    modes = ProductModes(
        data_collection_enabled=current.data_collection_enabled if collection is None else collection,
        trace_disclosure=current.trace_disclosure if disclosure is None
        else TraceDisclosure(str(disclosure)),
    )
    monkeypatch.setattr(polcfg, "MODES", modes)
    return modes
