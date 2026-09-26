# Responsibility: Verify the scheduled meter sweep drains a backlog in bounded batches and treats an
#                 unconfigured deployment as nothing to do, not as a failure.
from __future__ import annotations

import json

import pytest

from meshpipeline.application import metering_service
from meshpipeline.application.maintenance import meter
from meshpipeline.contracts import billing
from meshpipeline.runtime import meter_sweep


@pytest.fixture(autouse=True)
def _clear_gateway():
    yield
    billing.set_billing_gateway(None)


@pytest.fixture
def gateway(monkeypatch):
    import meshpipeline.adapters.stripe_billing as stripe_billing
    monkeypatch.setattr(stripe_billing, "build_billing_gateway", lambda: object())


def test_an_unconfigured_deployment_exits_cleanly(monkeypatch, capsys):
    import meshpipeline.adapters.stripe_billing as stripe_billing

    def _unconfigured():
        raise billing.BillingUnavailable("no key")
    monkeypatch.setattr(stripe_billing, "build_billing_gateway", _unconfigured)
    monkeypatch.setattr(meter, "report_pending_usage",
                        lambda: pytest.fail("swept without a gateway"))
    assert meter_sweep.main([]) == 0
    assert json.loads(capsys.readouterr().out)["billing"] == "unconfigured"


def test_a_full_batch_is_followed_by_another_and_a_short_one_ends_the_run(
        gateway, monkeypatch, capsys):
    batches = iter([{"reported": metering_service.SWEEP_BATCH, "skipped": 0},
                    {"reported": 3, "skipped": 1}])
    monkeypatch.setattr(meter, "report_pending_usage", lambda: next(batches))
    assert meter_sweep.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"reported": metering_service.SWEEP_BATCH + 3, "skipped": 1, "batches": 2}


def test_a_backlog_that_never_ends_is_bounded(gateway, monkeypatch, capsys):
    # A provider that keeps accepting slowly must not hold the job past its task timeout; the rest
    # is the next tick's.
    monkeypatch.setattr(meter, "report_pending_usage",
                        lambda: {"reported": metering_service.SWEEP_BATCH, "skipped": 0})
    assert meter_sweep.main([]) == 0
    assert json.loads(capsys.readouterr().out)["batches"] == meter_sweep.MAX_BATCHES


def test_a_batch_of_unreportable_rows_ends_the_run_instead_of_re_reading_them(
        gateway, monkeypatch, capsys):
    # A skipped row stays unstamped and is selected first again; counting it toward a full batch
    # would loop over the same rows every iteration while reportable ones waited behind them.
    calls = []

    def _sweep():
        calls.append(1)
        return {"reported": 0, "skipped": metering_service.SWEEP_BATCH}
    monkeypatch.setattr(meter, "report_pending_usage", _sweep)
    assert meter_sweep.main([]) == 0
    assert len(calls) == 1
