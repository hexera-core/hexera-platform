# Responsibility: Classify every failure into one vocabulary, and decide what a user may be told about it.
# Owns: the failure classes, exception classification, the public message for each, and the persisted reason.
# Boundaries: classification and redaction; it raises nothing itself and performs no recovery.
# Collaborates with: api/ for public errors, persistence/models.py for stored reasons, and contracts/dead_letter.py.
from __future__ import annotations

import enum
import json
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


class FailureClass(str, enum.Enum):
    # domain outcomes (the request/mesh is the issue; terminal, user-facing)
    USER_INPUT       = "user_input"        # bad/unsupported request or geometry
    DOMAIN_REJECTED  = "domain_rejected"   # reviewer FAIL / unsolvable / contract
    # The caller asked for something that is not theirs. Not a bad request and not a bug: saying
    # "your geometry is unsupported" would be false, and "we broke" would hide a real refusal.
    NOT_AUTHORIZED   = "not_authorized"

    # Durable data is not what it must be: an approved snapshot that no longer matches its row,
    # an object missing from storage, bytes that do not match their checksum. Distinct from
    # DEPENDENCY_DOWN, which means we could not REACH storage - here we reached it and what came
    # back was wrong. Blameless to the user, and never retryable: repeating the same read of the
    # same corrupted data cannot produce a different answer.
    DATA_INTEGRITY   = "data_integrity"

    # system failures (a dependency failed; not the user's fault)
    PROVIDER_TRANSIENT = "provider_transient"  # LLM 429/5xx/timeout - retryable
    PROVIDER_DOWN      = "provider_down"       # circuit open / provider exhausted
    DEPENDENCY_DOWN    = "dependency_down"     # DB / Redis / MinIO / SearXNG unreachable
    RESOURCE           = "resource"            # OOM / timeout / subprocess killed
    INTERNAL           = "internal"            # unexpected bug
    # review evidence (we could not JUDGE the mesh; not the provider, not the mesh)
    # Telling a user "an AI service is unavailable" when the truth is "your mesh's quality
    # evidence was incomplete" is a lie that also hides a real quality signal. This is a
    # system failure (we could not verify), never a PASS and never a mesh FAIL.
    REVIEW_EVIDENCE_MISSING = "review_evidence_missing"

    @property
    def is_system(self) -> bool:
        return self in (
            FailureClass.PROVIDER_TRANSIENT, FailureClass.PROVIDER_DOWN,
            FailureClass.DEPENDENCY_DOWN, FailureClass.RESOURCE, FailureClass.INTERNAL,
            FailureClass.REVIEW_EVIDENCE_MISSING, FailureClass.DATA_INTEGRITY,
        )

    @property
    def is_retryable(self) -> bool:
        return self in (FailureClass.PROVIDER_TRANSIENT, FailureClass.DEPENDENCY_DOWN)


class SystemFailure(Exception):

    def __init__(self, dependency: str, failure_class: FailureClass,
                 operator_detail: str = "", *, cause: BaseException | None = None):
        self.dependency = dependency
        self.failure_class = failure_class
        self.operator_detail = operator_detail or str(cause or "")
        self.cause = cause
        super().__init__(f"[{failure_class.value}] {dependency}: {self.operator_detail}")

    def __reduce__(self):
        # Picklable for Celery's result backend: the default Exception pickling
        # replays self.args through __init__, which our positional signature would
        # reject (-> UnpickleableExceptionWrapper). `cause` is intentionally dropped
        # (a raw BaseException may itself be unpicklable; the detail is preserved).
        return (self.__class__, (self.dependency, self.failure_class, self.operator_detail))


# #
# Classification - map raw exceptions to a FailureClass for a given dependency.
# #
_TIMEOUT_NAMES = ("Timeout", "TimeoutError", "TimeoutExpired", "ReadTimeout",
                  "ConnectTimeout", "APITimeoutError")
_CONN_NAMES    = ("ConnectionError", "ConnectError", "APIConnectionError",
                  "RemoteProtocolError", "ConnectionRefusedError", "OperationalError",
                  "InterfaceError", "RedisConnectionError")
_RATE_NAMES    = ("RateLimitError", "TooManyRequests")
_SERVER_NAMES  = ("InternalServerError", "ServiceUnavailable", "BadGateway",
                  "ServerError", "S3Error", "MinioException")


