# OpenAI provider and per-target call kwargs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenAI callable as a fourth provider, and move per-role sampling parameters from static module-level dicts to per-target builders, so a route can point at a provider that rejects `min_p`/`top_k`.

**Architecture:** A new `call_kwargs.py` owns one neutral `SamplingSpec` per role and translates it to each provider's accepted parameter set. `router.py:_for_target()` — which already builds per-target kwargs, but only substitutes the model id — becomes the single call site. The DeepInfra and DeepSeek arms must produce **byte-identical** dicts to today's `BUILDER_CALL_KWARGS` / `REVIEWER_CALL_KWARGS` / `INTAKE_CALL_KWARGS` / `SUMMARIZER_CALL_KWARGS`, which is what makes this refactor provably behaviour-preserving.

**Tech Stack:** Python 3.11, `openai` SDK (already a dependency — OpenAI is wire-compatible with the existing adapters), pytest, FastAPI, GCP Secret Manager.

**Spec:** [`docs/superpowers/specs/2026-09-14-runtime-model-routing-design.md`](../specs/2026-09-14-runtime-model-routing-design.md) — this plan implements **stage 1 of §11** only.

## Global Constraints

- **No default may move.** `settings/inventory.py:128` `ROUTE_MATRIX` is not edited by this plan. Every role keeps its current provider and model. (Spec §2, §4.)
- **DeepInfra and DeepSeek behaviour must not change.** Their kwargs are asserted byte-identical to today's dicts. Any diff is a bug, not an improvement.
- **No vendor SDK outside its adapter.** `tests/unit/hygiene/test_vendor_sdk_confinement.py` must keep passing unedited. OpenAI is already allowlisted for `providers.py`; the new client module needs an entry.
- **No product module may import a model-provider SDK.** `test_no_product_module_imports_a_model_provider_sdk` (line 104) must keep passing.
- **Prices are quoted, never derived.** `pricing.py` feeds customer billing. A price that cannot be confirmed is left absent so `unpriced_route_models()` reports it, per the existing convention at `pricing.py:24`.
- **Run tests with:** `PYTHONPATH=src python -m pytest <path> -q -p no:cacheprovider`. This worktree has no `.venv`; `make setup` creates one. The unit tier needs `sqlalchemy`, `vtk`, `gmsh`, `pyamg` to collect fully.

---

### Task 1: OpenAI settings, client and `client_for` branch

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/openai_api.py`
- Modify: `src/meshpipeline/settings/providers.py:22-27` (values), `:end` (`LLM_PROVIDER_KEY_ENV`)
- Modify: `src/meshpipeline/settings/inventory.py:246` (declare the two new vars)
- Modify: `src/meshpipeline/adapters/model_inference/providers.py:12-21` (`client_for`)
- Modify: `tests/unit/hygiene/test_vendor_sdk_confinement.py` (allowlist entry)
- Test: `tests/unit/infra/test_openai_provider.py`

Named `openai_api.py`, not `openai.py`: a module named `openai` inside the package is legal under absolute imports but makes every `import openai` in the package ambiguous to a reader.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `get_openai_client()` returning an `AsyncOpenAI`; `provcfg.OPENAI_API_KEY: str`, `provcfg.OPENAI_BASE_URL: str`; `client_for(RouteTarget(provider="openai", ...))` resolving.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_openai_provider.py
# Responsibility: Verify the openai provider label resolves to a credentialled client, and refuses without one.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import providers
from meshpipeline.contracts.model_routing import RouteTarget
from meshpipeline.settings.env import ConfigurationError


def _target(model: str = "gpt-5.6-terra") -> RouteTarget:
    return RouteTarget(provider="openai", model=model, account="default",
                       circuit_group="openai")


def test_the_openai_label_resolves_to_a_client(monkeypatch):
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "sk-test")
    client = providers.client_for(_target())
    assert client.chat.completions is not None


def test_an_uncredentialled_openai_route_refuses_with_the_variable_name(monkeypatch):
    monkeypatch.setattr("meshpipeline.settings.providers.OPENAI_API_KEY", "")
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        providers.client_for(_target())


def test_an_unknown_provider_names_every_configured_one():
    unknown = RouteTarget(provider="cohere", model="x", account="default",
                          circuit_group="x")
    with pytest.raises(ValueError, match="openai"):
        providers.client_for(unknown)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_openai_provider.py -q -p no:cacheprovider`
Expected: FAIL — `ValueError: no adapter for provider 'openai'`.

- [ ] **Step 3: Add the settings**

In `src/meshpipeline/settings/providers.py`, after the `DEEPINFRA_*` block (line 27):

```python
OPENAI_API_KEY: str  = optional_env("OPENAI_API_KEY", "")
OPENAI_BASE_URL: str = optional_env("OPENAI_BASE_URL", "https://api.openai.com/v1")
```

and in `LLM_PROVIDER_KEY_ENV` at the end of the same file, add one entry:

```python
    "openai": "OPENAI_API_KEY",
```

