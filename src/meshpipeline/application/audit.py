# Responsibility: Write one structured audit line per security-relevant event the API decides.
# Owns: the audit line's shape, its redaction rule, and the label the log sink routes on.
# Boundaries: it writes; it never decides whether the action is allowed, and a failed write never
#             refuses the action it describes.
from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

#: THE ROUTING LABEL. deploy/gcp/scripts/configure-audit-log-bucket.sh sinks every entry carrying
#: it into a locked, retention-bound log bucket that no application identity can write to or
#: delete from - that bucket, not this process, is what makes the trail tamper-resistant. The
#: admin console's own trail uses the same label key with kind "admin-action".
LABELS = {"component": "api", "kind": "audit"}

#: Detail keys whose values never reach the trail: any key containing one of the parts, or equal
#: to one of the names. The trail records THAT a credential or address was involved, not what it
#: was. `key_id` survives on purpose - an identifier is what makes the line traceable.
_REDACTED_KEY_PARTS = ("secret", "token", "password", "email", "authorization", "signature")
_REDACTED_KEY_NAMES = frozenset({"key", "api_key", "raw_key", "x_api_key", "sig", "x_user_sig"})
REDACTED = "[redacted]"


def _scrub(detail: dict) -> dict:
    out: dict = {}
    for key, value in detail.items():
        lowered = key.lower()
        if lowered in _REDACTED_KEY_NAMES or any(p in lowered for p in _REDACTED_KEY_PARTS):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = _scrub(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out


def record(action: str, *, outcome: str = "success", actor: str = "",
           organization_id: str = "", target: str = "", detail: dict | None = None,
           write: Callable[[str], None] | None = None) -> None:
    """Write one audit line.

    `actor` is an opaque identifier - a user id, an API key id, "admin", "system" - and never an
    email address: the trail is retained far longer than an account, and an address in it would
    outlive the person's erasure request.

    STDOUT, not a table, for the reason the admin console gives: Cloud Run ingests a JSON object
    on stdout into Cloud Logging intact, and a record this process cannot later rewrite is worth
    more than one it can.
    """
    payload = {
        "severity": "NOTICE",
        "message": f"audit {action} {outcome}",
        "timestamp": datetime.now(UTC).isoformat(),
        "logging.googleapis.com/labels": LABELS,
        "audit": {
            "action": action,
            "outcome": outcome,
            "actor": actor or "anonymous",
            "organization_id": organization_id or "",
            "target": target or "",
            "detail": _scrub(detail or {}),
        },
    }
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        (write or _stdout)(line)
    except Exception as exc:
        # The action still proceeds - refusing a sign-in because logging is degraded is the wrong
        # failure - but the miss is reported so an unaudited action is visible, not silent.
        logger.error("audit line could not be written for %s (%s)", action, type(exc).__name__)


def _stdout(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
