"""Model routing: the right model for each kind of call.

Two providers, chosen by capability rather than preference:

- **DeepSeek (`deepseek-chat`)** handles every text call — perception, planning,
  composition, reranking. Measured against the previous model on this exact workload:
  perception 0.9 s vs 3.2 s, composition 1.4 s vs 2.8 s, JSON parsed 3/3 both, and the
  answers came back *longer and warmer*. Roughly 3x faster for better output.
- **`gpt-5.6-terra` via RKAPI** handles vision, because DeepSeek has none. Reading an
  error code off a photo is the one thing only it can do.

Routing by capability means a provider outage degrades one feature instead of the
service: if DeepSeek is unreachable the text calls fall back to terra, which is slower
but complete.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import structlog

from app.clients.rkapi import RKAPIClient, Usage, parse_json_loose
from app.config import settings

log = structlog.get_logger(__name__)

# DeepSeek bills in real dollars, not resale credits: $0.28 / $0.42 per M tokens
# (cache miss / output) on `deepseek-chat` at the time of writing.
DEEPSEEK_PRICE_IN = 0.28 / 1_000_000
DEEPSEEK_PRICE_OUT = 0.42 / 1_000_000


class DeepSeekClient(RKAPIClient):
    """Same streaming contract as the RKAPI client, different endpoint and pricing.

    Inherits the retry, lane-pooling and always-stream behaviour rather than
    reimplementing them — those were hard-won and are not provider-specific.
    """

    def __init__(self) -> None:
        keys = [k.strip() for k in settings.deepseek_api_keys.split(",") if k.strip()]
        if not keys:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        self.model = settings.deepseek_model
        self._lanes = [self._make_lane(k) for k in keys]
        import itertools
        self._rr = itertools.cycle(range(len(self._lanes)))
        self._base_url = settings.deepseek_base_url

    @staticmethod
    def _make_lane(key: str):
        from app.clients.rkapi import _Lane
        return _Lane(key=key)

    def _client_for(self, lane):
        if lane.client is None:
            from openai import AsyncOpenAI
            lane.client = AsyncOpenAI(
                api_key=lane.key, base_url=self._base_url,
                timeout=settings.rkapi_timeout, max_retries=0,
            )
        return lane.client

    @staticmethod
    def price(usage: Usage) -> float:
        return usage.input * DEEPSEEK_PRICE_IN + usage.output * DEEPSEEK_PRICE_OUT


class LLM:
    """One façade the agent talks to. Decides provider per call."""

    def __init__(self) -> None:
        self._text: Optional[Any] = None
        self._vision: Optional[RKAPIClient] = None

    # ── providers ─────────────────────────────────────────────────────────────
    @property
    def text(self):
        if self._text is None:
            if settings.text_provider == "deepseek":
                try:
                    self._text = DeepSeekClient()
                    log.info("llm.text_provider", provider="deepseek",
                             model=settings.deepseek_model)
                except Exception as e:  # noqa: BLE001
                    log.warning("llm.deepseek_unavailable", error=str(e)[:140])
                    self._text = self.vision
            else:
                self._text = self.vision
        return self._text

    @property
    def vision(self) -> RKAPIClient:
        if self._vision is None:
            from app.clients.rkapi import get_rkapi
            self._vision = get_rkapi()
        return self._vision

    # ── the calls the agent makes ─────────────────────────────────────────────
    async def stream(self, messages: List[Dict[str, Any]], *, max_tokens: int = 2000,
                     **kw) -> AsyncIterator[Dict[str, Any]]:
        async for chunk in self.text.stream(messages, max_tokens=max_tokens, **kw):
            yield chunk

    async def complete(self, messages: List[Dict[str, Any]], *,
                       max_tokens: int = 2000, **kw) -> Tuple[str, Usage]:
        try:
            return await self.text.complete(messages, max_tokens=max_tokens, **kw)
        except Exception as e:  # noqa: BLE001
            # One provider being down should cost speed, not the answer.
            if self.text is not self.vision:
                log.warning("llm.text_fallback_to_vision", error=str(e)[:140])
                return await self.vision.complete(messages, max_tokens=max_tokens, **kw)
            raise

    async def json_complete(self, messages: List[Dict[str, Any]], *,
                            max_tokens: int = 2000,
                            default: Optional[Dict] = None, **kw) -> Tuple[Dict[str, Any], Usage]:
        text, usage = await self.complete(messages, max_tokens=max_tokens, **kw)
        return parse_json_loose(text, default=default), usage

    async def vision_json(self, messages: List[Dict[str, Any]], *, max_tokens: int = 2500,
                          default: Optional[Dict] = None) -> Tuple[Dict[str, Any], Usage]:
        """Images always go to the vision-capable model, whatever the text provider is."""
        return await self.vision.json_complete(messages, max_tokens=max_tokens,
                                               default=default)

    def spend_report(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            out["text"] = {"provider": settings.text_provider,
                           "model": getattr(self.text, "model", "?"),
                           "lanes": self.text.spend_report()}
        except Exception:  # noqa: BLE001
            pass
        try:
            out["vision"] = {"model": self.vision.model,
                             "lanes": self.vision.spend_report()}
        except Exception:  # noqa: BLE001
            pass
        return out


_llm: Optional[LLM] = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm
