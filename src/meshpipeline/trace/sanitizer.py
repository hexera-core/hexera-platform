# Responsibility: Scrub every public payload, in BOTH modes.
# Boundaries: the last pass over a payload: secrets and provider identity are removed regardless of mode.
from __future__ import annotations

import math
import re
from typing import Any, Final

REDACTED: Final = "[redacted]"
MODEL_MARK: Final = "[model]"
TRUNCATED: Final = "…[truncated]"

# Bounds. Generous enough for a real tool argument, small enough that a runaway
# structure cannot become the page.
MAX_STRING: Final = 4_000
MAX_ITEMS: Final = 100
MAX_KEYS: Final = 60
MAX_DEPTH: Final = 6
MAX_TOTAL: Final = 60_000

# Key names that ARE a credential or a private location, whatever they hold.
_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|bearer|authorization|auth[_-]?header|"
    r"cookie|session[_-]?id|credential|private[_-]?key|access[_-]?key|signature|"
    r"connection[_-]?string|dsn|database[_-]?url|redis[_-]?url|conn[_-]?str)",
    re.I)

# Keys that name the model, the provider, or the route that reached it. Removed in
# BOTH modes - see the module docstring.
_IDENTITY_KEY = re.compile(
    r"^(model|model[_-]?name|model[_-]?id|model[_-]?version|model[_-]?alias|"
    r"provider|provider[_-]?name|provider[_-]?id|deployment|deployment[_-]?name|"
    r"route|router|routing|system[_-]?fingerprint|fingerprint|fallback[_-]?model|"
    r"inference[_-]?backend|engine[_-]?backend|llm|llm[_-]?model)$",
    re.I)

# Keys whose VALUE is a filesystem location we must not publish verbatim.
_PATH_KEY = re.compile(r"(path|dir|directory|root|workspace|location|file)$", re.I)

_VALUE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # bearer/authorization headers wherever they appear in text
    (re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{8,}"), f"Bearer {REDACTED}"),
    # signed URLs: any URL carrying a signature/credential/expiry query
    (re.compile(r"https?://[^\s\"']*[?&](?:X-Amz-Signature|X-Goog-Signature|"
                r"Signature|sig|token|access_key|AWSAccessKeyId|Expires)=[^\s\"'&]+[^\s\"']*",
                re.I), REDACTED),
    # THIS PRODUCT'S OWN KEYS. contracts/api_key.py makes `hx_live_` visible in the credential
    # precisely so a leaked key is recognisable "by a secret scanner, by a log filter, by a
    # person reading a paste" - and this is the log filter. The shape is
    # hx_live_<12 base62>_<url-safe secret>, and the SECRET HALF IS REQUIRED here: `key_prefix`
    # alone is public, it is what /settings/api-keys prints in every row, and redacting it would
    # blank the one thing that lets somebody tell their keys apart.
    (re.compile(r"\bhx_live_[A-Za-z0-9]{12}_[A-Za-z0-9_\-]{16,}"), REDACTED),
    # long opaque credentials with a recognisable prefix
    (re.compile(r"\b(?:sk|pk|rk|api|tok|ghp|gho|xox[abps])[-_][A-Za-z0-9_\-]{16,}"), REDACTED),
    # jwt
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), REDACTED),
    # credentials embedded in a URL
    (re.compile(r"\b([a-z][a-z0-9+.\-]*://)[^\s/:@]+:[^\s/@]+@"), r"\1" + REDACTED + "@"),
    # private service endpoints
    (re.compile(r"\b(?:redis|postgres(?:ql)?|amqp|mongodb)://[^\s\"']+", re.I), REDACTED),
    # absolute host paths, incl. the workspace roots this system actually uses
    (re.compile(r"/srv/workspaces/[^\s\"',)]*"), "<workspace>"),
    (re.compile(r"(?:/home/[^/\s\"',)]+|/Users/[^/\s\"',)]+|/root)(?=[/\s\"',)]|$)"), "<home>"),
    (re.compile(r"\b[A-Za-z]:\\\\[^\s\"',)]+"), "<path>"),
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _identity_terms() -> tuple[re.Pattern[str], ...]:
    terms: set[str] = set()
    try:
        from meshpipeline.settings import policy as _p
        for attr in dir(_p):
            if not attr.isupper():
                continue
            if not any(w in attr for w in ("MODEL", "PROVIDER", "DEPLOYMENT")):
                continue
            val = getattr(_p, attr, None)
            if isinstance(val, str) and len(val.strip()) >= 3:
                terms.add(val.strip())
            elif isinstance(val, dict):
                for v in val.values():
                    if isinstance(v, str) and len(v.strip()) >= 3:
                        terms.add(v.strip())
    except Exception:      # settings must never be able to break sanitisation
        pass
    pats = [re.compile(re.escape(t), re.I) for t in sorted(terms, key=len, reverse=True)]

    # A model's VERSION TAIL: segments that are version-like (contain a digit) or a
    # known size/variant qualifier, repeating and interleaving, so "gpt-4o-mini-2024"
    # and "claude opus 4.5" are consumed whole - while "GLM 5.2 I will read the brief"
    # keeps its sentence. Over-redaction that eats a model's actual prose is its own
    # kind of dishonesty.
    _TAIL = (r"(?:[-_/ ](?:[a-z]*[0-9][a-z0-9.]*"
             r"|mini|turbo|pro|max|lite|instruct|chat|base|preview|opus|sonnet|haiku"
             r"|flash|ultra|nano|small|medium|large|thinking))*")

    # DISTINCTIVE families: the name alone identifies a model, so it goes wherever it
    # appears. Collision with engineering vocabulary is negligible.
    pats.append(re.compile(
        r"\b(?:gpt|glm|claude|gemini|llama|mistral|mixtral|qwen|kimi|deepseek|"
        r"command[- ]?r|jamba)\b" + _TAIL, re.I))

    # AMBIGUOUS stems: real model families whose names are also ordinary technical
    # words. `phi` is the OpenFOAM face-flux field this repository itself writes into
    # fvSchemes ("div(phi,U)"); `yi` is a coordinate component; nova/titan/grok are
    # plain English. Redacting them bare corrupts genuine engineering content on the
    # public page, so they are only redacted WITH a version-like tail - "phi-4" is a
    # model, "div(phi,U)" is a discretisation scheme. A deployment that actually runs
    # one of these is covered regardless, because its configured identity is matched
    # verbatim by the settings-derived patterns above.
    _QUAL = (r"(?:[-_/ ](?:mini|turbo|pro|max|lite|instruct|chat|base|preview|opus"
             r"|sonnet|haiku|flash|ultra|nano|small|medium|large|thinking))")
    pats.append(re.compile(
        r"\b(?:phi|yi|nova|titan|grok)\b"
        # at least one VERSION-like segment must follow, though named qualifiers may
        # come first ("nova-pro-1"). Without a version this is ordinary vocabulary.
        + _QUAL + r"*" + r"(?:[-_/ ][a-z]*[0-9][a-z0-9.]*)" + _TAIL, re.I))
    pats.append(re.compile(
        r"\b(?:openai|anthropic|google|deepmind|mistralai|moonshotai|zai-org|"
        r"together(?:ai)?|groq|fireworks|bedrock|vertex|azure openai|openrouter|"
        r"deepinfra|novita|hyperbolic)\b", re.I))
    pats.append(re.compile(r"\bfp_[0-9a-f]{8,}\b"))          # system fingerprints
    return tuple(pats)