`LLM_PROVIDER_KEY_ENV` is read by `routes.missing_provider_credentials()` and by
`inference_overrides._supported_providers()`, so this one line is what makes `openai` a provider an
operator may name in `MODEL_PRICE_OVERRIDES`. It is not sufficient on its own:
`missing_provider_credentials()` must resolve the credential's VALUE through the registry too —
reading it by the name the registry supplies — rather than through a second hardcoded env-name-to-
value map, which is a copy of the registry and reports every provider it was never updated for as
uncredentialed. (This paragraph originally read "No other registration exists or is needed." That
was wrong: a second map did exist in `missing_provider_credentials()`, the implementer followed
this sentence faithfully, and an `openai` route was consequently always reported as missing its
key and refused boot under `APP_ENV=prod`. Caught by the final whole-branch review and fixed.)

- [ ] **Step 4: Declare them in the inventory**

In `src/meshpipeline/settings/inventory.py`, in the same group as `DEEPSEEK_MODEL` (line 246):

```python
        EnvVar("OPENAI_API_KEY", "", secret=True, help="required only when a route resolves to the openai provider"),
        EnvVar("OPENAI_BASE_URL", "https://api.openai.com/v1", help="override only to reach an OpenAI-compatible gateway"),
```

`secret=True` but **not** `required=True`, following `TAVILY_API_KEY` (line 435) rather than
`DEEPSEEK_API_KEY` (line 244). `required=True` means "a real mesh job cannot run without this",
which is false for a deployment whose routes never resolve to OpenAI — and making it true would
make a single-provider deployment impossible, which spec §8 explicitly rejects. `secret=True` is
what keeps the value out of a generated `.env` and puts the name in the deploy gate's roster
(Task 7).

- [ ] **Step 5: Write the client module**

```python
# src/meshpipeline/adapters/model_inference/openai_api.py
# Responsibility: Build the OpenAI client a route targeting the openai provider calls through.
# Boundaries: client construction and credentials; no routing, no retries, no sampling policy.
from __future__ import annotations

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.model_inference.tracing import get_async_openai


def get_openai_client():
    if not provcfg.OPENAI_API_KEY:
        from meshpipeline.settings.env import ConfigurationError
        raise ConfigurationError(
            "OPENAI_API_KEY is not set, but a route targets the openai provider. Set it, or "
            "point the route elsewhere. (Startup validation reports this for the enabled profile.)")
    return get_async_openai(api_key=provcfg.OPENAI_API_KEY, base_url=provcfg.OPENAI_BASE_URL)
```

There is deliberately no call-kwargs dict here, unlike `deepinfra.py` and `deepseek.py`. Task 2
moves that concern out of the client modules entirely.

- [ ] **Step 6: Add the `client_for` branch**

In `src/meshpipeline/adapters/model_inference/providers.py`, inside `client_for` before the
`raise`:

```python
    if target.provider == "openai":
        from meshpipeline.adapters.model_inference.openai_api import get_openai_client
        return get_openai_client()
```

and update the final message so it lists what is actually configured:

```python
    raise ValueError(
        f"no adapter for provider {target.provider!r} (route target {target.label}). "
        "Configured providers: deepinfra, deepseek, openai.")
```

- [ ] **Step 7: Allowlist the new module for the `openai` SDK**

In `tests/unit/hygiene/test_vendor_sdk_confinement.py`, add to the `"openai"` set:

```python
        "adapters/model_inference/openai_api.py",
```

`openai_api.py` does not import `openai` directly today — it goes through `tracing.get_async_openai`
— but the entry is added now so Task 4's typed-error work does not look like a seam leak later.

- [ ] **Step 8: Run the tests**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_openai_provider.py tests/unit/hygiene/test_vendor_sdk_confinement.py -q -p no:cacheprovider`
Expected: PASS, all four tests.

- [ ] **Step 9: Verify the live parameter surface**

This is the one fact this plan does not encode from memory. With a funded key exported as
`OPENAI_API_KEY`, run:

```bash
curl -s https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6-terra","messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
  | head -c 400
```

Record which of `max_tokens` / `max_completion_tokens` is accepted and whether `min_p` or `top_k`
produce a 400. Task 4 encodes the answer. If the call returns `unsupported_parameter` naming
`max_tokens`, Task 4 uses `max_completion_tokens`.

- [ ] **Step 10: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/openai_api.py \
        src/meshpipeline/settings/providers.py src/meshpipeline/settings/inventory.py \
        src/meshpipeline/adapters/model_inference/providers.py \
        tests/unit/hygiene/test_vendor_sdk_confinement.py \
        tests/unit/infra/test_openai_provider.py
git commit -m "Make openai a provider a route may target"
```

---

### Task 2: `SamplingSpec` and `kwargs_for`, at parity with today

**Files:**
- Create: `src/meshpipeline/adapters/model_inference/call_kwargs.py`
- Test: `tests/unit/infra/test_call_kwargs_parity.py`

This task adds the new module and proves it reproduces today's dicts exactly. It changes no
behaviour — `router.py` still uses the old dicts until Task 3.

**Interfaces:**
- Consumes: `RouteTarget` from `contracts/model_routing.py`.
- Produces:
  - `SamplingSpec(temperature: float | None, top_p: float | None, min_p: float | None, top_k: int | None, presence_penalty: float | None, max_tokens: int, stream: bool, thinking_off: bool)` — frozen dataclass, all sampling fields optional.
  - `spec_for(role: str) -> SamplingSpec` where role ∈ `{"builder","planner","visual_reviewer","intake","summarizer"}`.
  - `kwargs_for(target: RouteTarget, spec: SamplingSpec) -> dict` — includes `"model": target.model`.

