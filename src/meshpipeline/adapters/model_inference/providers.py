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
    if target.provider == "openai":
        from meshpipeline.adapters.model_inference.openai_api import get_openai_client
        return get_openai_client()
    raise ValueError(
        f"no adapter for provider {target.provider!r} (route target {target.label}). "
        "Configured providers: deepinfra, deepseek, openai.")


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
        # 402 Payment Required is how DeepSeek answers an exhausted account. The SDK has no
        # subclass for it, so it arrives as a bare APIStatusError and used to fall all the way
        # through to APPLICATION_DEFECT below - logging "unclassified APIStatusError" and making
        # an unpaid bill look like a bug in this product. Checked after every specific subclass
        # above (each of which IS an APIStatusError) so this can only catch what they do not.
        if isinstance(exc, openai.APIStatusError) and getattr(exc, "status_code", None) == 402:
            return FailureCategory.INSUFFICIENT_BALANCE
 # transient: the provider is having a bad moment
        if isinstance(exc, openai.RateLimitError):
            return FailureCategory.RATE_LIMIT
        if isinstance(exc, openai.APITimeoutError):
            return FailureCategory.TIMEOUT
        if isinstance(exc, openai.APIConnectionError):
            return FailureCategory.CONNECTION
        if isinstance(exc, openai.InternalServerError):
            return FailureCategory.SERVICE_UNAVAILABLE
        # THE PROVIDER BROKE ITS OWN STREAM. A bare APIError - not a status error, not a
        # connection error - is what the SDK raises when a response it had already begun
        # streaming carries an error event or ends mid-way ("An error occurred during
        # streaming"). The request was accepted (HTTP 200, first chunk received) and the provider
        # failed after that: provider weather, retryable and failover-eligible, exactly like a
        # 5xx. Left to fall through to APPLICATION_DEFECT below it ended the job on the spot -
        # nine shapes of the full cfMesh sweep and the first console run on dev, each with a
        # valid mesh already built and one model call away from delivery.
        if type(exc) is openai.APIError:
            # Say WHAT the provider sent. Two console runs and nine sweep shapes died on this
            # path with nothing in the log but the type name; the text is the only evidence of
            # whether the provider is unwell or refusing something about the request.
            logger.warning("provider broke its own stream (%s): %s", type(exc).__name__,
                           str(exc)[:300])
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
    logger.warning("model call raised an unclassified %s (%s) - treating as an application "
                   "defect (no failover)", name, str(exc)[:200])
    return FailureCategory.APPLICATION_DEFECT
