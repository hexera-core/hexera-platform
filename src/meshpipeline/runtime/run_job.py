# Responsibility: Run one pipeline execution directly, without a queue.
# Boundaries: a process entry point for a single run.
from __future__ import annotations

import argparse
import logging
import sys

# CLI --help text. Given only a job id, load the immutable dispatch_payload snapshot and run the
# complete LangGraph pipeline to completion in THIS process (the same run_simulation / build_graph
# path the Celery worker executes, same graph and Redis event semantics), then exit. No broker.
_CLI_DOC = "One-shot pipeline entrypoint: run a prepared job to completion in-process (no Celery)."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="meshpipeline.runtime.run_job", description=_CLI_DOC)
    ap.add_argument("--job-id", required=True, help="the SimulationJob id to run")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Runtime composition: this one-shot process runs the graph itself, so bind the product
    # contracts to their concrete adapters before the pipeline runs.
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()
    from meshpipeline.application.pipeline_run import run_from_job
    result = run_from_job(args.job_id)
    status = (result or {}).get("status", "unknown")
    logging.getLogger("meshpipeline.runtime.run_job").info("pipeline finished - job_id=%s status=%s",
                                             args.job_id, status)
    # non-zero exit on a failed run so the Cloud Run job surfaces it
    return 0 if status in ("succeeded", "failed") else 1


if __name__ == "__main__":
    sys.exit(main())
