"""Abuse protection for the public demo, kept in Redis: rate limits, LLM budget, idempotency.

Per IP limits live in nginx (deploy/nginx); these are the ones only the app can enforce.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from redis.asyncio import Redis

from support_agent.llm import LLM, Classification, Confirmation, ConversationMessage


class RateLimited(Exception):
    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds


class ConversationRateLimit:
    """At most `turns` turns per Conversation in a fixed window of `window_seconds`."""

    def __init__(self, redis: Redis, turns: int, window_seconds: int) -> None:
        self._redis = redis
        self._turns = turns
        self._window = window_seconds

    async def take(self, conversation_id: uuid.UUID) -> None:
        """Count a turn; raises RateLimited, without counting it, once the window is full."""
        key = f"rate:conversation:{conversation_id}"
        count = await _count(self._redis, key, self._window)
        if count > self._turns:
            await self._redis.decr(key)
            ttl = await self._redis.ttl(key)
            raise RateLimited(ttl if ttl > 0 else self._window)


class LLMBudget:
    """A cap on LLM calls per (UTC) day, across all Customers.

    Checked before a turn and counted per call, so the last turn of the day may go a few
    calls over the cap rather than being cut off halfway.
    """

    _KEY_PREFIX = "llm_calls:"

    def __init__(self, redis: Redis, calls_per_day: int) -> None:
        self._redis = redis
        self._cap = calls_per_day

    async def exhausted(self) -> bool:
        return int(await self._redis.get(self.key(datetime.now(UTC).date())) or 0) >= self._cap

    async def check(self) -> None:
        if await self.exhausted():
            now = datetime.now(UTC)
            midnight = datetime.combine(now.date() + timedelta(days=1), time(), UTC)
            raise RateLimited(max(1, int((midnight - now).total_seconds())))

    async def count(self) -> None:
        key = self.key(datetime.now(UTC).date())
        await _count(self._redis, key, int(timedelta(days=2).total_seconds()))

    @staticmethod
    def key(day: date) -> str:
        return f"{LLMBudget._KEY_PREFIX}{day.isoformat()}"

    @staticmethod
    def owns(key: str) -> bool:
        """Whether a Redis key is one of the budget's daily counters."""
        return key.startswith(LLMBudget._KEY_PREFIX)


async def _count(redis: Redis, key: str, ttl_seconds: int) -> int:
    """Increment a counter, starting its TTL on the first increment, atomically."""
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        count, _ = await pipe.execute()
    return int(count)


class MeteredLLM:
    """An LLM that counts every call against the budget."""

    def __init__(self, llm: LLM, count: Callable[[], Awaitable[None]]) -> None:
        self._llm = llm
        self._count = count

    async def classify(self, history: Sequence[ConversationMessage]) -> Classification:
        await self._count()
        return await self._llm.classify(history)

    async def classify_confirmation(self, history: Sequence[ConversationMessage]) -> Confirmation:
        await self._count()
        return await self._llm.classify_confirmation(history)

    async def respond(self, history: Sequence[ConversationMessage], facts: str) -> str:
        await self._count()
        return await self._llm.respond(history, facts)


class IdempotencyKeyReused(Exception):
    """The key was already used by this Customer for a different request."""


class IdempotencyKeyInFlight(Exception):
    """The first request with this key is still running."""


class Idempotency:
    """Remembers the response to a request by (Customer, Idempotency-Key) for `ttl_seconds`."""

    _IN_FLIGHT = "in-flight"

    def __init__(self, redis: Redis, ttl_seconds: int, in_flight_ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        # Short, so a claim whose process died mid-turn doesn't answer 409 for a day.
        self._in_flight_ttl = in_flight_ttl_seconds

    async def claim(
        self, customer_id: str, key: str, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The stored response to replay, or None once this request may run (then `settle`)."""
        redis_key = self._key(customer_id, key)
        fingerprint = _fingerprint(request)
        entry = json.dumps({"fingerprint": fingerprint, "response": self._IN_FLIGHT})
        if await self._redis.set(redis_key, entry, ex=self._in_flight_ttl, nx=True):
            return None
        stored = await self._redis.get(redis_key)
        if stored is None:  # expired in between: run it
            return await self.claim(customer_id, key, request)
        found = json.loads(stored)
        if found["fingerprint"] != fingerprint:
            raise IdempotencyKeyReused
        if found["response"] == self._IN_FLIGHT:
            raise IdempotencyKeyInFlight
        response: dict[str, Any] = found["response"]
        return response

    async def settle(
        self, customer_id: str, key: str, request: dict[str, Any], response: dict[str, Any]
    ) -> None:
        entry = json.dumps({"fingerprint": _fingerprint(request), "response": response})
        await self._redis.set(self._key(customer_id, key), entry, ex=self._ttl)

    async def release(self, customer_id: str, key: str) -> None:
        """Forget a claim whose request failed, so a retry runs it again."""
        await self._redis.delete(self._key(customer_id, key))

    @staticmethod
    def _key(customer_id: str, key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return f"idempotency:{customer_id}:{digest}"


def _fingerprint(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
