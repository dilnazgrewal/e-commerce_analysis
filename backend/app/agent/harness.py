"""The harness around every Claude call: kill switch, daily token budget, circuit breaker, timeouts, logging.

`Harness.call()` never raises for API trouble. It returns a `CallResult` saying what happened, so the decision
pipeline can step down to the next tier instead of crashing, and every attempt lands in the audit log.
"""

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import anthropic

from app.config import get_settings

CALL_TIMEOUT_S = 20.0  # measured: a trivial Sonnet 5.5 tool call took ~8 s, so the planned 8 s was too tight
RULE_AUTHOR_TIMEOUT_S = 120.0
BREAKER_FAILURES = 5
BREAKER_WINDOW_S = 120
BREAKER_COOLDOWN_S = 300


class LLM(Protocol):
    """Anything with the SDK's `create(**kwargs)` shape: the real client or a scripted fake in tests."""

    def create(self, **kwargs: Any) -> Any: ...


class AnthropicLLM:
    def __init__(self, beta: bool, timeout_s: float = CALL_TIMEOUT_S) -> None:
        key = get_settings().anthropic_api_key
        client = anthropic.Anthropic(api_key=key.get_secret_value() if key else None)
        client = client.with_options(timeout=timeout_s, max_retries=1)
        self._create = client.beta.messages.create if beta else client.messages.create

    def create(self, **kwargs: Any) -> Any:
        return self._create(**kwargs)


@dataclass
class CallResult:
    ok: bool
    response: Any = None
    error: str | None = (
        None  # timeout | rate_limited | server_error | bad_request | connection | refusal | other
    )
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class CircuitBreaker:
    def __init__(
        self,
        failures: int = BREAKER_FAILURES,
        window_s: float = BREAKER_WINDOW_S,
        cooldown_s: float = BREAKER_COOLDOWN_S,
        clock=time.monotonic,
    ) -> None:
        self.failures, self.window_s, self.cooldown_s, self.clock = failures, window_s, cooldown_s, clock
        self._events: deque[float] = deque()
        self._open_until = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return self.clock() < self._open_until

    def record(self, ok: bool) -> None:
        with self._lock:
            now = self.clock()
            if ok:
                self._events.clear()
                return
            self._events.append(now)
            while self._events and now - self._events[0] > self.window_s:
                self._events.popleft()
            if len(self._events) >= self.failures:
                self._open_until = now + self.cooldown_s
                self._events.clear()


@dataclass
class TokenBudget:
    daily_limit: int
    used: int = 0
    day: date = field(default_factory=date.today)

    def _roll(self) -> None:
        if date.today() != self.day:
            self.day, self.used = date.today(), 0

    def exhausted(self) -> bool:
        self._roll()
        return self.used >= self.daily_limit

    def spend(self, tokens: int) -> None:
        self._roll()
        self.used += tokens


def ai_enabled() -> bool:
    """Kill switch: OFFERIQ_AI_ENABLED=0 sends every decision down the rules-only path."""
    return os.environ.get("OFFERIQ_AI_ENABLED", "1") not in ("0", "false", "off")


class Harness:
    def __init__(
        self,
        sonnet: LLM | None = None,
        haiku: LLM | None = None,
        budget: TokenBudget | None = None,
        opus: LLM | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        settings = get_settings()
        self.has_key = (
            settings.anthropic_api_key is not None
            or (sonnet is not None and haiku is not None)
            or (opus is not None)
        )
        self._sonnet, self._haiku, self._opus = sonnet, haiku, opus
        self.budget = budget or TokenBudget(settings.claude_daily_token_budget)
        self.breaker = breaker or CircuitBreaker()

    @property
    def sonnet(self) -> LLM:
        if self._sonnet is None:
            self._sonnet = AnthropicLLM(beta=True)  # beta endpoint: server-side refusal fallback
        return self._sonnet

    @property
    def haiku(self) -> LLM:
        if self._haiku is None:
            self._haiku = AnthropicLLM(beta=False)
        return self._haiku

    @property
    def opus(self) -> LLM:
        """Rule authoring only (dashboard, a person waiting): a full policy takes longer to write."""
        if self._opus is None:
            self._opus = AnthropicLLM(beta=True, timeout_s=RULE_AUTHOR_TIMEOUT_S)
        return self._opus

    def unavailable_reason(self) -> str | None:
        if not ai_enabled():
            return "kill switch is on (AI disabled)"
        if not self.has_key:
            return "no Anthropic API key configured"
        if self.breaker.is_open():
            return "circuit breaker open after repeated API failures"
        if self.budget.exhausted():
            return f"daily token budget of {self.budget.daily_limit:,} used up"
        return None

    def call(self, llm: LLM, **kwargs: Any) -> CallResult:
        started = time.perf_counter()
        try:
            response = llm.create(**kwargs)
        except anthropic.APITimeoutError:
            return self._fail("timeout", started)
        except anthropic.RateLimitError:
            return self._fail("rate_limited", started)
        except anthropic.BadRequestError as e:
            return self._fail(f"bad_request: {e.message[:200]}", started)
        except anthropic.APIStatusError as e:
            return self._fail(
                "server_error" if e.status_code >= 500 else f"api_error_{e.status_code}", started
            )
        except anthropic.APIConnectionError:
            return self._fail("connection", started)
        latency = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage", None)
        result = CallResult(
            ok=True,
            response=response,
            latency_ms=latency,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        self.budget.spend(result.input_tokens + result.output_tokens)
        self.breaker.record(True)
        if getattr(response, "stop_reason", None) == "refusal":
            result.ok, result.error = False, "refusal"
        return result

    def _fail(self, error: str, started: float) -> CallResult:
        self.breaker.record(False)
        return CallResult(ok=False, error=error, latency_ms=int((time.perf_counter() - started) * 1000))
