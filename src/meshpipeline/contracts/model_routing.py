# Responsibility: Describe which model a capability may use, and what to do when that model refuses.
# Owns: the capability and quota-domain vocabulary, route targets, and the retry policy attached to a route.
# Boundaries: a declaration of policy; it performs no call and holds no provider credentials.
# Collaborates with: adapters/model_inference/routing.py, which applies these routes.
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):

    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    MULTIMODAL = "multimodal"
    REASONING_CONTROL = "reasoning_control"


class FailureCategory(str, Enum):

 # transient: the provider could not serve a request it would normally accept
    RATE_LIMIT = "rate_limit"
    OVERLOAD = "overload"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVICE_UNAVAILABLE = "service_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    CIRCUIT_OPEN = "circuit_open"

 # never a reason to change provider
    AUTH = "auth"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVALID_REQUEST = "invalid_request"
    APPLICATION_DEFECT = "application_defect"
    TOOL_SCHEMA = "tool_schema"
    POLICY_REJECTION = "policy_rejection"
    INVALID_OUTPUT = "invalid_output"
    QUALITY_FAILED = "quality_failed"
    EMPTY_RESPONSE = "empty_response"


FAILOVER_ELIGIBLE: frozenset[FailureCategory] = frozenset({
    FailureCategory.RATE_LIMIT,
    FailureCategory.OVERLOAD,
    FailureCategory.TIMEOUT,
    FailureCategory.CONNECTION,
    FailureCategory.SERVICE_UNAVAILABLE,
    FailureCategory.MODEL_UNAVAILABLE,
    FailureCategory.CIRCUIT_OPEN,
})
"""The DEFAULT eligible set. A route may narrow this; it must never widen it - anything outside
this set is a configuration or application defect, and routing around a defect just buys the
same failure from a second provider."""

NEVER_FAILOVER: frozenset[FailureCategory] = frozenset(
    c for c in FailureCategory if c not in FAILOVER_ELIGIBLE)


class SelectionReason(str, Enum):

    PRIMARY = "primary"                          # the normal path
    NO_STANDBY_CONFIGURED = "no_standby_configured"
    STANDBY_PRIMARY_CIRCUIT_OPEN = "standby_primary_circuit_open"
    STANDBY_PRIMARY_SATURATED = "standby_primary_saturated"   # queue deadline exceeded
    STANDBY_PRIMARY_FAILED = "standby_primary_failed"         # retries exhausted, transient


@dataclass(frozen=True)
class QuotaDomain:

    provider: str
    account: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.account}:{self.model}"


@dataclass(frozen=True)
class RouteTarget:

    provider: str
    model: str
    circuit_group: str
    account: str = "default"

    @property
    def domain(self) -> QuotaDomain:
        return QuotaDomain(provider=self.provider, account=self.account, model=self.model)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class RetryPolicy:

    max_attempts: int = 3
    backoff_base_s: float = 1.0
    backoff_max_s: float = 60.0
    jitter: float = 0.25          # +/- fraction, so a fleet does not retry in lockstep
    rate_limit_backoff_base_s: float | None = None
    rate_limit_backoff_max_s: float | None = None

    def backoff_for(self, category: FailureCategory, attempt: int) -> float:
        base, cap = self.backoff_base_s, self.backoff_max_s
        if category is FailureCategory.RATE_LIMIT:
            if self.rate_limit_backoff_base_s is not None:
                base = self.rate_limit_backoff_base_s
            if self.rate_limit_backoff_max_s is not None:
                cap = self.rate_limit_backoff_max_s
        return min(base * (2 ** attempt), cap)


@dataclass(frozen=True)
class ModelRoute:

    role: str
    primary: RouteTarget
    standby: RouteTarget | None = None
    required_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    timeout_s: float = 120.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    concurrency_budget: int = 8
    queue_deadline_s: float = 30.0
    allowed_failover: frozenset[FailureCategory] = FAILOVER_ELIGIBLE
    # TOTAL wall-clock budget for the whole route: admission wait + every attempt + every
    # backoff. `timeout_s` alone bounds ONE attempt, so a role with 2 attempts and a 60s
    # per-attempt timeout can legitimately take 120s + backoff - which is the wrong answer for a
    # caller that must not be kept waiting (the summarizer degrades a search; it must never hold
    # a builder round hostage). None = no total cap; only the per-attempt timeout applies.
    total_deadline_s: float | None = None

    def __post_init__(self) -> None:
        widened = self.allowed_failover - FAILOVER_ELIGIBLE
        if widened:
            raise ValueError(
                f"route {self.role!r} allows failover on {sorted(c.value for c in widened)}, "
                "which are configuration/application failures - a standby would fail the same "
                "way and the real defect would be hidden. Allowed: "
                f"{sorted(c.value for c in FAILOVER_ELIGIBLE)}")
        if self.concurrency_budget < 1:
            raise ValueError(f"route {self.role!r}: concurrency_budget must be >= 1")

    @property
    def targets(self) -> tuple[RouteTarget, ...]:
        return (self.primary,) if self.standby is None else (self.primary, self.standby)

    def may_fail_over(self, category: FailureCategory) -> bool:
        return self.standby is not None and category in self.allowed_failover
