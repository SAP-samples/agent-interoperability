"""Per-replica sliding-window limiter for SAP tool calls."""

from __future__ import annotations

import collections
import threading
import time

from runtime.config import RateLimitSettings


class RateLimitExceeded(RuntimeError):
    pass


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, user_email: str, settings: RateLimitSettings) -> None:
        now = time.time()
        cutoff = now - settings.window_seconds
        with self._lock:
            events = self._events.setdefault(user_email, collections.deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= settings.max_tool_calls:
                raise RateLimitExceeded("SAP tool-call rate limit exceeded")
            events.append(now)
