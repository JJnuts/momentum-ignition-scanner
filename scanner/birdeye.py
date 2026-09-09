"""Birdeye Data Services client.

Design:
  - Every call goes through `_request`: plan gating -> rate limit -> HTTP ->
    record raw response -> ledger CU -> retry policy -> return `data`.
  - Transport is a seam (`transport` callable) so tests run without network.
  - Plan gating raises EndpointUnavailable BEFORE any HTTP, so we never spend
    CU or hit 401/403 on endpoints the plan doesn't include.
  - CU is charged to the ledger only on HTTP 200 (assumption: failed calls are
    not billed; revisit if the dashboard disagrees).
  - Retries: 429 (honours Retry-After), 5xx, timeouts/connection errors.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from .ledger import CallRecord, CULedger
from .plans import ENDPOINT_PATH, PLANS, cu_cost, endpoint_available
from .ratelimit import TokenBucket
from .recorder import RawRecorder

log = logging.getLogger("birdeye")

# (status, body, headers)
TransportResult = tuple[int | None, Any, dict[str, str]]
Transport = Callable[[str, dict[str, Any], dict[str, str]], Awaitable[TransportResult]]


class BirdeyeError(RuntimeError):
    def __init__(self, endpoint: str, status: int | None, message: str, body: Any = None) -> None:
        super().__init__(f"{endpoint}: HTTP {status}: {message}")
        self.endpoint = endpoint
        self.status = status
        self.body = body


class EndpointUnavailable(BirdeyeError):
    """Endpoint not included in the configured plan (no HTTP call was made)."""


class RetryExhausted(BirdeyeError):
    pass


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        out[k] = v
    return out


class BirdeyeClient:
    def __init__(
        self,
        api_key: str,
        plan: str,
        base_url: str = "https://public-api.birdeye.so",
        recorder: RawRecorder | None = None,
        ledger: CULedger | None = None,
        transport: Transport | None = None,
        limiter: TokenBucket | None = None,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        timeout_s: float = 20.0,
        rate_safety: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if plan not in PLANS:
            raise ValueError(f"unknown plan {plan!r}")
        if not 0.1 <= rate_safety <= 1.0:
            raise ValueError("rate_safety must be in [0.1, 1.0]")
        self.api_key = api_key
        self.plan = plan
        self.base_url = base_url.rstrip("/")
        self.recorder = recorder
        self.ledger = ledger
        self._transport = transport or self._aiohttp_transport
        # Birdeye's per-second limiter is strict: at exactly plan rps we saw 429s on
        # 1/3 of calls (smoke 2026-09-08). Run at rate_safety * rps with no burst
        # on the 1 rps tier.
        rps = PLANS[plan]["rps"] * rate_safety
        self.effective_rps = rps
        self.limiter = limiter or TokenBucket(rps, capacity=1.0 if PLANS[plan]["rps"] <= 1 else None)
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._clock = clock
        self._sleep = sleep
        self._session: aiohttp.ClientSession | None = None
        self.calls = 0
        self.retries = 0
        self.cu_headers_seen: set[str] = set()

    # ---- lifecycle -------------------------------------------------------
    async def __aenter__(self) -> "BirdeyeClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _aiohttp_transport(self, path: str, params: dict[str, Any], headers: dict[str, str]) -> TransportResult:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        url = self.base_url + path
        async with self._session.get(url, params=params, headers=headers) as resp:
            text = await resp.text()
            try:
                body: Any = json.loads(text) if text else {}
            except json.JSONDecodeError:
                body = {"raw": text[:2000]}
            return resp.status, body, {k.lower(): v for k, v in resp.headers.items()}

    # ---- core ------------------------------------------------------------
    def _headers(self, chain: str) -> dict[str, str]:
        return {"X-API-KEY": self.api_key, "x-chain": chain, "accept": "application/json"}

    def _note_headers(self, headers: dict[str, str]) -> None:
        for name in headers:
            n = name.lower()
            if ("compute" in n or n.startswith("x-cu") or "credit" in n or "ratelimit" in n) and n not in self.cu_headers_seen:
                self.cu_headers_seen.add(n)
                log.info("birdeye response header seen: %s=%s", n, headers[name])

    async def _request(self, endpoint: str, chain: str, params: dict[str, Any], cu_size: dict[str, Any] | None = None) -> Any:
        if not endpoint_available(endpoint, self.plan):
            raise EndpointUnavailable(endpoint, None, f"not available on plan '{self.plan}'")
        path = ENDPOINT_PATH[endpoint]
        cu, verified = cu_cost(endpoint, **(cu_size or {}))
        params = _clean_params(params)
        headers = self._headers(chain)

        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire()
            t0 = self._clock()
            status: int | None
            resp_headers: dict[str, str] = {}
            try:
                status, body, resp_headers = await self._transport(path, params, headers)
            except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as e:
                status, body = None, {"error": repr(e)}
            latency_ms = int((self._clock() - t0) * 1000)
            self.calls += 1

            ok = status == 200 and isinstance(body, dict) and body.get("success", True) is not False
            if self.recorder is not None:
                self.recorder.record(endpoint, params, status, body, chain=chain, latency_ms=latency_ms)
            if self.ledger is not None:
                self.ledger.record(CallRecord(endpoint=endpoint, chain=chain, cu=cu, verified=verified,
                                              status=status, latency_ms=latency_ms, params=params, charged=ok))
            if ok:
                self._note_headers(resp_headers)
                return body.get("data")

            message = _error_message(body)
            retryable = status is None or status == 429 or (status is not None and status >= 500)
            if retryable and attempt <= self.max_retries:
                self.retries += 1
                delay = _retry_after(resp_headers) or self.backoff_base_s * (2 ** (attempt - 1))
                rl = {k: v for k, v in resp_headers.items() if "ratelimit" in k or k == "retry-after"}
                log.warning("%s %s -> %s (%s); retry %d/%d in %.1fs; headers=%s", endpoint, chain, status,
                            message, attempt, self.max_retries, delay, rl)
                await self._sleep(delay)
                continue
            if retryable:
                raise RetryExhausted(endpoint, status, message, body)
            raise BirdeyeError(endpoint, status, message, body)

    # ---- typed wrappers --------------------------------------------------
    async def token_list_v3(self, chain: str, sort_by: str = "liquidity", sort_type: str = "desc",
                            offset: int = 0, limit: int = 100, **filters: Any) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("token_list_v3 limit must be 1..100")
        data = await self._request("token_list_v3", chain,
                                   {"sort_by": sort_by, "sort_type": sort_type, "offset": offset, "limit": limit, **filters})
        return list((data or {}).get("items") or [])

    async def ohlcv_v3(self, chain: str, address: str, type_: str, time_from: int, time_to: int,
                       mode: str = "range", count_limit: int | None = None, currency: str = "usd") -> list[dict]:
        est_candles = count_limit or 200
        data = await self._request("ohlcv_v3", chain,
                                   {"address": address, "type": type_, "time_from": time_from, "time_to": time_to,
                                    "mode": mode, "count_limit": count_limit, "currency": currency},
                                   cu_size={"candles": est_candles})
        return list((data or {}).get("items") or [])

    async def txs_token_v3(self, chain: str, address: str, limit: int = 100, offset: int = 0,
                           tx_type: str = "swap", after_time: int | None = None, before_time: int | None = None,
                           source: str | None = None, owner: str | None = None) -> tuple[list[dict], bool]:
        if not 1 <= limit <= 100:
            raise ValueError("txs_token_v3 limit must be 1..100")
        data = await self._request("txs_token_v3", chain,
                                   {"address": address, "limit": limit, "offset": offset, "tx_type": tx_type,
                                    "sort_by": "block_unix_time", "sort_type": "desc",
                                    "after_time": after_time, "before_time": before_time,
                                    "source": source, "owner": owner})
        data = data or {}
        items = list(data.get("items") or [])
        has_next = bool(data.get("has_next", data.get("hasNext", False)))
        return items, has_next

    async def txs_token(self, chain: str, address: str, limit: int = 50, offset: int = 0,
                        tx_type: str = "swap") -> tuple[list[dict], bool]:
        if not 1 <= limit <= 50:
            raise ValueError("txs_token (v1) limit must be 1..50")
        data = await self._request("txs_token", chain,
                                   {"address": address, "limit": limit, "offset": offset,
                                    "tx_type": tx_type, "sort_type": "desc"})
        data = data or {}
        return list(data.get("items") or []), bool(data.get("hasNext", data.get("has_next", False)))

    async def trade_data_single(self, chain: str, address: str, frames: list[str] | None = None) -> dict:
        data = await self._request("trade_data_single", chain, {"address": address, "frames": frames})
        return dict(data or {})

    async def market_data_single(self, chain: str, address: str) -> dict:
        data = await self._request("market_data_single", chain, {"address": address})
        return dict(data or {})

    async def multi_price(self, chain: str, addresses: list[str], include_liquidity: bool = True) -> dict:
        if not 1 <= len(addresses) <= 100:
            raise ValueError("multi_price accepts 1..100 addresses")
        data = await self._request("multi_price", chain,
                                   {"list_address": addresses, "include_liquidity": include_liquidity},
                                   cu_size={"n": len(addresses)})
        return dict(data or {})

    async def token_overview(self, chain: str, address: str) -> dict:
        data = await self._request("token_overview", chain, {"address": address})
        return dict(data or {})

    async def token_security(self, chain: str, address: str) -> dict:
        data = await self._request("token_security", chain, {"address": address})
        return dict(data or {})

    async def token_creation_info(self, chain: str, address: str) -> dict:
        data = await self._request("token_creation_info", chain, {"address": address})
        return dict(data or {})

    async def new_listing(self, chain: str, limit: int = 20, time_to: int | None = None,
                          meme_platform_enabled: bool | None = None) -> list[dict]:
        data = await self._request("new_listing", chain,
                                   {"limit": limit, "time_to": time_to, "meme_platform_enabled": meme_platform_enabled})
        return list((data or {}).get("items") or [])

    # ---- wallet intelligence (verified 2026-09-09) -----------------------------------
    async def wallet_pnl_summary(self, chain: str, wallet: str, duration: str = "all",
                                 pnl_method: str = "net_cash") -> dict:
        data = await self._request("wallet_pnl_summary", chain,
                                   {"wallet": wallet, "duration": duration, "pnl_method": pnl_method})
        return dict(data or {})

    async def token_top_traders(self, chain: str, address: str, time_frame: str = "24h",
                                sort_by: str = "realized_pnl", sort_type: str = "desc",
                                offset: int = 0, limit: int = 10) -> list[dict]:
        if not 1 <= limit <= 10:
            raise ValueError("token_top_traders limit must be 1..10")
        data = await self._request("token_top_traders", chain,
                                   {"address": address, "time_frame": time_frame, "sort_by": sort_by,
                                    "sort_type": sort_type, "offset": offset, "limit": limit})
        return list((data or {}).get("items") or [])

    async def token_holder_profile(self, chain: str, address: str) -> dict:
        data = await self._request("token_holder_profile", chain, {"token_address": address})
        return dict(data or {})

    async def wallet_tags_tracker(self, chain: str, address: str, time_from: int, time_to: int | None = None,
                                  time_frame: str = "5m", tags: list[str] | None = None,
                                  top_10_holder: bool | None = None) -> dict:
        data = await self._request("wallet_tags_tracker", chain,
                                   {"token_address": address, "time_from": time_from, "time_to": time_to,
                                    "time_frame": time_frame, "tags": tags, "top_10_holder": top_10_holder})
        return dict(data or {})

    async def token_first_buyers(self, chain: str, address: str, offset: int = 0, limit: int = 70) -> list[dict]:
        if not 1 <= limit <= 100 or offset + limit > 1000:
            raise ValueError("token_first_buyers: limit 1..100 and offset+limit <= 1000")
        data = await self._request("token_first_buyers", chain,
                                   {"token_address": address, "offset": offset, "limit": limit})
        return list((data or {}).get("items") or [])

    async def smart_money_token_list(self, chain: str, interval: str = "1d", trader_style: str = "all",
                                     sort_by: str = "smart_traders_no", offset: int = 0, limit: int = 20) -> list[dict]:
        data = await self._request("smart_money_token_list", chain,
                                   {"interval": interval, "trader_style": trader_style, "sort_by": sort_by,
                                    "sort_type": "desc", "offset": offset, "limit": limit})
        return list((data or {}).get("items") or [])

    async def wallet_identity(self, chain: str, address: str) -> dict:
        data = await self._request("wallet_identity", chain, {"address": address})
        return dict(data or {})


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "msg", "raw"):
            if body.get(key):
                return str(body[key])[:300]
        if body.get("success") is False:
            return "success=false"
    return str(body)[:300]


def _retry_after(headers: dict[str, str]) -> float | None:
    v = headers.get("retry-after")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None
