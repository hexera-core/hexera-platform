# Responsibility: Put this platform's two geometry stage settings into the environment variables the measurement
#                 package reads them out of, and report what was set.
# Boundaries: the ONLY module that writes another component's environment contract. It decides nothing: the two
#             booleans are decided in settings/policy.py and passed in. No package import, no file, no I/O.
#
# WHY ITS OWN MODULE. `settings/policy.py` may not touch `os.environ` at all: every production module reads a typed
# setting and the loader is the only thing that reads the environment
# (tests/unit/settings/test_product_mode_authority.py). These two variables are not developer settings, they are
# another component's contract, the same shape as PROMETHEUS_MULTIPROC_DIR in runtime/metrics_server.py and
# ALEMBIC_CONFIG in runtime/migrate.py, and each of those lives in its own file for the same reason. Keeping this in
# policy.py exempted the whole settings module from the rule, which is too much to give up for two lines.
#
# THE NAMES ARE SPELLED OUT, one literal per read, and read through the approved reader. The configuration
# certification refuses a name built at runtime (devtools/quality/config_inventory.py) and it is right to: a name
# assembled from data is one no catalogue can check. A loop over PACKAGE_SWITCHES reads
# `os.environ[<a name from data>]` and fails that gate, which is how this module came to exist.
from __future__ import annotations

import logging
import os

from meshpipeline.settings.env import optional_env

logger = logging.getLogger(__name__)

#: `{the package's variable: the platform setting that decides it}`. Documentation and the one place the pairing
#: is written down; `arm` does not loop over it, for the reason in the header. Both names are declared in
#: settings/inventory.py with exposure `external`, which is what they are.
PACKAGE_SWITCHES: dict[str, str] = {
    "GEOMETRY_AGENT_MEASURED_STOPS": "GEOMETRY_MEASURED_STOPS_ENABLED",
    "GEOMETRY_AGENT_FLUID_SIDE": "GEOMETRY_FLUID_SIDE_ENABLED"}

#: The package's own words. It RAISES on a value it does not recognise, so `true`/`false` would turn a measurement
#: into an exception rather than a switch (geometry_agent.facts.measure.measured_stops_on,
#: geometry_agent.agent.catalog.fluid_side_on).
ON, OFF = "on", "off"


def arm(*, measured_stops: bool, fluid_side: bool) -> dict[str, str]:
    """Write both variables unless they are already set, and return what the package will now read.

    Idempotent: the same value every time, and never unset, so two jobs in one worker cannot disagree about it
    and a child process inherits whatever is there. `facts.measure_isolated` measures in a CHILD PROCESS, so the
    caller has to arm before it opens the file; setting and unsetting around a call would be two measurements in
    one worker racing over one variable.

    AN OPERATOR'S OWN EXPORT WINS. A developer who exported the package's variable meant it, and a platform that
    overwrote it would make the package's own tests and evals unrunnable in the same shell.
    """
    stops = optional_env("GEOMETRY_AGENT_MEASURED_STOPS", "").strip()
    if not stops:
        stops = ON if measured_stops else OFF
        os.environ["GEOMETRY_AGENT_MEASURED_STOPS"] = stops
    side = optional_env("GEOMETRY_AGENT_FLUID_SIDE", "").strip()
    if not side:
        side = ON if fluid_side else OFF
        os.environ["GEOMETRY_AGENT_FLUID_SIDE"] = side
    return {"GEOMETRY_AGENT_MEASURED_STOPS": stops, "GEOMETRY_AGENT_FLUID_SIDE": side}


__all__ = ["OFF", "ON", "PACKAGE_SWITCHES", "arm"]