- [ ] **Step 1: Write the failing parity test**

```python
# tests/unit/infra/test_call_kwargs_parity.py
# Responsibility: Verify the per-target kwargs builder reproduces the shipped dicts exactly.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.contracts.model_routing import RouteTarget


def _target(provider: str, model: str) -> RouteTarget:
    return RouteTarget(provider=provider, model=model, account="default",
                       circuit_group=provider)


def _shipped(role: str) -> dict:
    """The dicts as they exist today, read from the modules that own them."""
    from meshpipeline.adapters.model_inference import deepinfra, deepseek
    if role == "builder":
        return dict(deepinfra.BUILDER_CALL_KWARGS)
    if role == "planner":
        return dict(deepinfra._planner_call_kwargs())
    if role == "visual_reviewer":
        return dict(deepinfra.REVIEWER_CALL_KWARGS)
    if role == "intake":
        return dict(deepseek.INTAKE_CALL_KWARGS)
    if role == "summarizer":
        return dict(deepseek.SUMMARIZER_CALL_KWARGS)
    raise AssertionError(role)


ROLE_TARGET = {
    "builder":         ("deepinfra", "zai-org/GLM-5.2"),
    "planner":         ("deepinfra", "zai-org/GLM-5.2"),
    "visual_reviewer": ("deepinfra", "Qwen/Qwen3-VL-235B-A22B-Thinking"),
    "intake":          ("deepseek",  "deepseek-v4-pro"),
    "summarizer":      ("deepseek",  "deepseek-v4-flash"),
}


@pytest.mark.parametrize("role", sorted(ROLE_TARGET))
def test_the_builder_reproduces_the_shipped_kwargs_exactly(role):
    provider, model = ROLE_TARGET[role]
    built = ck.kwargs_for(_target(provider, model), ck.spec_for(role))
    expected = _shipped(role)
    expected["model"] = model
    assert built == expected, (
        f"{role}: the per-target builder changed the call. This refactor must be "
        f"behaviour-preserving for deepinfra and deepseek.")


def test_an_unknown_role_is_refused_rather_than_defaulted():
    with pytest.raises(KeyError, match="summariser"):
        ck.spec_for("summariser")   # British spelling is not a role
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_call_kwargs_parity.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'meshpipeline.adapters.model_inference.call_kwargs'`.

- [ ] **Step 3: Write the module**

```python
# src/meshpipeline/adapters/model_inference/call_kwargs.py
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
        return SamplingSpec(max_tokens=rcfg.REVIEWER_MAX_TOKENS,
                            temperature=rcfg.REVIEWER_TEMPERATURE,
                            top_p=rcfg.REVIEWER_TOP_P, top_k=rcfg.REVIEWER_TOP_K,
                            presence_penalty=rcfg.REVIEWER_PRESENCE_PENALTY, stream=True)
    if role == "intake":
        return SamplingSpec(max_tokens=icfg.INTAKE_MAX_TOKENS,
                            temperature=icfg.INTAKE_TEMPERATURE,
                            min_p=icfg.INTAKE_MIN_P, thinking_off=True)
    if role == "summarizer":
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


_BUILDERS = {
    "deepinfra": _openai_wire,
    "deepseek": _openai_wire,
}


def kwargs_for(target: RouteTarget, spec: SamplingSpec) -> dict:
    try:
        build = _BUILDERS[target.provider]
    except KeyError:
        raise KeyError(
            f"no call-kwargs builder for provider {target.provider!r}; providers that have "
            f"one: {sorted(_BUILDERS)}") from None
    return build(target, spec)
```

- [ ] **Step 4: Run the parity test**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_call_kwargs_parity.py -q -p no:cacheprovider`
Expected: PASS, six tests.

If a role fails, the assertion prints both dicts. **Fix `call_kwargs.py` to match the shipped
dict — never edit the shipped dict to match.** The key ordering differences do not matter (`==`
on dicts ignores order); a differing value or a missing key does.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/call_kwargs.py \
        tests/unit/infra/test_call_kwargs_parity.py
git commit -m "Build call kwargs per target, at parity with the shipped dicts"
```

---

### Task 3: Route every call through `kwargs_for`

**Files:**
- Modify: `src/meshpipeline/adapters/model_inference/router.py:66-70` (`_for_target`), `:139-166` (`_run_glm_stream`), `:196-216` (`_run_reviewer`), `:228-247` (`_run_chat`, `call_intake_model`), `:250-265` (`call_summarizer_model`)
- Test: `tests/unit/infra/test_router_uses_per_target_kwargs.py`

**Interfaces:**
- Consumes: `call_kwargs.spec_for`, `call_kwargs.kwargs_for` from Task 2.
- Produces: `router._for_target(role: str, target: RouteTarget) -> dict` — **signature change**: it now takes a role name instead of a base dict.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_router_uses_per_target_kwargs.py
# Responsibility: Verify the router builds its call parameters from the target, not a frozen dict.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import router
from meshpipeline.contracts.model_routing import RouteTarget


