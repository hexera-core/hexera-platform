# Responsibility: Define the package holding the pipeline-execution backends - the Celery app and its tasks, deferred.
# Boundaries: no code on purpose - the composition root imports the backend it binds.