def classify_exception(exc: BaseException, dependency: str) -> SystemFailure:
    if isinstance(exc, SystemFailure):
        return exc
    name = type(exc).__name__
    detail = f"{name}: {exc}"
    if any(t in name for t in _RATE_NAMES):
        return SystemFailure(dependency, FailureClass.PROVIDER_TRANSIENT, detail, cause=exc)
    if any(t in name for t in _TIMEOUT_NAMES):
        # subprocess timeout = resource exhaustion; network timeout = transient
        cls = FailureClass.RESOURCE if "Expired" in name else FailureClass.PROVIDER_TRANSIENT
        return SystemFailure(dependency, cls, detail, cause=exc)
    if any(t in name for t in _CONN_NAMES):
        return SystemFailure(dependency, FailureClass.DEPENDENCY_DOWN, detail, cause=exc)
    if any(t in name for t in _SERVER_NAMES):
        return SystemFailure(dependency, FailureClass.PROVIDER_TRANSIENT, detail, cause=exc)
    if isinstance(exc, MemoryError):
        return SystemFailure(dependency, FailureClass.RESOURCE, detail, cause=exc)
    if isinstance(exc, OSError):
        return SystemFailure(dependency, FailureClass.RESOURCE, detail, cause=exc)
    return SystemFailure(dependency, FailureClass.INTERNAL, detail, cause=exc)


_EXACT_MARKER_CLASS: dict[str, FailureClass] = {
    # The reviewer could not JUDGE the mesh: required evidence was missing/unreadable, or its
    # declaration omitted required axes. Not a provider outage, not a mesh FAIL.
    "reviewer_evidence_missing": FailureClass.REVIEW_EVIDENCE_MISSING,
    # Visual verification could not run because the RENDERER was unavailable (init/crash/timeout,
    # missing native dep). Also "we could not verify" - never provider downtime. Distinct message,
    # same class.
    "reviewer_render_unavailable": FailureClass.REVIEW_EVIDENCE_MISSING,
    # The unified reviewer interaction ran to its round budget without an eligible verdict - an
    # incomplete/blocked review, not a provider outage. Zero tool calls and exhaustion are evidence
    # conditions, never provider-down.
    "reviewer_exhausted": FailureClass.REVIEW_EVIDENCE_MISSING,
}


def classify_api_failure(api_failure: str) -> FailureClass:
    s = (api_failure or "").lower()
    # EXACT match first, for markers whose meaning must never be guessed from a substring.
    # Markers arrive in TWO forms - the router wraps them (`<<API_FAILURE:x>>`) while the
    # reviewer sets them bare - so normalise the wrapper before comparing.
    _bare = s
    if _bare.startswith("<<api_failure:") and _bare.endswith(">>"):
        _bare = _bare[len("<<api_failure:"):-2].strip()
    if _bare in _EXACT_MARKER_CLASS:
        return _EXACT_MARKER_CLASS[_bare]
    # `<role>_non_transient` is the marker for terminal, NON-retryable failures - auth, insufficient
    # balance, invalid request, application defect, tool schema, policy rejection, invalid output,
    # quality failed (adapters.model_inference.failure_markers). It literally means "not transient",
    # yet it CONTAINS the substring "transient": without this guard the cascade below would match
    # that substring and call a broken, unretryable call retryable (PROVIDER_TRANSIENT), telling the
    # user "try again in a few minutes" for what will never succeed on retry. It is a non-retryable
    # system outage - the same PROVIDER_DOWN the default arm already assigns any api_failure that
    # matches nothing more specific (which is exactly where `non_transient` lands once the false
    # "transient" match is removed). Checked before the cascade so the substring can never win.
    if _bare == "non_transient" or _bare.endswith("_non_transient"):
        return FailureClass.PROVIDER_DOWN
    if "rate" in s:
        return FailureClass.PROVIDER_TRANSIENT
    if "timeout" in s or "transient" in s or "unavailable" in s or "empty_response" in s:
        return FailureClass.PROVIDER_TRANSIENT
    if "connection" in s or "down" in s:
        return FailureClass.DEPENDENCY_DOWN
    return FailureClass.PROVIDER_DOWN  # default: treat an unclassified api_failure as a system outage