def test_for_target_takes_a_role_and_returns_that_roles_kwargs():
    target = RouteTarget(provider="deepinfra", model="zai-org/GLM-5.2",
                         account="default", circuit_group="deepinfra")
    kw = router._for_target("builder", target)
    assert kw["model"] == "zai-org/GLM-5.2"
    assert kw["stream"] is True
    assert kw["extra_body"]["min_p"] == pytest.approx(0.05)


def test_for_target_follows_the_target_not_the_roles_default_model():
    # The point of the refactor: the same role, a different target, different parameters.
    target = RouteTarget(provider="deepseek", model="deepseek-v4-pro",
                         account="default", circuit_group="deepseek")
    kw = router._for_target("intake", target)
    assert kw["model"] == "deepseek-v4-pro"
    assert kw["extra_body"]["thinking"] == {"type": "disabled"}


def test_the_router_no_longer_imports_the_shipped_kwargs_dicts():
    src = (router.__file__)
    text = open(src, encoding="utf-8").read()
    for name in ("BUILDER_CALL_KWARGS", "REVIEWER_CALL_KWARGS", "INTAKE_CALL_KWARGS",
                 "SUMMARIZER_CALL_KWARGS", "_planner_call_kwargs"):
        assert name not in text, (
            f"router.py still reaches for {name}; a frozen per-role dict cannot express a "
            "provider that rejects min_p or top_k")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_router_uses_per_target_kwargs.py -q -p no:cacheprovider`
Expected: FAIL — `_for_target()` takes a dict, so `TypeError` or a wrong result; the third test fails on the imports still being present.

- [ ] **Step 3: Replace `_for_target`**

In `router.py`, replace lines 66-70 entirely:

```python
def _for_target(role: str, target: RouteTarget) -> dict:
    # The sampling parameters a call may carry depend on WHO IS SERVING IT, not only on the
    # role. Built per attempt, because a retry may land on a different target than the first try.
    from meshpipeline.adapters.model_inference.call_kwargs import kwargs_for, spec_for
    return kwargs_for(target, spec_for(role))
```

- [ ] **Step 4: Update the five call sites**

`_run_glm_stream` — delete the `BUILDER_CALL_KWARGS` import and the `_base` line, change the
signature to take a role, and rewrite the `_invoke` body:

```python
async def _run_glm_stream(route, label, messages: Conversation,
                          tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
                          job_id: str, user_id: str,
                          parallel_tool_calls: ParallelToolCalls = None,
                          *, on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    async def _invoke(target: RouteTarget):
        call_kw = _with_tool_kwargs(_for_target(route.role, target), tools, tool_choice,
                                    parallel_tool_calls)
        response = await _streamed(
            target, messages, call_kw, f"{label}:{job_id[:8]}",
            langfuse_kwargs(session_id=job_id, user_id=user_id, name=label),
            on_reasoning=on_reasoning)
        if not (response and response.choices):
            raise _EmptyResponse(f"{label}: no choices")
        return response, _usage_from(response)

    return await _run(route, _invoke, job_id=job_id)
```

`route.role` is already `"builder"` or `"planner"`, so the `call_kwargs=` parameter and the
planner's separate import both disappear. In `call_planner_model`, delete the
`from ... deepinfra import _planner_call_kwargs` line and the `call_kwargs=_planner_call_kwargs()`
argument.

`_run_reviewer` — delete the `REVIEWER_CALL_KWARGS` import, and change its `call_kw` line to:

```python
        call_kw = _with_tool_kwargs(_for_target(route.role, target), tools, "auto",
                                    parallel_tool_calls)
```

`_run_chat` — drop the `base_kwargs` parameter from the signature and change its `call_kw` line to:

```python
        call_kw = _with_tool_kwargs(_for_target(route.role, target), tools, "auto",
                                    parallel_tool_calls)
```

`call_intake_model` — delete the `INTAKE_CALL_KWARGS` import and the argument:

```python
    return await _run_chat(icfg.INTAKE_ROUTE, name, messages, job_id, user_id, tools=tools)
```

`call_summarizer_model` — delete the `SUMMARIZER_CALL_KWARGS` import and change its `call_kw` line:

```python
        call_kw = _for_target(scfg.SUMMARIZER_ROUTE.role, target)
```

- [ ] **Step 5: Run the new test and the existing router suites**

Run:
```
PYTHONPATH=src python -m pytest tests/unit/infra/test_router_uses_per_target_kwargs.py \
  tests/unit/infra/test_call_kwargs_parity.py \
  tests/unit/infra/test_provider_error_hardening.py \
  tests/unit/infra/test_deepinfra_api_failure_hardening.py \
  tests/unit/platform/test_model_routing.py \
  tests/unit/infra/test_route_timing_equivalence.py \
  tests/unit/infra/test_failure_marker_equivalence.py -q -p no:cacheprovider
```
Expected: PASS. Before this task the last five were 131 passing; they must still all pass, because
Task 2 proved the kwargs are identical.

- [ ] **Step 6: Delete the now-unused dicts**

Once the suites are green, remove `BUILDER_CALL_KWARGS`, `_planner_call_kwargs` and
`REVIEWER_CALL_KWARGS` from `deepinfra.py`, and `INTAKE_CALL_KWARGS` and
`SUMMARIZER_CALL_KWARGS` from `deepseek.py`. Leave `get_deepinfra_client`,
`get_deepseek_client`, `_deepinfra_timeout` and `_THINKING_OFF`'s removal alone —
`_THINKING_OFF` moves to `call_kwargs.py` and its copy in `deepseek.py` goes with the dicts.

Then delete the parity test's `_shipped` helper and the parity test itself: it asserted against
symbols that no longer exist, and its job — proving the refactor was behaviour-preserving — is
done. Record what it proved in the commit message.

Run the same suite again. Expected: PASS, minus the parity tests.

- [ ] **Step 7: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/router.py \
        src/meshpipeline/adapters/model_inference/deepinfra.py \
        src/meshpipeline/adapters/model_inference/deepseek.py \
        tests/unit/infra/test_router_uses_per_target_kwargs.py
git rm tests/unit/infra/test_call_kwargs_parity.py
git commit -m "Build every call's parameters from the resolved target"
```

---

### Task 4: The OpenAI arm of `kwargs_for`

**Files:**
- Modify: `src/meshpipeline/adapters/model_inference/call_kwargs.py` (add `_openai_native`, register it)
- Test: `tests/unit/infra/test_call_kwargs_per_provider.py`

**Interfaces:**
- Consumes: `SamplingSpec`, `kwargs_for` from Task 2.
- Produces: `kwargs_for(RouteTarget(provider="openai", ...), spec)` returning kwargs with no `min_p` and no `top_k`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_call_kwargs_per_provider.py
# Responsibility: Verify each provider receives only parameters it accepts.
from __future__ import annotations

import pytest

from meshpipeline.adapters.model_inference import call_kwargs as ck
from meshpipeline.contracts.model_routing import RouteTarget


def _t(provider: str, model: str = "m") -> RouteTarget:
    return RouteTarget(provider=provider, model=model, account="default",
                       circuit_group=provider)


@pytest.mark.parametrize("role", ["builder", "planner", "visual_reviewer", "intake",
                                  "summarizer"])
def test_openai_never_receives_a_vendor_sampling_extension(role):
    """min_p and top_k are vLLM/SGLang sampler fields. OpenAI rejects an unknown parameter
    outright, so a route pointed at openai would 400 on every call."""
    kw = ck.kwargs_for(_t("openai"), ck.spec_for(role))
    body = kw.get("extra_body", {})
    assert "min_p" not in body and "top_k" not in body, f"{role}: {body}"
    assert "min_p" not in kw and "top_k" not in kw


@pytest.mark.parametrize("role", ["intake", "summarizer"])
def test_openai_never_receives_deepseeks_thinking_switch(role):
    kw = ck.kwargs_for(_t("openai"), ck.spec_for(role))
    assert "thinking" not in kw.get("extra_body", {})


def test_openai_still_carries_the_roles_real_intent():
    kw = ck.kwargs_for(_t("openai", "gpt-5.6-terra"), ck.spec_for("builder"))
    assert kw["model"] == "gpt-5.6-terra"
    assert kw["temperature"] == pytest.approx(0.3)
    assert kw["top_p"] == pytest.approx(0.95)
    assert kw["stream"] is True
    assert kw["stream_options"] == {"include_usage": True}


def test_deepinfra_still_gets_its_extensions():
    kw = ck.kwargs_for(_t("deepinfra"), ck.spec_for("builder"))
    assert kw["extra_body"]["min_p"] == pytest.approx(0.05)


def test_an_unregistered_provider_is_refused_not_guessed():
    with pytest.raises(KeyError, match="anthropic"):
        ck.kwargs_for(_t("anthropic"), ck.spec_for("builder"))
```

The last test documents that Anthropic is deliberately absent until stage 2 — a route pointed
there fails loudly at kwargs construction rather than sending GLM's sampler to Claude.

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_call_kwargs_per_provider.py -q -p no:cacheprovider`
Expected: FAIL — `KeyError: no call-kwargs builder for provider 'openai'`.

- [ ] **Step 3: Add the OpenAI builder**

In `call_kwargs.py`, after `_openai_wire`:

```python
def _openai_native(target: RouteTarget, spec: SamplingSpec) -> dict:
    """OpenAI proper. Same wire format, STRICTER parameter set: an unknown top-level or
    extra_body field is a 400, not an ignored hint. So the vendor sampler extensions
    (min_p, top_k) and DeepSeek's thinking switch are dropped rather than forwarded."""
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
```

and register it:

```python
_BUILDERS = {
    "deepinfra": _openai_wire,
    "deepseek": _openai_wire,
    "openai": _openai_native,
}
```

- [ ] **Step 4: Apply Task 1 Step 9's finding**

If the live check showed `max_tokens` is rejected in favour of `max_completion_tokens`, change
the one line in `_openai_native` to `kw = {"model": target.model, "max_completion_tokens": spec.max_tokens}`
and add this test to the file:

```python
def test_openai_uses_the_output_cap_name_the_api_accepts():
    kw = ck.kwargs_for(_t("openai"), ck.spec_for("builder"))
    assert "max_completion_tokens" in kw and "max_tokens" not in kw
```

If `max_tokens` was accepted, skip this step and leave the builder as written in Step 3.

- [ ] **Step 5: Run the tests**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_call_kwargs_per_provider.py tests/unit/infra/test_router_uses_per_target_kwargs.py -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/meshpipeline/adapters/model_inference/call_kwargs.py \
        tests/unit/infra/test_call_kwargs_per_provider.py
git commit -m "Send OpenAI only the sampling parameters it accepts"
```

---

### Task 5: Prices for the OpenAI models

**Files:**
- Modify: `src/meshpipeline/adapters/inference_telemetry/pricing.py:11-22`
- Test: `tests/unit/infra/test_openai_pricing.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `price_for("openai", <model>)` returning a non-zero triple for each listed model.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_openai_pricing.py
# Responsibility: Verify every OpenAI model a route may reach meters at a real price.
from __future__ import annotations

import pytest

from meshpipeline.adapters.inference_telemetry import pricing

# Fetched from developers.openai.com/api/docs/pricing on 2026-09-14. USD per 1M tokens.
EXPECTED = {
    "gpt-6-astra":    (10.00, 50.00, 1.00),
    "gpt-5.6-sol":    (4.00,  20.00, 0.40),
    "gpt-5.6-terra":  (2.00,  12.00, 0.20),
    "gpt-5.6-luna":   (0.20,   1.20, 0.02),
}


@pytest.mark.parametrize("model", sorted(EXPECTED))
def test_each_openai_model_carries_its_published_price(model):
    assert pricing.price_for("openai", model) == EXPECTED[model]


def test_an_unpriced_model_still_reports_zero_rather_than_inventing_one():
    assert pricing.price_for("openai", "gpt-does-not-exist") == (0.0, 0.0, 0.0)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_openai_pricing.py -q -p no:cacheprovider`
Expected: FAIL — four failures, each `(0.0, 0.0, 0.0) != (...)`.

- [ ] **Step 3: Add the rows**

In `pricing.py`, inside `_PRICES`, after the DeepSeek entries:

```python
    # Fetched from developers.openai.com/api/docs/pricing on 2026-09-14.
    "openai:gpt-6-astra":                              (10.00, 50.00, 1.00),
    "openai:gpt-5.6-sol":                              (4.00,  20.00, 0.40),
    "openai:gpt-5.6-terra":                            (2.00,  12.00, 0.20),
    "openai:gpt-5.6-luna":                             (0.20,   1.20, 0.02),
```

Also update the table's header comment, which currently reads `Verified 2026-07-16`, to record
the second retrieval date rather than overwrite the first:

```python
# (input, output, cached_input) USD per 1M tokens. DeepInfra and DeepSeek verified 2026-07-16
# against each provider's own model page - see the Gate-1 provider audit for the retrieval
# record. OpenAI fetched 2026-09-14 from developers.openai.com/api/docs/pricing.
```

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_openai_pricing.py -q -p no:cacheprovider`
Expected: PASS, five tests.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/adapters/inference_telemetry/pricing.py \
        tests/unit/infra/test_openai_pricing.py
git commit -m "Price the OpenAI models a route may reach"
```

---

### Task 6: Close the reviewer's $0.00 metering

**Files:**
- Modify: `src/meshpipeline/adapters/inference_telemetry/pricing.py:11-26`
- Test: `tests/unit/infra/test_no_configured_route_meters_at_zero.py`

Today `visual_reviewer` runs `Qwen/Qwen3-VL-235B-A22B-Thinking`, whose price was never confirmed
on DeepInfra's catalogue (`pricing.py:24`). It meters at `$0.00`, so every mesh job under-bills by
the reviewer's entire cost. This task makes that condition impossible to reintroduce silently.

**Interfaces:**
- Consumes: `pricing.unpriced_route_models()` (exists).
- Produces: no new symbols.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/infra/test_no_configured_route_meters_at_zero.py
# Responsibility: Verify no model this deployment routes to bills at zero.
from __future__ import annotations

from meshpipeline.adapters.inference_telemetry.pricing import unpriced_route_models


def test_every_configured_route_model_has_a_confirmed_price():
    unpriced = unpriced_route_models()
    assert unpriced == [], (
        "these configured models meter at $0.00, so every job that uses them under-bills "
        f"silently: {unpriced}. Confirm the price on the provider's catalogue and add it to "
        "_PRICES, or point the route at a model that has one.")
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/infra/test_no_configured_route_meters_at_zero.py -q -p no:cacheprovider`
Expected: FAIL — `['deepinfra:Qwen/Qwen3-VL-235B-A22B-Thinking']`.

- [ ] **Step 3: Confirm the price on DeepInfra's catalogue**

```bash
curl -s https://api.deepinfra.com/models/list | \
  python3 -c 'import json,sys; [print(m.get("model_name"), m.get("pricing")) for m in json.load(sys.stdin) if "Qwen3-VL" in str(m.get("model_name",""))]'
```

Two possible outcomes, and they lead to different edits:

- **The `-Thinking` variant is listed with a price.** Add its row to `_PRICES`, delete the
  explanatory comment at `pricing.py:24-29`, and record the retrieval date in the header comment.
- **Only `-Instruct` is listed.** The configured model does not exist on the provider under that
  id, which is a routing bug rather than a pricing gap — the route has been naming a model
  DeepInfra does not serve. Change `REVIEWER_MODEL`'s default in
  `src/meshpipeline/agents/reviewer/settings.py:9` and the `visual_reviewer` row of
  `ROUTE_MATRIX` in `src/meshpipeline/settings/inventory.py:133` to
  `Qwen/Qwen3-VL-235B-A22B-Instruct`, which is already priced at `(0.20, 0.88, 0.11)`, and delete
  the comment at `pricing.py:24-29`.

The second outcome is the one exception to this plan's "no default may move" constraint: it is not
a provider swap, it is correcting an id that names nothing. Note which outcome occurred in the
commit message.

- [ ] **Step 4: Run the tests**

Run:
```
PYTHONPATH=src python -m pytest tests/unit/infra/test_no_configured_route_meters_at_zero.py \
  tests/unit/infra/test_openai_pricing.py tests/unit/platform/test_model_routing.py \
  -q -p no:cacheprovider
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/meshpipeline/adapters/inference_telemetry/pricing.py \
        tests/unit/infra/test_no_configured_route_meters_at_zero.py
git commit -m "Stop the reviewer metering at zero"
```

---

### Task 7: Deploy the credential

**Files:**
- Modify: `deploy/gcp/scripts/create-secrets.sh:47,90`
- Modify: `deploy/gcp/scripts/bootstrap-env.sh:457`
- Modify: `deploy/gcp/generated.prod.env:139`
- Modify: `deploy/gcp/scripts/create-api-service.sh:130,247`
- Modify: `deploy/gcp/scripts/create-worker-fleet.sh:232`
- Modify: `deploy/gcp/worker/startup.sh:75`
- Test: `tests/unit/deploy/test_openai_secret_is_wired.py`

**Interfaces:**
- Consumes: `OPENAI_API_KEY` from Task 1.
- Produces: no Python symbols; the container `openai-api-key` reaches the API service and the worker fleet.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/deploy/test_openai_secret_is_wired.py
# Responsibility: Verify the openai credential reaches every runtime that resolves a route.
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]

