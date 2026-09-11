"""RKAPI client — the only place that talks to the LLM.

Three things here are not optional, each learned the hard way:

1. **Always stream.** The same call takes 86 s through the non-streaming endpoint and
   2.7 s through the streaming one. Non-streaming requests idle while the model
   reasons and routinely trip Cloudflare's 100 s timeout, which surfaces as a 504
   storm rather than an obvious stall. `stream=True` on every call, without exception.
2. **Send a real User-Agent.** RKAPI sits behind Cloudflare, which answers a default
   `httpx`/`urllib` UA with `403 error code: 1010`. That looks exactly like a dead key.
   The openai SDK sets its own UA, which is why the SDK path works.
3. **A key is a rate lane, not a mutex.** Measured on this account: one key served 8
   concurrent small calls in 3.7 s wall (2.16 calls/s) with no errors — they pipeline.
   An earlier build allowed one in-flight request per key, inherited from a heavy
   document-parse workload where requests really did serialise, and it throttled
   throughput roughly eightfold: at 50 concurrent users the median wait was 6 minutes.
   The semaphore stays as a per-key ceiling (`RKAPI_PER_KEY_CONCURRENCY`) so one key
   cannot be stampeded, but it is no longer 1.

Throughput is roughly 40 tokens/s, so `max_completion_tokens` is a latency budget, not
just a cost cap.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import structlog
from openai import AsyncOpenAI
from openai import APIStatusError, APITimeoutError, RateLimitError

from app.config import settings

log = structlog.get_logger(__name__)

# Billing, per the RKAPI catalog: base $2/$12 per M, openai group multiplier 1.5,
# cached input at a tenth of the input rate. These are credit-USD, not real money.
PRICE_IN = 2.00 / 1_000_000
PRICE_OUT = 12.00 / 1_000_000
GROUP_MULT = 1.5
CACHE_DISCOUNT = 0.1


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cached: int = 0
    total: int = 0
    model: str = ""
    cost_credits: float = 0.0

    def as_event(self) -> Dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cached": self.cached,
            "total": self.total,
            "cost_credits": round(self.cost_credits, 6),
            "model": self.model,
        }


def price(input_tokens: int, output_tokens: int, cached: int = 0, model: str = "") -> float:
    billable_in = max(input_tokens - cached, 0) + cached * CACHE_DISCOUNT
    return (billable_in * PRICE_IN + output_tokens * PRICE_OUT) * GROUP_MULT


@dataclass
class _Lane:
    key: str
    sem: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(settings.rkapi_per_key_concurrency))
    client: Optional[AsyncOpenAI] = None
    spend: float = 0.0
    calls: int = 0
    inflight: int = 0


class RKAPIError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class RKAPIClient:
    """Key-pooled, always-streaming chat client."""

    def __init__(self, keys: Optional[List[str]] = None, model: Optional[str] = None):
        keys = keys or settings.rkapi_keys
        if not keys:
            raise RuntimeError("RKAPI_OPENAI_KEYS is empty — no lane to send requests on")
        self.model = model or settings.rkapi_model
        self._lanes = [_Lane(key=k) for k in keys]
        self._rr = itertools.cycle(range(len(self._lanes)))

    # ── lane management ───────────────────────────────────────────────────────
    def _client_for(self, lane: _Lane) -> AsyncOpenAI:
        if lane.client is None:
            lane.client = AsyncOpenAI(
                api_key=lane.key,
                base_url=settings.rkapi_base_url,
                timeout=settings.rkapi_timeout,
                max_retries=0,  # retries are ours, so they can be logged and jittered
                default_headers={"User-Agent": "OpenAI/Python 1.55.0"},
            )
        return lane.client

    def _pick_lane(self, hint: Optional[int]) -> _Lane:
        """Least-loaded lane. `hint` pins one so a batch job can spread N workers across
        N keys deterministically.

        Picking the *first* lane with capacity piles every request onto key 0 until it
        saturates, which wastes the other keys and drains one account's wallet far
        faster than the rest.
        """
        if hint is not None:
            return self._lanes[hint % len(self._lanes)]
        return min(self._lanes, key=lambda ln: ln.inflight)

    @property
    def lane_count(self) -> int:
        return len(self._lanes)

    def spend_report(self) -> List[Dict[str, Any]]:
        return [
            {"lane": i, "calls": ln.calls, "inflight": ln.inflight,
             "cost_credits": round(ln.spend, 6)}
            for i, ln in enumerate(self._lanes)
        ]

    # ── the one call everything else goes through ─────────────────────────────
    async def stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: int = 2000,
        lane_hint: Optional[int] = None,
        attempts: int = 3,
        model: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield `{"type": "delta", "text": ...}` then one `{"type": "usage", ...}`.

        Retries transient failures (429, 5xx, timeouts, Cloudflare hiccups) with
        jittered backoff. A non-retryable failure raises immediately — a 403 for a
        model this key cannot reach will never succeed on a second try.
        """
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            lane = self._pick_lane(lane_hint)
            try:
                async with lane.sem:
                    lane.inflight += 1
                    started = time.perf_counter()
                    got_text = False
                    async for chunk in self._one_stream(lane, messages, max_tokens, model):
                        if chunk["type"] == "delta":
                            got_text = True
                        elif chunk["type"] == "usage":
                            usage: Usage = chunk["usage"]
                            lane.spend += usage.cost_credits
                            lane.calls += 1
                            log.debug(
                                "rkapi.call",
                                ms=int((time.perf_counter() - started) * 1000),
                                input=usage.input, output=usage.output,
                                cost=usage.cost_credits, attempt=attempt,
                            )
                        yield chunk
                    if not got_text:
                        # An empty stream is a failure wearing a success costume: it
                        # would otherwise be parsed as "the model said nothing".
                        raise RKAPIError("empty stream from RKAPI", retryable=True)
                    lane.inflight -= 1
                    return
            except (RateLimitError, APITimeoutError) as e:
                lane.inflight = max(lane.inflight - 1, 0)
                last_error = e
            except APIStatusError as e:
                lane.inflight = max(lane.inflight - 1, 0)
                if e.status_code in (408, 409, 429) or e.status_code >= 500:
                    last_error = e
                else:
                    raise RKAPIError(
                        f"RKAPI {e.status_code}: {str(e)[:200]}",
                        retryable=False, status=e.status_code,
                    ) from e
            except RKAPIError as e:
                lane.inflight = max(lane.inflight - 1, 0)
                if not e.retryable:
                    raise
                last_error = e
            except Exception as e:  # noqa: BLE001 — network layer throws many shapes
                lane.inflight = max(lane.inflight - 1, 0)
                last_error = e

            if attempt < attempts - 1:
                backoff = (2 ** attempt) + random.uniform(0, 0.75)
                log.warning("rkapi.retry", attempt=attempt, backoff=round(backoff, 2),
                            error=str(last_error)[:160])
                await asyncio.sleep(backoff)

        raise RKAPIError(f"RKAPI failed after {attempts} attempts: {last_error}", retryable=True)

    async def _one_stream(
        self, lane: _Lane, messages: List[Dict[str, Any]], max_tokens: int,
        model: Optional[str],
    ) -> AsyncIterator[Dict[str, Any]]:
        client = self._client_for(lane)
        stream = await client.chat.completions.create(
            model=model or self.model,
            messages=messages,
            max_completion_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        usage = Usage(model=model or self.model)
        async for event in stream:
            if event.usage:
                usage.input = event.usage.prompt_tokens or 0
                usage.output = event.usage.completion_tokens or 0
                usage.total = event.usage.total_tokens or (usage.input + usage.output)
                details = getattr(event.usage, "prompt_tokens_details", None)
                usage.cached = getattr(details, "cached_tokens", 0) or 0
            if not event.choices:
                continue
            delta = event.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                yield {"type": "delta", "text": text}
        usage.cost_credits = price(usage.input, usage.output, usage.cached, usage.model)
        yield {"type": "usage", "usage": usage}

    # ── convenience wrappers ──────────────────────────────────────────────────
    async def complete(
        self, messages: List[Dict[str, Any]], *, max_tokens: int = 2000,
        lane_hint: Optional[int] = None, model: Optional[str] = None,
    ) -> tuple[str, Usage]:
        """Collect a full response. Still streams underneath — see the module docstring."""
        parts: List[str] = []
        usage = Usage(model=model or self.model)
        async for chunk in self.stream(messages, max_tokens=max_tokens,
                                       lane_hint=lane_hint, model=model):
            if chunk["type"] == "delta":
                parts.append(chunk["text"])
            elif chunk["type"] == "usage":
                usage = chunk["usage"]
        return "".join(parts), usage

    async def json_complete(
        self, messages: List[Dict[str, Any]], *, max_tokens: int = 2000,
        lane_hint: Optional[int] = None, default: Optional[Dict] = None,
    ) -> tuple[Dict[str, Any], Usage]:
        """Structured output without `response_format`, which this reasoning model
        rejects. The prompt asks for JSON; we parse defensively."""
        text, usage = await self.complete(messages, max_tokens=max_tokens, lane_hint=lane_hint)
        return parse_json_loose(text, default=default), usage


def parse_json_loose(text: str, default: Optional[Dict] = None) -> Dict[str, Any]:
    """Pull the first JSON object out of a model response.

    Handles the three shapes that actually occur: clean JSON, JSON inside a ```json
    fence, and JSON preceded by a sentence of preamble.
    """
    if not text:
        return dict(default or {})
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if len(s.split("```")) > 1 else s
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    start = s.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(s[start:], start):
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except json.JSONDecodeError:
                        break
    log.warning("rkapi.json_parse_failed", preview=text[:200])
    return dict(default or {})


_client: Optional[RKAPIClient] = None


def get_rkapi() -> RKAPIClient:
    global _client
    if _client is None:
        _client = RKAPIClient()
    return _client
