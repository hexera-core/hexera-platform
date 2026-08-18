# Responsibility: Define the package holding what adapters share - the Redis client and the resilience wrappers.
# Boundaries: no code on purpose; nothing is re-exported, so an adapter imports the helper it means.