# #
# User-facing (blameless) + operator (precise) + DB (coarse) mappings.
# #
_USER_MESSAGES: dict[FailureClass, str] = {
    FailureClass.REVIEW_EVIDENCE_MISSING: (
        "The review did not produce findings for all required quality criteria, so we could "
        "not verify your mesh. This is on our side, not a problem with your geometry or "
        "request. Please try again - we apologise for the inconvenience."
    ),
    FailureClass.PROVIDER_TRANSIENT: (
        "Some of our systems are temporarily experiencing issues, so we couldn't "
        "complete your mesh job right now. This is on our side, not a problem with "
        "your geometry or request. Please try again in a few minutes - we apologise "
        "for the inconvenience."
    ),
    FailureClass.PROVIDER_DOWN: (
        "One of the AI services this pipeline depends on is currently unavailable, "
        "so we couldn't complete your mesh job. This is on our side. Please try "
        "again shortly - we apologise for the inconvenience."
    ),
    FailureClass.DEPENDENCY_DOWN: (
        "We're having trouble reaching part of our infrastructure right now, so we "
        "couldn't complete your mesh job. This is a temporary issue on our side - "
        "please try again in a few minutes."
    ),
    FailureClass.RESOURCE: (
        "Your mesh job ran into a resource limit on our side before it could finish. "
        "This is not a problem with your request. Please try again shortly; if it "
        "keeps happening, let us know."
    ),
    FailureClass.INTERNAL: (
        "Something went wrong on our side while processing your mesh job. This is not "
        "a problem with your geometry or request. Please try again in a few minutes - "
        "we apologise for the inconvenience."
    ),
    FailureClass.NOT_AUTHORIZED: (
        "That geometry isn't available on this account, so we couldn't start the mesh "
        "job. If you expected it to be here, please upload it again."
    ),
    FailureClass.DATA_INTEGRITY: (
        "We couldn't confirm that the stored geometry is exactly the file you approved, "
        "so we stopped rather than mesh the wrong thing. This is on our side. Please "
        "upload the geometry again - we apologise for the inconvenience."
    ),
}

_FAILED_REASON: dict[FailureClass, str] = {
    # maps the rich class to the coarse DB FailedReason value (string, no migration)
    FailureClass.PROVIDER_TRANSIENT: "api_failure",
    FailureClass.PROVIDER_DOWN:      "api_failure",
    FailureClass.DEPENDENCY_DOWN:    "api_failure",
    FailureClass.RESOURCE:           "unhandled",
    FailureClass.INTERNAL:           "unhandled",
    # an EXISTING FailedReason value: a truthful new class needs no migration
    FailureClass.REVIEW_EVIDENCE_MISSING: "api_failure",
    # both map onto EXISTING FailedReason values - a truthful new class needs no migration
    FailureClass.NOT_AUTHORIZED:          "unhandled",
    FailureClass.DATA_INTEGRITY:          "unhandled",
}


def user_message_for(fc: FailureClass) -> str:
    return _USER_MESSAGES.get(fc, _USER_MESSAGES[FailureClass.INTERNAL])


def failed_reason_for(fc: FailureClass) -> str:
    return _FAILED_REASON.get(fc, "unhandled")


# #
# Dead-letter: a durable record of a job that died from a system failure, so it
# is inspectable/replayable instead of silently lost. Best-effort (the injected
# sink + structured log); never raises.
# #
def record_dead_letter(job_id: str, failure_class: FailureClass, dependency: str,
                       detail: str, *, extra: dict | None = None) -> None:
    rec = {
        "ts": datetime.now(UTC).isoformat(),
        "job_id": job_id,
        "failure_class": failure_class.value,
        "dependency": dependency,
        "detail": detail[:1000],
        **(extra or {}),
    }
    # Always log it (operators + Sentry-style scrapers pick this up). The log is the copy
    # that cannot fail, which is why the sink below is allowed to be best-effort.
    logger.error("DEAD_LETTER %s", json.dumps(rec, default=str))
    try:
        from meshpipeline.contracts.dead_letter import append as _append_dead_letter
        _append_dead_letter(rec)
    except Exception as exc:
        logger.warning("record_dead_letter: could not append to the dead-letter sink: %s", exc)
