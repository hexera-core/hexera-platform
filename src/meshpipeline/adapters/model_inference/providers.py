# Responsibility: Resolve a route target to its provider client, and classify that provider's errors.
# Boundaries: selection and classification; the call itself belongs to the router.
from __future__ import annotations

import logging

from meshpipeline.contracts.model_routing import FailureCategory, RouteTarget

logger = logging.getLogger(__name__)


def client_for(target: RouteTarget):
    if target.provider == "deepinfra":
        from meshpipeline.adapters.model_inference.deepinfra import get_deepinfra_client
        return get_deepinfra_client()
    if target.provider == "deepseek":
        from meshpipeline.adapters.model_inference.deepseek import get_deepseek_client
        return get_deepseek_client()
    raise ValueError(
        f"no adapter for provider {target.provider!r} (route target {target.label}). "
        "Configured providers: deepinfra, deepseek.")


def classify(exc: BaseException) -> FailureCategory:
    try:
        import openai
    except ImportError:
        openai = None  # type: ignore[assignment]

    if openai is not None:
 # never failover: ours, not theirs
        if isinstance(exc, openai.AuthenticationError):
            return FailureCategory.AUTH
        if isinstance(exc, openai.PermissionDeniedError):
            return FailureCategory.AUTH
        if isinstance(exc, openai.BadRequestError):
            # 400s carry both "your JSON is wrong" and "insufficient balance", depending on the
            # provider. Balance is not a transient provider fault either, so both stay here.
            text = str(exc).lower()
            if "balance" in text or "quota" in text or "credit" in text:
                return FailureCategory.INSUFFICIENT_BALANCE
            return FailureCategory.INVALID_REQUEST
        if isinstance(exc, openai.NotFoundError):
            # A model id that does not exist is a configuration defect; a model that is
            # temporarily withdrawn is not. We cannot tell them apart from a 404, so we take the
            # conservative reading: do NOT fail over, surface the misconfiguration.
            return FailureCategory.INVALID_REQUEST
 # transient: the provider is having a bad moment
        if isinstance(exc, openai.RateLimitError):
            return FailureCategory.RATE_LIMIT
        if isinstance(exc, openai.APITimeoutError):
            return FailureCategory.TIMEOUT
        if isinstance(exc, openai.APIConnectionError):
            return FailureCategory.CONNECTION
        if isinstance(exc, openai.InternalServerError):
            return FailureCategory.SERVICE_UNAVAILABLE

    if isinstance(exc, (TimeoutError,)):
        return FailureCategory.TIMEOUT
    name = type(exc).__name__
    if any(k in name for k in ("Timeout",)):
        return FailureCategory.TIMEOUT
    if any(k in name for k in ("ConnectError", "ConnectionError", "RemoteProtocol")):
        return FailureCategory.CONNECTION
    # Unknown exceptions are APPLICATION DEFECTS, not provider weather. Defaulting the unknown
    # to a transient category would make every bug in this product failover-eligible.
    logger.warning("model call raised an unclassified %s - treating as an application defect "
                   "(no failover)", name)
    return FailureCategory.APPLICATION_DEFECT
