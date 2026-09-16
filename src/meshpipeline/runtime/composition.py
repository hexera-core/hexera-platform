# Responsibility: Bind every declared contract to the concrete adapter this deployment is configured for.
# Boundaries: THE composition root: the one place a Protocol meets an implementation.
from __future__ import annotations

import meshpipeline.settings.providers as provcfg
from meshpipeline.contracts.mesh_execution import MeshExecutor
from meshpipeline.contracts.pipeline_execution import PipelineLauncher, set_pipeline_launcher


def build_pipeline_launcher() -> PipelineLauncher:
    import types

    name = provcfg.PIPELINE_BACKEND
    # Lazy per-backend import (only the selected adapter is loaded); the module is bound to a
    # single annotated variable so the branches don't rebind an imported name.
    backend: types.ModuleType
    if name == "celery":
        from meshpipeline.adapters.pipeline_execution import celery
        backend = celery
    elif name == "deferred":
        from meshpipeline.adapters.pipeline_execution import deferred
        backend = deferred
    else:
        raise ValueError(
            f"unknown PIPELINE_BACKEND {name!r} (expected celery|deferred)")
    return backend


def install_pipeline_launcher() -> None:
    set_pipeline_launcher(build_pipeline_launcher())


def build_mesh_executor() -> MeshExecutor:
    # The provider executor, behind the submission authority. The authority is what makes a
    # same-generation replay reuse an accepted submission instead of starting a second mesh run,
    # so nothing may reach the provider except through it.
    from meshpipeline.adapters.mesh_execution.cloud_run_client import CloudRunMeshExecutor
    from meshpipeline.application.native_submission import ClaimingMeshExecutor
    return ClaimingMeshExecutor(CloudRunMeshExecutor())


def _enqueue_training_export(job_id: str, state: dict, *, created_at=None, ended_at=None) -> None:
    from meshpipeline.adapters.pipeline_execution.maintenance_tasks import export_conversation_sample
    export_conversation_sample.apply_async(
        args=[job_id, state],
        kwargs={"created_at": created_at, "ended_at": ended_at},
        queue="training_export",
    )


def install_adapters() -> None:
    from meshpipeline.adapters.dead_letter.redis import RedisDeadLetterSink
    from meshpipeline.adapters.delivery_guard.redis import RedisDeliveryGuard
    from meshpipeline.adapters.event_stream.redis import JobPublisher, RedisEventSubscription
    from meshpipeline.adapters.firebase_token.identity_platform import (
        verify as verify_identity_platform_token,
    )
    from meshpipeline.adapters.inference_telemetry.redis import RedisInferenceTelemetrySink
    from meshpipeline.adapters.mesh_timing.redis import RedisMeshTimingStore
    from meshpipeline.adapters.model_capacity.redis import RedisCapacityController
    from meshpipeline.adapters.model_inference import router
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.adapters.rate_limit.redis import RedisRateLimitStore
    from meshpipeline.adapters.search.factory import build_web_search_provider
    from meshpipeline.adapters.ws_ticket.redis import RedisWsTicketStore
    from meshpipeline.contracts import (
        dead_letter,
        delivery_guard,
        event_stream,
        firebase_token,
        inference_telemetry,
        mesh_execution,
        mesh_timing,
        model_capacity,
        model_inference,
        object_storage,
        rate_limit,
        search,
        training_export,
        ws_ticket,
    )

    model_inference.set_model_router(router)
    event_stream.set_publisher_factory(JobPublisher)
    event_stream.set_subscription_factory(RedisEventSubscription)
    object_storage.set_object_store(build_object_store())
    search.set_web_search_provider(build_web_search_provider())
    training_export.set_export_enqueuer(_enqueue_training_export)
    mesh_execution.set_mesh_executor(build_mesh_executor())
    # Console sign-in. The product knows only `contracts.firebase_token.verify`; which identity
    # provider is behind it - and therefore which certificate endpoint and which claim
    # vocabulary - is settled here, once.
    firebase_token.set_token_verifier(verify_identity_platform_token)

    # The narrow Redis-backed capabilities. Each is its own port: a capability can move off
    # Redis (or off a shared Redis) on its own, without touching the others or the callers.
    delivery_guard.set_delivery_guard(RedisDeliveryGuard())
    rate_limit.set_rate_limit_store(RedisRateLimitStore())
    dead_letter.set_dead_letter_sink(RedisDeadLetterSink())
    mesh_timing.set_mesh_timing_store(RedisMeshTimingStore())
    # The router builds a fully costed InferenceCall for EVERY model call, and with no sink bound
    # the contract's recorder returns immediately and discards each one. Pricing is measured
    # resource cost, so an unbound sink is lost revenue, not a missing graph.
    inference_telemetry.set_inference_telemetry(RedisInferenceTelemetrySink())
    ws_ticket.set_ws_ticket_store(RedisWsTicketStore())
    # Model-call admission sits in front of EVERY model call (routing.execute -> acquire), so
    # a process that composes adapters without it fails on its first Intake/Builder/Reviewer
    # turn with "no capacity controller configured". Redis-backed like the capabilities above:
    # admission has to be shared across API and worker processes, which an in-process
    # controller cannot do. The adapter opens the configured client itself, lazily.
    model_capacity.set_capacity_controller(RedisCapacityController())

    install_pipeline_launcher()
