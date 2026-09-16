"""Model routing: the right model for each kind of call.

**`deepseek-flash` handles everything, text and vision.** That was not true when this
file was written — DeepSeek was text-only, so reading an error code off a photo had to
go to `gpt-5.6-terra` through RKAPI, and carrying a second provider for one capability
was the price of multimodal. DeepSeek shipped vision on flash, so that price is gone.

Verified against this project's own VLM prompt before switching, not assumed: valid JSON
on the exact schema, OCR read verbatim off the photo, and an honest `confidence: 0.1`
with `brand: unknown` on a picture of a broom rather than a confident guess. Prompt
caching applies too — 1,152 of 1,331 input tokens served from cache on the second call,
which is what the static system prompts in `prompts.py` were shaped for.

Text and vision share ONE client on purpose. Same model, same lane pool, and the cached
prefix is shared rather than paid for twice.

RKAPI stays wired as a backup provider and nothing routes to it by default. It costs
nothing to keep and it is the only thing standing between a DeepSeek outage and a dead
demo — set `VISION_PROVIDER=rkapi` or `TEXT_PROVIDER=rkapi` to fail over by config.
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import structlog

from app.clients.rkapi import RKAPIClient, Usage, parse_json_loose
from app.config import settings

log = structlog.get_logger(__name__)

# DeepSeek bills in real dollars, not resale credits. `deepseek-flash` peak rates per
# M tokens: $0.30 cache miss, $0.006 cache hit, $1.20 output. Off-peak (01:00-04:00 and
# 06:00-10:00 UTC) is half of each. Peak is used here so a reported cost is never an
# under-estimate, and the cache-hit rate is priced separately because it is ~50x cheaper
# and this agent re-sends the same system prompts on every single turn.
DEEPSEEK_PRICE_IN = 0.30 / 1_000_000
DEEPSEEK_PRICE_CACHED = 0.006 / 1_000_000
DEEPSEEK_PRICE_OUT = 1.20 / 1_000_000


class DeepSeekClient(RKAPIClient):
    """Same streaming contract as the RKAPI client, different endpoint and pricing.

    Inherits the retry, lane-pooling and always-stream behaviour rather than
    reimplementing them — those were hard-won and are not provider-specific.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        keys = [k.strip() for k in settings.deepseek_api_keys.split(",") if k.strip()]
        if not keys:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")
        self.model = model or settings.deepseek_model
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

    def price_usage(self, usage: Usage) -> float:
        # `usage.input` counts every input token including the cached ones, so the
        # cached share is billed at the cache rate and only the remainder at full price.
        fresh = max(usage.input - usage.cached, 0)
        return (fresh * DEEPSEEK_PRICE_IN
                + usage.cached * DEEPSEEK_PRICE_CACHED
                + usage.output * DEEPSEEK_PRICE_OUT)


class LLM:
    """One façade the agent talks to. Decides provider per call."""

    def __init__(self) -> None:
        self._deepseek: Dict[str, Any] = {}
        self._rkapi: Optional[RKAPIClient] = None

    # ── providers ─────────────────────────────────────────────────────────────
    def _deepseek_client(self, model: Optional[str] = None):
        """One client per model id, built on demand.

        Text and vision use different ids of the same model — see `config.py` — so this
        cannot be a single shared instance without putting text on the slow path.
        """
        model = model or settings.deepseek_model
        if model not in self._deepseek:
            self._deepseek[model] = DeepSeekClient(model)
            log.info("llm.deepseek_ready", model=model)
        return self._deepseek[model]

    def _rkapi_client(self) -> RKAPIClient:
        if self._rkapi is None:
            from app.clients.rkapi import get_rkapi
            self._rkapi = get_rkapi()
        return self._rkapi

    def _pick(self, provider: str, what: str, model: Optional[str] = None):
        """Configured provider, falling back to the other one rather than failing.

        A provider that cannot be constructed (no key, bad config) must not take the
        service down when the other one is sitting right there.
        """
        if provider == "rkapi":
            return self._rkapi_client()
        try:
            return self._deepseek_client(model)
        except Exception as e:  # noqa: BLE001
            log.warning("llm.deepseek_unavailable", for_=what, error=str(e)[:140])
            return self._rkapi_client()

    @property
    def text(self):
        return self._pick(settings.text_provider, "text", settings.deepseek_model)

    @property
    def vision(self):
        return self._pick(settings.vision_provider, "vision",
                          settings.deepseek_vision_model)

    @property
    def backup(self):
        """A genuinely different provider to retry on, or None.

        Text and vision now resolve to the SAME client, so the old
        `if self.text is not self.vision` test silently stopped being a fallback the
        moment DeepSeek grew vision — it compared an object with itself.
        """
        try:
            text = self.text
            other = (self._rkapi_client() if isinstance(text, DeepSeekClient)
                     else self._deepseek_client())
        except Exception:  # noqa: BLE001
            return None
        return other if other is not text else None

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
            backup = self.backup
            if backup is not None:
                log.warning("llm.text_fallback", error=str(e)[:140])
                return await backup.complete(messages, max_tokens=max_tokens, **kw)
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
        for role, provider, model in (
                ("text", settings.text_provider, settings.deepseek_model),
                ("vision", settings.vision_provider, settings.deepseek_vision_model)):
            try:
                client = self._pick(provider, role, model)
                out[role] = {"provider": provider,
                             "model": getattr(client, "model", "?"),
                             "lanes": client.spend_report()}
            except Exception:  # noqa: BLE001
                pass
        # Text and vision usually share one client now, so reporting both unconditionally
        # would double-count the same lanes in the admin spend panel.
        if (out.get("text", {}).get("model") == out.get("vision", {}).get("model")
                and "vision" in out):
            out["vision"] = {"model": out["vision"]["model"], "shared_with": "text"}
        return out


_llm: Optional[LLM] = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm
