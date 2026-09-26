# Responsibility: Report every pending overage debit to the billing provider's meter, then exit.
# Boundaries: a process entry point for the scheduled Cloud Run job (deploy/gcp/scripts/
#             create-meter-sweep.sh). Binds only the billing gateway; what is reported, and why it
#             is safe to run twice, is application/metering_service.py.
from __future__ import annotations

import json
import logging
import sys

_CLI_DOC = "One-shot meter sweep: report unmetered overage to the billing provider, then exit."

#: HOW MANY BATCHES one execution may drain. A backlog built up over an outage is worked off in
#: bounded transactions, and this caps the whole run so a provider that keeps accepting slowly
#: cannot hold the job past its task timeout. Whatever is left is the next tick's.
MAX_BATCHES = 20


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("meshpipeline.runtime.meter_sweep")

    # ONLY THE BILLING GATEWAY. install_adapters() would also bind Redis, the object store and the
    # model router, none of which a sweep touches - and each one is another setting this job would
    # need, and another way for it to fail before reporting anything.
    from meshpipeline.adapters.stripe_billing import build_billing_gateway
    from meshpipeline.application import metering_service
    from meshpipeline.application.maintenance import meter
    from meshpipeline.contracts import billing
    try:
        billing.set_billing_gateway(build_billing_gateway())
    except billing.BillingUnavailable:
        # NOT A FAILURE. The deploy provisions this job only where billing is configured, so this
        # is a key removed after the fact; a non-zero exit would page for a deliberate state.
        log.warning("billing is not configured in this deployment; nothing to report")
        print(json.dumps({"reported": 0, "billing": "unconfigured"}))
        return 0

    total = {"reported": 0, "skipped": 0, "batches": 0}
    for _ in range(MAX_BATCHES):
        out = meter.report_pending_usage()
        total["reported"] += int(out.get("reported", 0))
        total["skipped"] += int(out.get("skipped", 0))
        total["batches"] += 1
        # A SHORT BATCH IS THE END OF THE BACKLOG. A full one may have more behind it.
        if int(out.get("reported", 0)) + int(out.get("skipped", 0)) < metering_service.SWEEP_BATCH:
            break
    print(json.dumps(total))
    # SKIPPED ROWS ARE RETRIED, NOT LOST - each stays unstamped for the next tick. They are reported
    # as a warning in the job's own log rather than as a failed execution, because one tenant whose
    # customer the provider rejects must not turn every tick red for everyone else.
    if total["skipped"]:
        log.warning("%s overage entr(ies) could not be reported and wait for the next run",
                    total["skipped"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
