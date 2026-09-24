# Responsibility: Answer which queues this application publishes work to, from the live Celery configuration.
# Owns: the three places a queue name can reach the broker from, and the refusal to answer with an empty set.
# Boundaries: it reads configuration and imports the task modules; it configures nothing and publishes nothing.

# WHY THIS IS NOT IN celery_app.py. That module says of itself "configuration only", and a function
# that introspects the configuration is not configuration. It is also the file a contract test reads as
# TEXT, so code that mentions a configuration key here rather than there keeps that test about the
# thing it is checking.
from __future__ import annotations

from meshpipeline.adapters.pipeline_execution.celery_app import celery_app


def published_queues() -> frozenset[str]:
    """Every queue this application publishes work to, read off the LIVE configuration.

    THE QUESTION THIS EXISTS TO ANSWER. A queue nobody consumes is silent by nature: the publish
    succeeds, the broker accepts the message, the caller logs "queued", and nothing ever runs. That
    is how `geometry_look` came to be published by the platform and consumed only by
    docker-compose.yml, so on the deployed fleet every look was queued and dropped, and with it
    every place the Surveyor would have named. Nothing in the repository could state the set of
    queues that need a consumer, so nothing could check it.

    THREE SOURCES, because a queue reaches the broker by three routes and any one of them alone is a
    blind spot:

      1. `task_routes` - the router's opinion, which is what an `apply_async` with no explicit queue
         gets.
      2. `beat_schedule` - a periodic task's `options.queue`, which the router never sees.
      3. THE TASKS THEMSELVES - `@task(queue=...)` puts the name on the task class, so a task with a
         decorator queue and no route is published to a queue this file does not mention. Reading it
         off the registered task is following the call rather than trusting the configuration to
         describe it.

    The task modules are imported here (`import_default_modules` walks `conf.include`), because an
    unimported task is an unregistered task and a registry that is merely empty would make every
    caller of this function agree that nothing needs consuming. An import failure is raised, not
    swallowed: not knowing the answer must never read as "no queues".
    """
    routes = getattr(celery_app.conf, "task_routes", None)
    schedule = getattr(celery_app.conf, "beat_schedule", None)
    loader = getattr(celery_app, "loader", None)
    if routes is None or schedule is None or loader is None:
        # The hermetic unit tier replaces `celery` with a stub whose conf carries nothing, so this is
        # reachable from a test process and only from one. Refusing is the point: a stubbed app would
        # otherwise report that no queue needs a consumer, and a gate built on that answer would pass
        # on a deployment that drains nothing.
        raise RuntimeError(
            "this is not a real Celery configuration - task_routes, beat_schedule or the loader is "
            "absent, which is what the hermetic test tier's celery stub looks like. Ask this question "
            "in a process that imports the installed celery.")
    names: set[str] = set()
    for route in (routes or {}).values():
        queue = route.get("queue") if isinstance(route, dict) else None
        if queue:
            names.add(str(queue))
    for entry in (schedule or {}).values():
        queue = (entry.get("options") or {}).get("queue") if isinstance(entry, dict) else None
        if queue:
            names.add(str(queue))
    loader.import_default_modules()
    for name, task in celery_app.tasks.items():
        # celery's own bookkeeping tasks (celery.chord, celery.backend_cleanup, ...) run on the
        # default queue and are not this application's work.
        if str(name).startswith("celery."):
            continue
        queue = getattr(task, "queue", None)
        if queue:
            names.add(str(queue))
    if not names:
        raise RuntimeError(
            "published_queues() found no queue at all, which cannot be true of an application that "
            "routes five of them. Something imported an empty Celery app, and an empty answer here "
            "reads as 'no queue needs a consumer' - the exact silence this function exists to break.")
    return frozenset(names)
