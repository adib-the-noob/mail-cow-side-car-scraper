"""A tiny thread-safe TTL cache.

Endpoints run in FastAPI's threadpool, so a plain lock is enough. The goal is
only to avoid hammering the mailcow containers on every request.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, object]] = {}

    def get_or_set(self, key: str, factory: Callable[[], T]) -> T:
        if self.ttl <= 0:
            return factory()

        now = time.monotonic()
        with self._lock:
            hit = self._store.get(key)
            if hit is not None and now < hit[0]:
                return hit[1]  # type: ignore[return-value]

        # Compute outside the lock; a duplicated concurrent compute is fine.
        value = factory()
        with self._lock:
            self._store[key] = (now + self.ttl, value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)
