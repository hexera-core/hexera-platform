"""Makes this directory a package so its `conftest` does not collide.

Without this file pytest imports every `conftest.py` under `tests/unit` as the
bare top-level module name `conftest`, and the first one imported owns the name.
`tests/unit/cad/repair_v4/` sorts before `tests/unit/engines/` and
`tests/unit/worker/`, so once this directory gained a conftest in 4894733a, the
five pre-existing modules that do `from conftest import FakePublisher,
install_durable_execution_fakes` resolved against the wrong file and failed to
collect - which made `pytest tests/unit` exit 2 and silently stopped the whole
unit tier from running at all.

With this file the module is imported as `repair_v4.conftest`, the name
`conftest` is left to `tests/unit/conftest.py` that those modules mean, and
nothing else changes. No test in this directory imports the bare `conftest`.
"""