CASES = [
    ("deploy/gcp/scripts/create-secrets.sh",      "OPENAI_API_KEY|"),
    ("deploy/gcp/scripts/bootstrap-env.sh",       "OPENAI_API_KEY_SECRET"),
    ("deploy/gcp/generated.prod.env",             "OPENAI_API_KEY_SECRET=openai-api-key"),
    ("deploy/gcp/scripts/create-api-service.sh",  "OPENAI_API_KEY:OPENAI_API_KEY_SECRET"),
    ("deploy/gcp/scripts/create-worker-fleet.sh", "openai-api-key-secret:OPENAI_API_KEY_SECRET"),
    ("deploy/gcp/worker/startup.sh",              "OPENAI_API_KEY:openai-api-key-secret"),
]


@pytest.mark.parametrize("path,needle", CASES)
def test_the_openai_credential_is_wired(path, needle):
    text = (REPO / path).read_text(encoding="utf-8")
    assert needle in text, f"{path} does not carry {needle!r}"


def _secret_bearing_roster() -> set[str]:
    """The SECRET_BEARING=( ... ) array in create-api-service.sh, parsed."""
    import re
    text = (REPO / "deploy/gcp/scripts/create-api-service.sh").read_text(encoding="utf-8")
    match = re.search(r"SECRET_BEARING=\((.*?)\)", text, re.DOTALL)
    assert match, "SECRET_BEARING array not found in create-api-service.sh"
    return set(match.group(1).split())


