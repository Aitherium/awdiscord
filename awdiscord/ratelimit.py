"""Token buckets: one per user, one per server, one for the whole bot.

A request must find a token in ALL THREE. The per-user bucket stops one
person draining the agent; the per-server bucket stops one busy server
starving the rest; the global bucket is what the agent behind it can afford.
A refusal says how long to wait, so the reply can say it too.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    stamp: float


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    retry_after: float = 0.0
    scope: str = ""


class RateLimiter:
    def __init__(self, per_user: tuple[int, float] = (5, 60.0),
                 per_guild: tuple[int, float] = (30, 60.0),
                 global_: tuple[int, float] = (120, 60.0),
                 clock=time.monotonic):
        """Each limit is (burst, window_seconds): `burst` requests, refilled
        evenly over `window_seconds`."""
        self._limits = {"user": per_user, "guild": per_guild, "global": global_}
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._clock = clock
        self._lock = threading.Lock()

    def _peek(self, scope: str, key: str, now: float) -> tuple[_Bucket, float]:
        burst, window = self._limits[scope]
        rate = burst / window
        b = self._buckets.get((scope, key))
        if b is None:
            b = _Bucket(tokens=float(burst), stamp=now)
            self._buckets[(scope, key)] = b
        b.tokens = min(float(burst), b.tokens + (now - b.stamp) * rate)
        b.stamp = now
        wait = 0.0 if b.tokens >= 1.0 else (1.0 - b.tokens) / rate
        return b, wait

    def check(self, user_id: str, guild_id: str | None = None) -> Verdict:
        """Take one token from every bucket, or none of them."""
        with self._lock:
            now = self._clock()
            keys = [("user", str(user_id)), ("global", "*")]
            if guild_id:
                keys.insert(1, ("guild", str(guild_id)))
            peeked = [(scope, *self._peek(scope, key, now)) for scope, key in keys]
            worst = max(peeked, key=lambda p: p[2])
            if worst[2] > 0:
                return Verdict(False, round(worst[2], 1), worst[0])
            for _, bucket, _ in peeked:
                bucket.tokens -= 1.0
            return Verdict(True)
