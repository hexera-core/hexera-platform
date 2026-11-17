# Responsibility: Re-deliver one finished run's artifacts from its surviving workspace, without a queue.
# Boundaries: a process entry point for a single re-delivery; it runs no graph and no mesher.
from __future__ import annotations

import argparse
import logging
import sys

# CLI --help text. Given a job id, upload and register the deliverables of a run whose mesh is still
# on disk but whose delivery never landed - the same uploader the pipeline uses, in THIS process, no
# broker and no re-mesh. The job's status is NOT changed: the run stays failed, which is what
# happened, and nothing a customer reads gates on it (the viewer and the downloads are served from
# the artifact rows this writes).
_CLI_DOC = ("One-shot re-delivery: upload a terminal-failed run's artifacts again from the "
            "workspace it left behind.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="meshpipeline.runtime.redeliver", description=_CLI_DOC)
    ap.add_argument("--job-id", required=True, help="the SimulationJob id to re-deliver")
    ap.add_argument("--workspace", default="",
                    help="the attempt workspace to deliver FROM; by default the run's own last "
                         "attempt directory that holds a mesh_manifest.json")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("meshpipeline.runtime.redeliver")
    # Runtime composition, for the same reason run_job.py calls it: this process does the delivery
    # itself, so the object store and the event publisher must be bound to their concrete adapters
    # before anything is uploaded or announced.
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()

    import asyncio

    from meshpipeline.application.redeliver import redeliver_job
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.session import dispose_engine, get_db

    async def _run():
        try:
            return await redeliver_job(get_db, JobRepository(), args.job_id,
                                       jlog=log, workspace=args.workspace or None)
        finally:
            # The engine is cached per event loop and this one is about to close; disposing inside
            # it is the only place the pooled connections can still be closed properly.
            await dispose_engine()

    outcome = asyncio.run(_run())
    if outcome.refused:
        log.error("NOT re-delivered - job_id=%s refused=%s", args.job_id, outcome.refused)
        return 1
    log.info("re-delivered - job_id=%s workspace=%s delivered=%s required_ready=%s record_restated=%s",
             args.job_id, outcome.workspace,
             [d.get("type") for d in outcome.delivered], outcome.required_ready, outcome.restated)
    # NON-ZERO UNLESS THE REQUIRED DELIVERABLES ARE ACTUALLY READY. The uploader can return having
    # delivered optional artifacts while a required one was refused as a conflict, and an operator
    # watching an exit code must not read that as a finished delivery.
    return 0 if outcome.required_ready else 1


if __name__ == "__main__":
    sys.exit(main())