def test_the_plaintext_refusal_roster_names_the_openai_key():
    assert "OPENAI_API_KEY" in _secret_bearing_roster(), (
        "API_EXTRA_ENV would accept OPENAI_API_KEY=<key> as a visible environment value; "
        "check_deploy_secrets.py only matches names it is told are secret-bearing")


def test_the_roster_is_exactly_the_inventorys_secret_set():
    """create-api-service.sh:243 says the roster IS the secret=True set from inventory.py,
    'restated here' for a blind spot in the deploy gate. Nothing enforced that until now, so
    the two could drift silently - which is how a new credential gets a plaintext path."""
    from meshpipeline.settings import inventory
    declared = {v.name for v in inventory.all_vars() if v.secret}
    roster = _secret_bearing_roster()
    assert declared == roster, (
        "SECRET_BEARING and the inventory's secret=True set have drifted: "
        f"only in inventory={sorted(declared - roster)}, "
        f"only in the script={sorted(roster - declared)}")
```

The last test is new scope, deliberately. `create-api-service.sh:243` claims the roster is the
inventory's `secret=True` set "restated here", and nothing enforced it — a credential added to the
inventory without a matching roster entry silently gains a plaintext path through `API_EXTRA_ENV`.
Adding a credential is exactly when to close that.

Both sets hold the same 11 names today (`DATABASE_URL`, `DEEPINFRA_API_KEY`, `DEEPSEEK_API_KEY`,
`LANGFUSE_SECRET_KEY`, `MESH_API_KEY`, `MINIO_SECRET_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`,
`SENTRY_DSN`, `TAVILY_API_KEY`, `USER_TOKEN_SECRET`), verified 2026-09-14. **Between Task 1 and
Task 7 they are deliberately out of sync** — Task 1 adds `OPENAI_API_KEY` to the inventory, and
this task adds it to the roster. That is why this test is written here and not earlier.

- [ ] **Step 2: Run it to make sure it fails**

Run: `PYTHONPATH=src python -m pytest tests/unit/deploy/test_openai_secret_is_wired.py -q -p no:cacheprovider`
Expected: FAIL — six failures.

- [ ] **Step 3: Make the edits**

`create-secrets.sh`, beside the other container names (line 47):

```bash
OPENAI_SECRET="${OPENAI_API_KEY_SECRET:-openai-api-key}"
```

and in the `SECRETS=()` array (after the `DEEPSEEK_API_KEY` row):

```bash
  "OPENAI_API_KEY|${OPENAI_SECRET}|optional|API_SERVICE_ACCOUNT WORKER_SERVICE_ACCOUNT|the model provider the agents call"
