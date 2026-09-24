"""In-memory sliding-window rate limiter keyed by client identifier."""
from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    @staticmethod
    def _key(client_id: str) -> str:
        # Store a hash, never the raw IP address, so memory holds no personal data.
        return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:24]

    def allow(self, client_id: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        key = self._key(client_id)
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory under many distinct clients
                stale = [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]
                for key in stale:
                    del self._hits[key]
            return True
