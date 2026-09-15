# Responsibility: Turn a role's neutral sampling intent into the parameters ONE provider accepts.
# Owns: the per-role SamplingSpec, and the per-provider translation of it.
# Boundaries: parameter shape only - it chooses no model, makes no call, and decides no retry.
# Collaborates with: router.py, which calls it once per attempt against the resolved target.

# WHY THIS EXISTS. The per-role kwargs used to be module-level dicts in deepinfra.py and
# deepseek.py, built at import from role settings. Both providers speak the OpenAI wire format,
# so one dict per role was enough. It stops being enough the moment a route can point somewhere
# else: OpenAI rejects `min_p` and `top_k` as unknown parameters, and Anthropic rejects
# `temperature`, `top_p` and `top_k` outright (400) because depth there is an `effort` setting,
# not a sampler. A dict frozen at import cannot express that, so the translation moves here and
# happens per TARGET.
from __future__ import annotations

from dataclasses import dataclass

from meshpipeline.contracts.model_routing import RouteTarget

_THINKING_OFF = {"thinking": {"type": "disabled"}}


@dataclass(frozen=True)
class SamplingSpec:
    """A role's sampling INTENT, in no provider's vocabulary.

    Every sampling field is optional because "this role does not express an opinion" and "this
    role wants 0.0" are different statements, and only the first may be dropped from a request.
    """

    max_tokens: int
    temperature: float | None = None
    top_p: float | None = None
    min_p: float | None = None
    top_k: int | None = None
    presence_penalty: float | None = None
    stream: bool = False
    thinking_off: bool = False


def spec_for(role: str) -> SamplingSpec:
    # Imported lazily and inside the function: the role settings modules pull in the agents'
    # config trees, exactly as routes._collect() does for the same reason.
    import meshpipeline.agents.builder.settings as bcfg
    import meshpipeline.agents.intake.settings as icfg
    import meshpipeline.agents.reviewer.settings as rcfg
    import meshpipeline.engines.snappy.settings as pcfg
    import meshpipeline.settings.providers as provcfg

    if role == "builder":
        return SamplingSpec(max_tokens=bcfg.BUILDER_MAX_TOKENS,
                            temperature=bcfg.BUILDER_TEMPERATURE,
                            top_p=bcfg.BUILDER_TOP_P, min_p=bcfg.BUILDER_MIN_P, stream=True)
    if role == "planner":
        return SamplingSpec(max_tokens=pcfg.PLANNER_MAX_TOKENS,
                            temperature=pcfg.PLANNER_TEMPERATURE,
                            top_p=pcfg.PLANNER_TOP_P, min_p=pcfg.PLANNER_MIN_P, stream=True)
    if role == "visual_reviewer":
        # temperature 0.7 / top_p 0.8 / top_k 20 are Qwen's OWN recommended parameters for the
        # Thinking variant, not tuning this project arrived at. Changing them needs a source.
        return SamplingSpec(max_tokens=rcfg.REVIEWER_MAX_TOKENS,
                            temperature=rcfg.REVIEWER_TEMPERATURE,
                            top_p=rcfg.REVIEWER_TOP_P, top_k=rcfg.REVIEWER_TOP_K,
                            presence_penalty=rcfg.REVIEWER_PRESENCE_PENALTY, stream=True)
    if role == "intake":
        # DeepSeek's API ACCEPTS min_p, but whether its backend actually applies it is
        # provider-dependent (DeepInfra definitely does), so sending it is deliberate, not proven.
        return SamplingSpec(max_tokens=icfg.INTAKE_MAX_TOKENS,
                            temperature=icfg.INTAKE_TEMPERATURE,
                            min_p=icfg.INTAKE_MIN_P, thinking_off=True)
    if role == "summarizer":
        # Reading provcfg rather than the role's own settings module is not an oversight:
        # agent_tools.shared.settings defines SUMMARIZER_TEMPERATURE/SUMMARIZER_MAX_TOKENS from
        # the SAME env names, so the two can never diverge and neither is the "real" one.
        return SamplingSpec(max_tokens=provcfg.SEARCH_SUMMARIZER_MAX_TOKENS,
                            temperature=provcfg.SEARCH_SUMMARIZER_TEMPERATURE,
                            thinking_off=True)
    raise KeyError(
        f"no sampling spec for role {role!r}; roles that have one: "
        "['builder', 'intake', 'planner', 'summarizer', 'visual_reviewer']")


def _openai_wire(target: RouteTarget, spec: SamplingSpec) -> dict:
    """DeepInfra and DeepSeek: the OpenAI chat-completions shape, with vendor sampling
    extensions carried in extra_body because they are not top-level OpenAI fields."""
    kw: dict = {"model": target.model}
    if spec.temperature is not None:
        kw["temperature"] = spec.temperature
    kw["max_tokens"] = spec.max_tokens
    if spec.top_p is not None:
        kw["top_p"] = spec.top_p
    if spec.presence_penalty is not None:
        kw["presence_penalty"] = spec.presence_penalty

    extra: dict = {}
    if spec.thinking_off:
        extra.update(_THINKING_OFF)
    if spec.min_p is not None:
        extra["min_p"] = spec.min_p
    if spec.top_k is not None:
        extra["top_k"] = spec.top_k

    if spec.stream:
        kw["stream"] = True
        kw["stream_options"] = {"include_usage": True}
    if extra:
        kw["extra_body"] = extra
    return kw


def _openai_native(target: RouteTarget, spec: SamplingSpec) -> dict:
    """OpenAI proper. Same wire format, STRICTER parameter set: an unknown top-level or
    extra_body field is a 400, not an ignored hint. So the vendor sampler extensions
    (min_p, top_k) and DeepSeek's thinking switch are dropped rather than forwarded."""
    # UNVERIFIED against the real endpoint - the WHOLE parameter set below, not only the output
    # cap. No OPENAI_API_KEY was available while this was written, so nothing here was exercised
    # against the live API. Two open questions, of equal standing: whether `max_tokens` (what Step
    # 3 of the plan specifies) is still accepted or has become `max_completion_tokens`, and
    # whether `temperature` and `top_p` are accepted at all, since several OpenAI reasoning-class
    # models reject a non-default `temperature` outright. Do not read the emphasis on the cap as
    # evidence that the samplers were checked; none of it was.
    kw: dict = {"model": target.model, "max_tokens": spec.max_tokens}
    if spec.temperature is not None:
        kw["temperature"] = spec.temperature
    if spec.top_p is not None:
        kw["top_p"] = spec.top_p
    if spec.presence_penalty is not None:
        kw["presence_penalty"] = spec.presence_penalty
    if spec.stream:
        kw["stream"] = True
        kw["stream_options"] = {"include_usage": True}
    return kw


_BUILDERS = {
    "deepinfra": _openai_wire,
    "deepseek": _openai_wire,
    "openai": _openai_native,
}


def kwargs_for(target: RouteTarget, spec: SamplingSpec) -> dict:
    try:
        build = _BUILDERS[target.provider]
    except KeyError:
        raise KeyError(
            f"no call-kwargs builder for provider {target.provider!r}; providers that have "
            f"one: {sorted(_BUILDERS)}") from None
    return build(target, spec)
