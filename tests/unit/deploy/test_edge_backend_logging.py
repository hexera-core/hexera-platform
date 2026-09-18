# Responsibility: Verify the edge turns on the load balancer's request logging for every backend
# service it routes to, on every run, so a request the balancer refuses is visible somewhere.
# Boundaries: a static read of create-edge.sh; it calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
EDGE = REPO / "deploy" / "gcp" / "scripts" / "create-edge.sh"

# THE GAP THIS PINS. A backend service is created with logging off and `_ensure` reuses an
# existing one untouched. When dev's console backend started failing chat turns in the browser,
# the balancer had recorded nothing and Cloud Run behind it had never received the requests:
# there was no log anywhere to read. (The backend's request timeout is not configurable for a
# serverless NEG - gcloud refuses `--timeout` on one - so logging is the one edge setting that
# is ours to reconcile.)


def _script() -> str:
    return EDGE.read_text(encoding="utf-8")


def test_the_balancer_logs_every_backend_it_routes_to():
    s = _script()
    assert 'compute backend-services update "${backend}" --global' in s, \
        "the edge never reconciles an existing backend service"
    assert "--enable-logging" in s and "--logging-sample-rate=1" in s, \
        "a request the balancer refuses would be invisible everywhere"


def test_the_reconcile_runs_for_a_backend_that_already_existed():
    # `update` must follow the `_ensure` create/describe, inside the per-backend loop, so a backend
    # service from an earlier run is brought up to date rather than left as it was born.
    s = _script()
    ensure_at = s.index('compute backend-services create "${backend}" --global')
    update_at = s.index('compute backend-services update "${backend}" --global')
    loop_end = s.index('BACKENDS+=("${host}|${backend}")')
    assert ensure_at < update_at < loop_end, "the logging update is not applied to every backend on every run"


def test_no_timeout_is_set_on_a_serverless_backend():
    # gcloud: "Timeout sec is not supported for a backend service with Serverless network endpoint
    # groups". Setting one would fail the edge stage on every deploy.
    s = _script()
    assert "--timeout" not in s.split("compute backend-services", 1)[1], \
        "a serverless-NEG backend service cannot take --timeout; the edge stage would fail"
