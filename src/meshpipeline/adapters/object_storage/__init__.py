# Responsibility: Expose the object-store surface - the contract's types plus the builder that constructs one.
# Boundaries: re-export only; the concrete store is chosen by the factory, never imported here.
from meshpipeline.adapters.object_storage.factory import _reset_for_tests, build_object_store
from meshpipeline.contracts.object_storage import (
    ObjectNotFound,
    ObjectStore,
    StorageError,
    StoredObject,
    normalize_key,
)

__all__ = ["ObjectStore", "StorageError", "ObjectNotFound", "StoredObject",
           "normalize_key", "build_object_store", "_reset_for_tests"]
