"""Process-bounded health probes; never replace a still-running refresh."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from threading import Lock


class StatusProbeCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = 30, wait_seconds: float = 2,
                 requests_per_minute: int = 60) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._wait = wait_seconds
        self._limit = requests_per_minute
        self._lock = Lock()
        self._requests: deque[float] = deque()
        self._executor = ThreadPoolExecutor(max_workers=11, thread_name_prefix="status-probe")
        self._pending: dict[str, Future] = {}
        self._pending_expires = 0.0
        self._cached: dict[str, bool] | None = None
        self._expires = 0.0

    def read(self, checkers: dict[str, Callable[[], bool]]) -> tuple[dict[str, bool] | None, int]:
        with self._lock:
            now = self._clock()
            while self._requests and self._requests[0] <= now - 60:
                self._requests.popleft()
            if len(self._requests) >= self._limit:
                return None, max(1, math.ceil(self._requests[0] + 60 - now))
            self._requests.append(now)
            self._collect()
            if self._cached is not None and now < self._expires:
                return dict(self._cached), 0
            if not self._pending:
                # Bound result age from dispatch, including queue and probe time.
                self._pending_expires = now + self._ttl
                self._pending = {name: self._executor.submit(fn) for name, fn in checkers.items()}
            pending = dict(self._pending)
            expires = self._pending_expires

        # Wait outside the lock; all concurrent requests share this same bounded batch.
        wait(pending.values(), timeout=self._wait)
        with self._lock:
            now = self._clock()
            self._collect()
            # Neither a hung peer nor delayed collection can renew a success.
            checks = {name: now < expires and self._healthy(future)
                      for name, future in pending.items()}
            return checks, 0

    @staticmethod
    def _healthy(future: Future) -> bool:
        return (future.done() and not future.cancelled() and future.exception() is None
                and future.result() is True)

    def _collect(self) -> None:
        if self._pending and all(future.done() for future in self._pending.values()):
            self._cached = {name: self._healthy(future) for name, future in self._pending.items()}
            self._expires = self._pending_expires
            self._pending = {}

    def close(self) -> None:
        self._executor.shutdown(wait=True)
