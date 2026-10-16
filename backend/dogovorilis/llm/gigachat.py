"""Асинхронный клиент GigaChat: OAuth-токен с кэшем, проверка TLS по корню Минцифры, повторы."""

import asyncio
import logging
import random
import ssl
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import certifi
import httpx

log = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1"


class LLMUnavailable(Exception):
    """Модель недоступна после всех повторов — сообщение должно остаться в очереди."""


@dataclass
class ChatResult:
    content: str
    model: str
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    attempts: int = 1
    raw: dict = field(default_factory=dict, repr=False)


def _ssl_context(ca_file: Path) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(str(ca_file))
    return ctx


class GigaChatClient:
    def __init__(self, auth_key: str, scope: str, ca_file: Path, timeout_s: float = 30.0,
                 max_attempts: int = 6, max_concurrency: int = 2):
        self._auth_key = auth_key
        self._scope = scope
        self._http = httpx.AsyncClient(verify=_ssl_context(ca_file), timeout=timeout_s)
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(max_concurrency)
        self._max_attempts = max_attempts

    async def close(self) -> None:
        await self._http.aclose()

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_exp - 60:
                return self._token
            r = await self._http.post(
                OAUTH_URL,
                headers={"Authorization": f"Basic {self._auth_key}", "RqUID": str(uuid.uuid4()),
                         "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={"scope": self._scope},
            )
            r.raise_for_status()
            body = r.json()
            self._token = body["access_token"]
            # expires_at приходит в миллисекундах (unix time)
            self._token_exp = body.get("expires_at", (time.time() + 1800) * 1000) / 1000
            return self._token

    async def chat(self, messages: list[dict], model: str, temperature: float = 0.01,
                   max_tokens: int = 512) -> ChatResult:
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "repetition_penalty": 1.0}
        last_err: Exception | None = None
        async with self._sem:
            for attempt in range(1, self._max_attempts + 1):
                t0 = time.perf_counter()
                try:
                    token = await self._get_token(force=attempt > 1 and isinstance(last_err, PermissionError))
                    r = await self._http.post(f"{API_URL}/chat/completions", json=payload,
                                              headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 401:
                        raise PermissionError("token expired")
                    if r.status_code == 429 or r.status_code >= 500:
                        raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                    r.raise_for_status()
                    body = r.json()
                    usage = body.get("usage", {})
                    return ChatResult(
                        content=body["choices"][0]["message"]["content"],
                        model=body.get("model", model),
                        latency_s=time.perf_counter() - t0,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                        attempts=attempt,
                        raw=body,
                    )
                except (httpx.TransportError, httpx.HTTPStatusError, PermissionError) as e:
                    last_err = e
                    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 \
                            and e.response.status_code != 429:
                        raise
                    delay = min(8.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.3)
                    log.warning("GigaChat attempt %d/%d failed: %r, retry in %.1fs",
                                attempt, self._max_attempts, e, delay)
                    if attempt < self._max_attempts:
                        await asyncio.sleep(delay)
        raise LLMUnavailable(repr(last_err))
