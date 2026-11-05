# Responsibility: Derive the object-storage coordinates one native submission exchanges through.
# Boundaries: derivation only - it stores nothing, calls no provider and decides no submission.
# Collaborates with: artifact_keys.py for the shared layout, and cloud_run_client.py, which exchanges through these.
from __future__ import annotations

import re
from dataclasses import dataclass

from meshpipeline.artifact_keys import input_key, output_key, result_key

#: The operation key's shape, as the claim authority derives it: sha256, lowercase hex. Checked
#: here rather than trusted, because these coordinates become object paths - a separator or a
#: traversal segment arriving from a caller would leave the operation's namespace.
_OPERATION_KEY = re.compile(r"\A[0-9a-f]{64}\Z")

#: The exchange identifier IS the operation key. Not a truncation of it: two operations whose
#: keys share a prefix would share a namespace, and the historical 12-character identifier was
#: random precisely because it carried no identity to collide on. No installed consumer imposes a
#: length - `runtime/mesh_invocation` reads whole URIs and `result_key_beside` derives from the
#: output key's directory - so the full digest travels unchanged.
EXCHANGE_ID_FORMAT = "the operation key verbatim: 64 lowercase hex characters"


class InvalidOperationKey(ValueError):
    pass


@dataclass(frozen=True)
class ExchangeCoordinates:

    operation_key: str
    exchange_id: str
    input_object_key: str
    output_object_key: str
    result_object_key: str

    def as_dict(self) -> dict[str, str]:
        # Serialisable without anything machine-specific: no workspace path, no token, no
        # credential, no filename from the user's upload.
        return {"operation_key": self.operation_key, "exchange_id": self.exchange_id,
                "input_object_key": self.input_object_key,
                "output_object_key": self.output_object_key,
                "result_object_key": self.result_object_key}


def coordinates_for(operation_key: str) -> ExchangeCoordinates:
    # THE ONLY construction. There is deliberately no caller-supplied exchange id: the identity
    # is the claim authority's, and a caller able to name its own namespace could submit twice
    # under two names while holding one claim.
    if not isinstance(operation_key, str) or not _OPERATION_KEY.match(operation_key):
        raise InvalidOperationKey(
            "the exchange identity must be the native-submission operation key - 64 lowercase "
            f"hex characters - and {operation_key!r} is not one; these coordinates become object "
            "paths, so a malformed key is refused before any storage is touched")
    return ExchangeCoordinates(
        operation_key=operation_key,
        exchange_id=operation_key,
        input_object_key=input_key(operation_key),
        output_object_key=output_key(operation_key),
        result_object_key=result_key(operation_key),
    )
