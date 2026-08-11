"""PumpPortal WebSocket feed — the primary real-time data source.

pump.fun has no official public API. PumpPortal is the de-facto third-party
feed and it is what almost every working bot uses. That makes it a single
point of failure, so `rpc_watcher.py` provides an independent path straight
from a Solana RPC node and the Scout runs both.

Feed rules that will bite you if ignored:
  - **One connection.** Opening a socket per token gets you IP banned. We
    multiplex every subscription over a single connection.
  - **New-token and migration streams are free.** Per-token and per-account
    trade streams are metered and billed in SOL, so the Scout caps how many
    wallets and tokens it subscribes to at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import websockets

from ..models import MigrationEvent, NewTokenEvent, TradeEvent

log = logging.getLogger(__name__)

WS_URL = "wss://pumpportal.fun/api/data"


def _f(data: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    """First present numeric key. Feed field names drift between versions, so
    we accept every spelling we have seen rather than crashing on a rename."""
    for key in keys:
        if key in data and data[key] is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                continue
    return default


def _s(data: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    return default


class PumpPortalFeed:
    """Single multiplexed WebSocket with dynamic subscription management."""

    def __init__(
        self,
        api_key: str | None = None,
        on_new_token: Callable[[NewTokenEvent], Any] | None = None,
        on_trade: Callable[[TradeEvent], Any] | None = None,
        on_wallet_trade: Callable[[TradeEvent], Any] | None = None,
        on_migration: Callable[[MigrationEvent], Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.on_new_token = on_new_token
        self.on_trade = on_trade
        self.on_wallet_trade = on_wallet_trade
        self.on_migration = on_migration

        self._ws: websockets.WindowsClientProtocol | Any = None
        self._tracked_wallets: set[str] = set()
        self._tracked_tokens: set[str] = set()
        self._pending: list[dict[str, Any]] = []
        self._running = False
        self._connected = asyncio.Event()

        self.messages_received = 0
        self.last_message_ts = 0.0

    @property
    def url(self) -> str:
        return f"{WS_URL}?api-key={self.api_key}" if self.api_key else WS_URL

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # -- subscription management -------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or not self._connected.is_set():
            # Queue it; _resubscribe replays the queue once we reconnect.
            self._pending.append(payload)
            return
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001
            log.warning("subscription send failed (%s); queued for reconnect", exc)
            self._pending.append(payload)

    async def subscribe_new_tokens(self) -> None:
        await self._send({"method": "subscribeNewToken"})

    async def subscribe_migrations(self) -> None:
        await self._send({"method": "subscribeMigration"})

    async def subscribe_wallets(self, addresses: list[str]) -> None:
        """Watch specific wallets' trades. Metered — costs SOL per event batch."""
        fresh = [a for a in addresses if a and a not in self._tracked_wallets]
        if not fresh:
            return
        self._tracked_wallets.update(fresh)
        await self._send({"method": "subscribeAccountTrade", "keys": fresh})
        log.info("subscribed to %d wallet(s), %d tracked total", len(fresh), len(self._tracked_wallets))

    async def unsubscribe_wallets(self, addresses: list[str]) -> None:
        drop = [a for a in addresses if a in self._tracked_wallets]
        if not drop:
            return
        self._tracked_wallets.difference_update(drop)
        await self._send({"method": "unsubscribeAccountTrade", "keys": drop})

    async def subscribe_tokens(self, mints: list[str]) -> None:
        """Watch trades on specific tokens — used for positions we hold and
        candidates we are still evaluating."""
        fresh = [m for m in mints if m and m not in self._tracked_tokens]
        if not fresh:
            return
        self._tracked_tokens.update(fresh)
        await self._send({"method": "subscribeTokenTrade", "keys": fresh})

    async def unsubscribe_tokens(self, mints: list[str]) -> None:
        drop = [m for m in mints if m in self._tracked_tokens]
        if not drop:
            return
        self._tracked_tokens.difference_update(drop)
        await self._send({"method": "unsubscribeTokenTrade", "keys": drop})

    async def _resubscribe(self) -> None:
        """Restore all subscriptions after a reconnect.

        Without this, a dropped socket silently stops the feed for every
        wallet you were copying and the bot just goes quiet.
        """
        await self._send({"method": "subscribeNewToken"})
        await self._send({"method": "subscribeMigration"})
        if self._tracked_wallets:
            await self._send(
                {"method": "subscribeAccountTrade", "keys": sorted(self._tracked_wallets)}
            )
        if self._tracked_tokens:
            await self._send(
                {"method": "subscribeTokenTrade", "keys": sorted(self._tracked_tokens)}
            )

        queued, self._pending = self._pending, []
        for payload in queued:
            await self._send(payload)

    # -- message handling ---------------------------------------------------

    def _parse(self, data: dict[str, Any]) -> None:
        """Route one feed message to the right callback.

        The feed distinguishes events by which fields are present rather than
        an explicit type field, so we sniff.
        """
        import time

        self.messages_received += 1
        self.last_message_ts = time.time()

        # Subscription acks and errors.
        if "message" in data and "mint" not in data:
            text = str(data["message"])
            if "error" in text.lower() or "invalid" in text.lower():
                log.warning("feed said: %s", text)
            else:
                log.debug("feed said: %s", text)
            return

        mint = _s(data, "mint", "ca", "token")
        if not mint:
            return

        tx_type = _s(data, "txType", "type").lower()

        # --- new token ------------------------------------------------------
        if tx_type in ("create", "created", "new_token"):
            event = NewTokenEvent(
                mint=mint,
                name=_s(data, "name"),
                symbol=_s(data, "symbol", "ticker"),
                creator=_s(data, "traderPublicKey", "creator", "user"),
                uri=_s(data, "uri", "metadataUri"),
                initial_buy_sol=_f(data, "solAmount", "initialBuy"),
                sol_in_curve=_f(data, "vSolInBondingCurve", "solInPool", "realSolReserves"),
                market_cap_sol=_f(data, "marketCapSol", "marketCap"),
                signature=_s(data, "signature"),
                pool=_s(data, "pool", default="pump"),
                raw=data,
            )
            if self.on_new_token:
                asyncio.create_task(_maybe_await(self.on_new_token, event))
            return

        # --- migration ------------------------------------------------------
        if "migrat" in tx_type or _s(data, "pool") in ("raydium", "pump-amm") and tx_type == "":
            event = MigrationEvent(mint=mint, pool=_s(data, "pool", default="raydium"), raw=data)
            if self.on_migration:
                asyncio.create_task(_maybe_await(self.on_migration, event))
            return

        # --- trade ----------------------------------------------------------
        if tx_type in ("buy", "sell"):
            trader = _s(data, "traderPublicKey", "trader", "user")
            event = TradeEvent(
                mint=mint,
                trader=trader,
                is_buy=(tx_type == "buy"),
                sol_amount=_f(data, "solAmount"),
                token_amount=_f(data, "tokenAmount", "initialBuy"),
                sol_in_curve=_f(data, "vSolInBondingCurve", "realSolReserves", "solInPool"),
                market_cap_sol=_f(data, "marketCapSol", "marketCap"),
                signature=_s(data, "signature"),
                slot=int(_f(data, "slot")),
                raw=data,
            )
            # A trade by a wallet we deliberately track is a copy-trade signal,
            # not just market colour, so it gets its own channel.
            if trader in self._tracked_wallets and self.on_wallet_trade:
                asyncio.create_task(_maybe_await(self.on_wallet_trade, event))
            elif self.on_trade:
                asyncio.create_task(_maybe_await(self.on_trade, event))

    # -- run loop -----------------------------------------------------------

    async def run(self) -> None:
        """Connect and stay connected. Returns only when stop() is called."""
        self._running = True
        backoff = 1.0

        while self._running:
            try:
                log.info("connecting to PumpPortal feed")
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=2048,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = 1.0
                    log.info("PumpPortal feed connected")
                    await self._resubscribe()

                    async for raw in ws:
                        try:
                            self._parse(json.loads(raw))
                        except json.JSONDecodeError:
                            continue
                        except Exception as exc:  # noqa: BLE001
                            log.warning("error handling feed message: %s", exc)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("PumpPortal feed dropped (%s); reconnecting in %.0fs", exc, backoff)
            finally:
                self._connected.clear()
                self._ws = None

            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


async def _maybe_await(fn: Callable[..., Any], *args: Any) -> None:
    try:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:  # noqa: BLE001
        log.exception("feed callback failed: %s", exc)
