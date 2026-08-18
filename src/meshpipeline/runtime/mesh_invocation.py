# Responsibility: Parse the per-run inputs the Cloud Run mesh job is invoked with, and refuse an incomplete one.
# Owns: MeshJobInvocation - the container's own argument contract, not developer configuration.
# Boundaries: one job's inputs; nothing here is a root .env setting and nothing here is a product mode.
from __future__ import annotations

import os
from dataclasses import dataclass

from meshpipeline.settings.env import ConfigurationError

DEFAULT_ENGINE = "cfmesh"
DEFAULT_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class MeshJobInvocation:
    input_uri: str
    output_uri: str
    engine: str
    timeout_seconds: int


def from_environment(env: dict[str, str] | None = None) -> MeshJobInvocation:
    # The dispatcher sets these per execution as container overrides. They are arguments to ONE
    # run, so a missing one is a broken dispatch, not a configuration the operator can fix in .env.
    src = os.environ if env is None else env
    missing = [n for n in ("INPUT_URI", "OUTPUT_URI") if not (src.get(n) or "").strip()]
    if missing:
        raise ConfigurationError(
            f"the mesh job was invoked without {', '.join(missing)}. These are per-run container "
            "overrides set by the dispatcher, not settings - the execution cannot be repaired here.")
    raw_timeout = (src.get("TIMEOUT") or "").strip() or str(DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = int(raw_timeout)
    except ValueError:
        raise ConfigurationError(
            f"the mesh job was invoked with TIMEOUT={raw_timeout!r}, which is not a whole number "
            "of seconds.") from None
    if timeout <= 0:
        raise ConfigurationError(
            f"the mesh job was invoked with TIMEOUT={timeout}; a run needs a positive budget.")
    return MeshJobInvocation(
        input_uri=src["INPUT_URI"].strip(),
        output_uri=src["OUTPUT_URI"].strip(),
        engine=(src.get("ENGINE") or "").strip() or DEFAULT_ENGINE,
        timeout_seconds=timeout,
    )
