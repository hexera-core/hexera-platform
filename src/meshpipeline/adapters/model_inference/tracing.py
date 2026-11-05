# Responsibility: Wrap the provider client so calls are traced when tracing is configured.
# Boundaries: optional instrumentation; with nothing configured it is transparent and changes no behaviour.
from __future__ import annotations

import logging

import meshpipeline.settings.providers as provcfg

logger = logging.getLogger(__name__)

_langfuse_enabled: bool | None = None
_langfuse_check_done: bool = False


def _is_langfuse_enabled() -> bool:
    global _langfuse_enabled, _langfuse_check_done
    if _langfuse_enabled is None:
        keys_set = bool(provcfg.LANGFUSE_PUBLIC_KEY and provcfg.LANGFUSE_SECRET_KEY)
        if not keys_set:
            _langfuse_enabled = False
            logger.info("Langfuse tracing disabled - no keys set")
        else:
            if not _langfuse_check_done:
                _langfuse_check_done = True
                try:
                    import urllib.request as _urlreq
                    _req = _urlreq.Request(
                        f"{provcfg.LANGFUSE_HOST.rstrip('/')}/api/public/health",
                        headers={"Authorization": f"Basic {provcfg.LANGFUSE_PUBLIC_KEY}:{provcfg.LANGFUSE_SECRET_KEY}"},
                    )
                    with _urlreq.urlopen(_req, timeout=3) as _resp:
                        _langfuse_enabled = _resp.status in (200, 404)
                except Exception as _hc_exc:
                    logger.warning(
                        "Langfuse health check failed - disabling tracing to prevent 401 flooding: %s",
                        _hc_exc,
                    )
                    _langfuse_enabled = False
                else:
                    if _langfuse_enabled:
                        logger.info("Langfuse tracing enabled - host=%s", provcfg.LANGFUSE_HOST)
    return bool(_langfuse_enabled)


def get_async_openai(api_key: str, base_url: str, timeout=None):
    import httpx
    if timeout is None:
        httpx_timeout = None
    elif isinstance(timeout, httpx.Timeout):
        httpx_timeout = timeout
    else:
        httpx_timeout = httpx.Timeout(timeout)

    if _is_langfuse_enabled():
        try:
            from langfuse.openai import AsyncOpenAI as TracedAsyncOpenAI
            kwargs: dict = {"api_key": api_key, "base_url": base_url}
            if httpx_timeout is not None:
                kwargs["timeout"] = httpx_timeout
            return TracedAsyncOpenAI(**kwargs)
        except Exception as exc:
            logger.warning("Langfuse init failed - falling back to untraced: %s", exc)

    from openai import AsyncOpenAI
    kwargs2: dict = {"api_key": api_key, "base_url": base_url}
    if httpx_timeout is not None:
        kwargs2["timeout"] = httpx_timeout
    return AsyncOpenAI(**kwargs2)


def langfuse_kwargs(
    session_id: str | None = None,
    user_id: str | None = None,
    name: str | None = None,
) -> dict:
    if not _is_langfuse_enabled():
        return {}
    out = {}
    if session_id:
        out["session_id"] = str(session_id)
    if user_id:
        out["user_id"] = str(user_id)
    if name:
        out["name"] = name
    return out
