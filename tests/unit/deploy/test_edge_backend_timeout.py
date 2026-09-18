# Responsibility: Verify the edge gives each console's load-balancer backend the request timeout the
# console itself allows, on every run, and logs the requests the balancer refuses.
# Boundaries: a static read of create-edge.sh; it calls no cloud.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
EDGE = REPO / "deploy" / "gcp" / "scripts" / "create-edge.sh"

# THE DEFECT THIS PINS. A backend service is created with Google's 30-second default and `_ensure`
# reuses an existing one untouched, so dev's console backend cut off every chat turn the intake
# spent more than 30 seconds on. The browser said "Failed to fetch"; Cloud Run, which allows the
# console 300 seconds, saw nothing; the balancer, with logging off, recorded nothing. The timeout
# has to be reconciled every run, from the service's own limit, and refusals have to be logged.


def _script() -> str:
    return EDGE.read_text(encoding="utf-8")


def test_the_backend_timeout_is_set_from_the_services_own_limit_every_run():
    s = _script()
    m = re.search(r'compute backend-services update "\$\{backend\}" --global \\\n\s+--timeout="\$\{be_timeout\}"', s)
    assert m, "the edge never reconciles the backend service's timeout"
    assert 'be_timeout="${CONSOLE_TIMEOUT_SECONDS:-300}"' in s, "the console backend does not take the console's own timeout"
    assert 'be_timeout="${ADMIN_TIMEOUT_SECONDS:-${CONSOLE_TIMEOUT_SECONDS:-300}}"' in s, \
        "the admin backend does not take the admin service's own timeout"


def test_the_reconcile_runs_for_a_backend_that_already_existed():
    # `update` must follow the `_ensure` create/describe, inside the per-backend loop, so a backend
    # service from an earlier run is brought up to date rather than left at its birth default.
    s = _script()
    ensure_at = s.index('compute backend-services create "${backend}" --global')
    update_at = s.index('compute backend-services update "${backend}" --global')
    loop_end = s.index('BACKENDS+=("${host}|${backend}")')
    assert ensure_at < update_at < loop_end, "the timeout update is not applied to every backend on every run"


def test_the_balancer_logs_what_it_refuses():
    s = _script()
    assert "--enable-logging" in s and "--logging-sample-rate=1" in s, \
        "a request the balancer refuses would be invisible everywhere"