```

`bootstrap-env.sh` (line 457), beside the other two:

```bash
OPENAI_API_KEY_SECRET=${OPENAI_API_KEY_SECRET:-openai-api-key}
```

`generated.prod.env` (line 139):

```
OPENAI_API_KEY_SECRET=openai-api-key
```

`create-api-service.sh` — the `SECRET_BINDINGS` loop at line 126 ends on
`"USER_TOKEN_SECRET:USER_TOKEN_SECRET_SECRET"; do`. Insert before that final element:

```bash
            "OPENAI_API_KEY:OPENAI_API_KEY_SECRET" \
```

and in the same file, extend the `SECRET_BEARING=( ... )` array at line 245 — it currently ends
`DEEPSEEK_API_KEY DEEPINFRA_API_KEY)`:

```bash
                DEEPSEEK_API_KEY DEEPINFRA_API_KEY OPENAI_API_KEY)
```

`create-worker-fleet.sh` — the loop at line 228 ends on
`"deepseek-api-key-secret:DEEPSEEK_API_KEY_SECRET"; do`. That is the final element, so it gains a
trailing `\` and the `; do` moves to the new last line:

```bash
            "deepseek-api-key-secret:DEEPSEEK_API_KEY_SECRET" \
            "openai-api-key-secret:OPENAI_API_KEY_SECRET"; do
```

`worker/startup.sh` — identically, the loop at line 71 ends on
`"DEEPSEEK_API_KEY:deepseek-api-key-secret"; do`:

```bash
            "DEEPSEEK_API_KEY:deepseek-api-key-secret" \
            "OPENAI_API_KEY:openai-api-key-secret"; do
```

Both loops already treat an absent secret name as "this deployment does not use that credential"
and `continue`, so a project with no OpenAI key is unaffected by either edit.

- [ ] **Step 4: Run the tests**

Run: `PYTHONPATH=src python -m pytest tests/unit/deploy/ -q -p no:cacheprovider`
Expected: PASS. The existing deploy suite must stay green — `test_integration_preflight_is_read_only.py` in particular.

- [ ] **Step 5: Create the containers and write the values**

Not part of the commit; an owner runs this once per project. `create-secrets.sh` creates the empty
containers and grants readers; it never writes a payload, by design.

```bash
bash deploy/gcp/scripts/create-secrets.sh          # creates + grants, both projects
printf '%s' '<key>' | gcloud secrets versions add openai-api-key --project hexera-prod  --data-file=-
printf '%s' '<key>' | gcloud secrets versions add openai-api-key --project hexera-dev   --data-file=-
```

- [ ] **Step 6: Commit**

```bash
git add deploy/gcp/scripts/create-secrets.sh deploy/gcp/scripts/bootstrap-env.sh \
        deploy/gcp/generated.prod.env deploy/gcp/scripts/create-api-service.sh \
        deploy/gcp/scripts/create-worker-fleet.sh deploy/gcp/worker/startup.sh \
        tests/unit/deploy/test_openai_secret_is_wired.py
git commit -m "Carry the openai credential to every runtime that resolves a route"
```

---

## Done when

- `PYTHONPATH=src python -m pytest tests/unit/infra tests/unit/platform tests/unit/deploy -q -p no:cacheprovider` passes at or above the pre-change baseline (1233 passed / 61 env-gap failures across `tests/unit/{infra,platform,api,agents}`).
- `routes.summary()` still prints the same five routes with the same five models.
- `unpriced_route_models()` returns `[]`.
- Setting `BUILDER_PROVIDER=openai` and `BUILDER_MODEL=gpt-5.6-terra` in a local `.env` produces a working builder call, and no `min_p` appears in the request.

## What this plan does not do

Stages 2–4 of the spec — the Anthropic adapter, the Postgres override store, and the admin
portal — each get their own plan. They are written after this one lands, because their task
decomposition depends on the `kwargs_for()` interface established here and on the live parameter
findings from Task 1 Step 9 and Task 6 Step 3.