_IDENTITY_TERMS = _identity_terms()


def scrub_text(text: str, *, limit: int = MAX_STRING) -> str:
    if not isinstance(text, str):
        text = str(text)
    for pat, repl in _VALUE_PATTERNS:
        text = pat.sub(repl, text)
    for pat in _IDENTITY_TERMS:
        text = pat.sub(MODEL_MARK, text)
    text = _CONTROL.sub("", text)
    # collapse a run of marks left by adjacent identity tokens
    text = re.sub(r"(?:\[model\][\s,·/|-]*){2,}", MODEL_MARK + " ", text)
    if len(text) > limit:
        text = text[:limit] + TRUNCATED
    return text


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


def _is_identity_key(key: str) -> bool:
    return bool(_IDENTITY_KEY.match(key.strip()))


def sanitize(value: Any, *, _depth: int = 0, _seen: set[int] | None = None) -> Any:
    seen = _seen if _seen is not None else set()

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # NaN and infinity are not measurements and do not survive JSON
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, BaseException):
        # the message, never the traceback or the exception object
        return scrub_text(f"{type(value).__name__}: {value}", limit=400)

    if _depth >= MAX_DEPTH:
        return TRUNCATED

    if isinstance(value, (list, tuple, set, frozenset)):
        if id(value) in seen:
            return TRUNCATED
        seen = seen | {id(value)}
        items = list(value)[:MAX_ITEMS]
        out = [sanitize(v, _depth=_depth + 1, _seen=seen) for v in items]
        if len(value) > MAX_ITEMS:
            out.append(TRUNCATED)
        return out

    if isinstance(value, dict):
        if id(value) in seen:
            return TRUNCATED
        seen = seen | {id(value)}
        out_d: dict[str, Any] = {}
        for k, v in list(value.items())[:MAX_KEYS]:
            if not isinstance(k, str):
                continue
            key = _CONTROL.sub("", k)[:120]
            if _is_identity_key(key):
                continue                       # model/provider identity: gone, both modes
            if _is_secret_key(key):
                out_d[key] = REDACTED
                continue
            if _PATH_KEY.search(key) and isinstance(v, str):
                out_d[key] = scrub_text(v, limit=512)
                continue
            out_d[key] = sanitize(v, _depth=_depth + 1, _seen=seen)
        return out_d

    # anything else - a provider response object, a file handle, a model - is not
    # data we designed a public shape for.
    return None


def sanitize_payload(value: Any) -> Any:
    out = sanitize(value)
    try:
        import json
        if len(json.dumps(out, default=str)) > MAX_TOTAL:
            return {"note": TRUNCATED}
    except (TypeError, ValueError):
        return None
    return out
