"""Async Solana JSON-RPC client with failover.

Two things this handles that a naive `requests.post` does not:

  - **Rate limits.** The public endpoint allows a handful of requests per
    second and will 429 you constantly. We back off, and if a fallback URL is
    configured we fail over to it rather than dropping the read.
  - **Retries that respect intent.** Reads are retried freely. Transaction
    sends are NOT retried blindly, because a "failed" send may actually have
    landed, and resending can buy the same token twice.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import httpx

from .curve import BondingCurve, decode_curve, derive_bonding_curve_address

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class SolanaRPC:
    def __init__(
        self,
        url: str,
        fallback_url: str | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self.url = url
        self.fallback_url = fallback_url
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        self._using_fallback = False

    async def __aenter__(self) -> SolanaRPC:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("SolanaRPC.connect() must be awaited before use")
        return self._client

    @property
    def active_url(self) -> str:
        return self.fallback_url if (self._using_fallback and self.fallback_url) else self.url

    async def call(self, method: str, params: list[Any] | None = None, retries: int | None = None) -> Any:
        """Make an RPC call, retrying transient failures."""
        retries = self.max_retries if retries is None else retries
        self._request_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }

        delay = 0.4
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            url = self.active_url
            try:
                resp = await self.client.post(url, json=body)
                if resp.status_code == 429:
                    raise RpcError("rate limited (429)")
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    err = data["error"]
                    # Application-level errors are deterministic; retrying an
                    # invalid-argument does not make it valid.
                    raise RpcError(f"{method}: {err.get('message', err)}")
                return data.get("result")
            except (httpx.HTTPError, RpcError) as exc:
                last_err = exc
                transient = isinstance(exc, httpx.HTTPError) or "rate limited" in str(exc)
                if not transient or attempt >= retries:
                    break
                # Alternate endpoints on failure when we have a second one.
                if self.fallback_url:
                    self._using_fallback = not self._using_fallback
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

        raise RpcError(f"RPC {method} failed after {retries + 1} attempts: {last_err}")

    # -- account reads ------------------------------------------------------

    async def get_account_data(self, address: str, commitment: str = "confirmed") -> bytes | None:
        result = await self.call(
            "getAccountInfo",
            [address, {"encoding": "base64", "commitment": commitment}],
        )
        value = (result or {}).get("value")
        if not value:
            return None
        return base64.b64decode(value["data"][0])

    async def get_multiple_account_data(
        self, addresses: list[str], commitment: str = "confirmed"
    ) -> list[bytes | None]:
        """Batch read — one round trip for many accounts.

        Pricing every open position individually is what gets you rate
        limited; this is how you price a whole portfolio in one request.
        """
        if not addresses:
            return []
        out: list[bytes | None] = []
        # getMultipleAccounts caps at 100 addresses per call.
        for i in range(0, len(addresses), 100):
            chunk = addresses[i : i + 100]
            result = await self.call(
                "getMultipleAccounts",
                [chunk, {"encoding": "base64", "commitment": commitment}],
            )
            for value in (result or {}).get("value", []):
                out.append(base64.b64decode(value["data"][0]) if value else None)
        return out

    async def get_bonding_curve(
        self, mint: str, commitment: str = "processed"
    ) -> BondingCurve | None:
        """Fetch and decode the curve for a mint. None if it does not exist
        (not a pump token, or already migrated)."""
        address = derive_bonding_curve_address(mint)
        data = await self.get_account_data(address, commitment)
        if data is None:
            return None
        try:
            return decode_curve(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("curve decode failed for %s: %s", mint, exc)
            return None

    async def get_bonding_curves(
        self, mints: list[str], commitment: str = "processed"
    ) -> dict[str, BondingCurve | None]:
        """Batch version — used by the position pricing loop."""
        if not mints:
            return {}
        addresses = [derive_bonding_curve_address(m) for m in mints]
        blobs = await self.get_multiple_account_data(addresses, commitment)
        out: dict[str, BondingCurve | None] = {}
        for mint, data in zip(mints, blobs, strict=False):
            if data is None:
                out[mint] = None
                continue
            try:
                out[mint] = decode_curve(data)
            except Exception:  # noqa: BLE001
                out[mint] = None
        return out

    async def get_balance_sol(self, address: str) -> float:
        result = await self.call("getBalance", [address, {"commitment": "confirmed"}])
        return (result or {}).get("value", 0) / 1_000_000_000

    async def get_token_supply(self, mint: str) -> dict[str, Any]:
        result = await self.call("getTokenSupply", [mint, {"commitment": "confirmed"}])
        return (result or {}).get("value", {})

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        """Top 20 holders — the basis for concentration checks."""
        result = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return (result or {}).get("value", [])

    async def get_token_account_balance(self, token_account: str) -> float:
        result = await self.call(
            "getTokenAccountBalance", [token_account, {"commitment": "confirmed"}]
        )
        value = (result or {}).get("value", {})
        return float(value.get("uiAmount") or 0.0)

    async def get_token_accounts_by_owner(self, owner: str, mint: str) -> list[dict[str, Any]]:
        result = await self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        return (result or {}).get("value", [])

    async def get_signatures_for_address(
        self, address: str, limit: int = 100, before: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
        if before:
            params["before"] = before
        return await self.call("getSignaturesForAddress", [address, params]) or []

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    async def get_latest_blockhash(self) -> str:
        result = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return (result or {}).get("value", {}).get("blockhash", "")

    async def get_slot(self) -> int:
        return await self.call("getSlot", [{"commitment": "confirmed"}]) or 0

    async def get_health(self) -> str:
        try:
            return await self.call("getHealth", [], retries=0) or "unknown"
        except Exception as exc:  # noqa: BLE001
            return f"unhealthy: {exc}"

    # -- transactions -------------------------------------------------------

    async def send_transaction(self, signed_tx_b64: str, skip_preflight: bool = True) -> str:
        """Submit a signed transaction. Deliberately NOT retried.

        A timeout here does not mean the transaction failed — it may well have
        landed. Resending risks a duplicate buy, so we surface the error and
        let the caller reconcile against the chain instead.

        skip_preflight defaults True: preflight simulation costs ~100-200ms,
        which is the difference between getting a fill and getting front-run.
        """
        return await self.call(
            "sendTransaction",
            [
                signed_tx_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "maxRetries": 3,
                    "preflightCommitment": "processed",
                },
            ],
            retries=0,
        )

    async def confirm_transaction(
        self, signature: str, timeout_s: float = 45.0, poll_s: float = 1.5
    ) -> dict[str, Any]:
        """Poll until a signature confirms, fails, or we give up.

        Returns {"confirmed": bool, "err": ..., "slot": ...}. A `False` with
        err=None means we timed out and genuinely do not know — the caller
        must reconcile against the wallet, not assume either outcome.
        """
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            try:
                result = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
                statuses = (result or {}).get("value", [])
                status = statuses[0] if statuses else None
                if status:
                    if status.get("err"):
                        return {"confirmed": False, "err": status["err"], "slot": status.get("slot")}
                    conf = status.get("confirmationStatus")
                    if conf in ("confirmed", "finalized"):
                        return {"confirmed": True, "err": None, "slot": status.get("slot")}
            except Exception as exc:  # noqa: BLE001
                log.debug("confirmation poll error for %s: %s", signature, exc)
            await asyncio.sleep(poll_s)
        return {"confirmed": False, "err": None, "slot": None, "timeout": True}
