"""Redis-backed message bus connecting the three devices.

Why Redis and not direct sockets: the three machines need to find each other,
survive one another's restarts, and replay what they missed. Redis pub/sub
gives the live path and Redis Streams give a durable journal, with one
`apt install` on one machine.

If Redis is unreachable the bus reconnects forever with backoff rather than
crashing the service — a Scout that cannot publish is still worth keeping
alive, because the network usually comes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, TypeVar

import orjson
import redis.asyncio as aioredis
from pydantic import BaseModel

from .models import TOPIC_MODELS, Envelope, Topic

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

JOURNAL_STREAM = "mctb.journal"


class Bus:
    """Async pub/sub + durable journal.

    Usage:
        bus = Bus(url, password, source="scout")
        await bus.connect()
        await bus.publish(Topic.TOKEN_NEW, event)
        async for topic, msg in bus.subscribe(Topic.TOKEN_NEW):
            ...
    """

    def __init__(
        self,
        url: str,
        password: str | None = None,
        source: str = "unknown",
        journal_maxlen: int = 200_000,
    ) -> None:
        self.url = url
        self.password = password or None
        self.source = source
        self.journal_maxlen = journal_maxlen
        self._redis: aioredis.Redis | None = None
        self._closed = False

    # -- lifecycle ----------------------------------------------------------

    async def connect(self, retries: int = 5) -> None:
        delay = 1.0
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                self._redis = aioredis.from_url(
                    self.url,
                    password=self.password,
                    decode_responses=False,
                    socket_keepalive=True,
                    health_check_interval=15,
                )
                await self._redis.ping()
                log.info("bus connected to %s", self._safe_url())
                return
            except Exception as exc:  # noqa: BLE001 - report any connection failure
                last_err = exc
                log.warning(
                    "bus connect attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt,
                    retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise ConnectionError(
            f"Could not reach Redis at {self._safe_url()} after {retries} attempts: {last_err}\n"
            "Check: is redis-server running? Is MCTB_REDIS_URL pointing at the right LAN IP? "
            "Did you set `bind 0.0.0.0` and `protected-mode no` (or a password) in redis.conf?"
        )

    def _safe_url(self) -> str:
        """URL with any inline credentials stripped, for logging."""
        if "@" in self.url:
            scheme, _, rest = self.url.partition("://")
            return f"{scheme}://***@{rest.rpartition('@')[2]}"
        return self.url

    async def close(self) -> None:
        self._closed = True
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("Bus.connect() must be awaited before use")
        return self._redis

    # -- publish ------------------------------------------------------------

    async def publish(self, topic: Topic | str, message: BaseModel, journal: bool = True) -> None:
        """Publish a model. Never raises on transport failure — logs instead.

        A publish failure must not take down a trading loop; the caller has
        already made its decision and dropping a status message is survivable.
        """
        name = topic.value if isinstance(topic, Topic) else topic
        if isinstance(message, Envelope) and message.source == "unknown":
            message.source = self.source
        payload = message.model_dump_json().encode()
        try:
            pipe = self.redis.pipeline()
            pipe.publish(name, payload)
            if journal:
                pipe.xadd(
                    JOURNAL_STREAM,
                    {b"topic": name.encode(), b"data": payload},
                    maxlen=self.journal_maxlen,
                    approximate=True,
                )
            await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.error("bus publish to %s failed: %s", name, exc)

    # -- subscribe ----------------------------------------------------------

    async def subscribe(
        self, *topics: Topic | str
    ) -> AsyncIterator[tuple[str, BaseModel]]:
        """Yield (topic, parsed_model) forever, reconnecting as needed."""
        names = [t.value if isinstance(t, Topic) else t for t in topics]
        while not self._closed:
            pubsub = None
            try:
                pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(*names)
                log.info("subscribed to %s", ", ".join(names))
                async for raw in pubsub.listen():
                    if raw is None or raw.get("type") != "message":
                        continue
                    channel = raw["channel"]
                    topic = channel.decode() if isinstance(channel, bytes) else channel
                    model_cls = TOPIC_MODELS.get(topic)
                    if model_cls is None:
                        continue
                    try:
                        yield topic, model_cls.model_validate_json(raw["data"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("undecodable message on %s: %s", topic, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("subscription dropped (%s); reconnecting in 2s", exc)
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()

    # -- shared key/value ---------------------------------------------------
    # Small pieces of cross-device state that are simpler as a key than as a
    # message: the kill switch, the tracked-wallet set, latest prices.

    async def set_json(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        try:
            await self.redis.set(key, orjson.dumps(value), ex=ttl_s)
        except Exception as exc:  # noqa: BLE001
            log.error("bus set_json(%s) failed: %s", key, exc)

    async def get_json(self, key: str, default: Any = None) -> Any:
        try:
            raw = await self.redis.get(key)
            return orjson.loads(raw) if raw else default
        except Exception as exc:  # noqa: BLE001
            log.error("bus get_json(%s) failed: %s", key, exc)
            return default

    async def set_kill_switch(self, engaged: bool, reason: str = "") -> None:
        await self.set_json("mctb:kill_switch", {"engaged": engaged, "reason": reason})

    async def kill_switch_engaged(self) -> tuple[bool, str]:
        data = await self.get_json("mctb:kill_switch", {"engaged": False, "reason": ""})
        return bool(data.get("engaged")), str(data.get("reason", ""))

    async def replay(self, count: int = 500, topic: str | None = None) -> list[tuple[str, Any]]:
        """Read recent journal entries — used by the Console on startup so the
        dashboard is not blank until the next event arrives."""
        try:
            entries = await self.redis.xrevrange(JOURNAL_STREAM, count=count)
        except Exception as exc:  # noqa: BLE001
            log.error("journal replay failed: %s", exc)
            return []
        out: list[tuple[str, Any]] = []
        for _entry_id, fields in reversed(entries):
            name = fields.get(b"topic", b"").decode()
            if topic and name != topic:
                continue
            model_cls = TOPIC_MODELS.get(name)
            if model_cls is None:
                continue
            try:
                out.append((name, model_cls.model_validate_json(fields[b"data"])))
            except Exception:  # noqa: BLE001
                continue
        return out
