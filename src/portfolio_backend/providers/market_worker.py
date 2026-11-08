"""Resident fetch pool for AKShare quotes/FX.

The collector and the API both go through ``AKShareQuotes._fetch``, which is
backed by this module. Long-lived worker processes import akshare once and
serve many fetches; the caller never imports akshare itself.

Lifecycle:
- The pool is created lazily on the first fetch in a process.
- Workers are spawned (never forked), so a threaded parent such as the API
  cannot deadlock on fork, and the parent address space never holds akshare.
- Each worker imports akshare on its first task (idle workers stay tiny).
- A crashed/hung pool is discarded and recreated; one retry, then the caller
  gets the same ``DomainError("market_source_unavailable")`` as the old
  subprocess-per-call path raised.
- A reaper shuts the pool down after ``IDLE_REAP_SECONDS`` without use, so the
  rare API refresh does not pin worker memory forever; the collector's
  steady cadence keeps its pool warm during trading hours.
"""

import logging
import threading
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context

from portfolio_backend.errors import DomainError
from portfolio_backend.providers.akshare_worker import fetch_rows

logger = logging.getLogger(__name__)

IDLE_REAP_SECONDS = 120.0
_REAP_PERIOD = 30.0
_RETRIES = 2
# After a pool infrastructure failure, callers fall back to one-shot subprocesses
# for a while instead of paying two doomed pool attempts on every fetch.
_POOL_BROKEN_COOLDOWN = 300.0

# Spawn (not fork) so the workers are safe to create from threaded parents;
# each worker pays its own interpreter startup, then stays resident.
_MP_CONTEXT = get_context("spawn")


class PoolBroken(DomainError):
    """The resident pool itself cannot serve; callers may fall back to subprocesses."""


def _execute(feed, symbol):
    return fetch_rows(feed, symbol)


class PoolFetcher:
    """Long-lived fetch pool with timeout/crash recovery and idle reaping."""

    def __init__(self, workers=1, idle_seconds=IDLE_REAP_SECONDS):
        self.workers = max(1, workers)
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._executor: ProcessPoolExecutor | None = None
        self._last_use = 0.0
        self._reaper: threading.Thread | None = None

    def _create(self):
        return ProcessPoolExecutor(max_workers=self.workers, mp_context=_MP_CONTEXT)

    def _ensure(self):
        with self._lock:
            if self._executor is None:
                self._executor = self._create()
                if self._reaper is None:
                    self._reaper = threading.Thread(
                        target=self._reap_loop, name="fetch-pool-reaper", daemon=True
                    )
                    self._reaper.start()
            return self._executor

    def _discard(self):
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.warning("fetch_pool_shutdown_failed", exc_info=True)

    def _reap_loop(self):
        while True:
            time.sleep(_REAP_PERIOD)
            with self._lock:
                if self._executor is None:
                    self._reaper = None
                    return
                if time.monotonic() - self._last_use <= self.idle_seconds:
                    continue
                executor, self._executor = self._executor, None
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.warning("fetch_pool_reap_failed", exc_info=True)
            return

    def fetch(self, feed, symbol, timeout=90):
        """Fetch rows; recreate the pool once after a crash/timeout, then raise DomainError."""
        for attempt in range(1, _RETRIES + 1):
            executor = self._ensure()
            try:
                future: Future = executor.submit(_execute, feed, symbol)
            except Exception as exc:
                self._discard()
                if attempt >= _RETRIES:
                    raise PoolBroken(
                        "market_source_unavailable", "免费行情源暂不可用", 503
                    ) from exc
                continue
            with self._lock:
                self._last_use = time.monotonic()
            try:
                return future.result(timeout=timeout)
            except FutureTimeout as exc:
                future.cancel()
                self._discard()
                if attempt >= _RETRIES:
                    raise DomainError(
                        "market_source_unavailable", "免费行情源暂不可用", 503
                    ) from exc
            except BrokenProcessPool as exc:
                # Workers died (spawn unsupported, OOM kill, ...): infrastructure, not the source.
                self._discard()
                if attempt >= _RETRIES:
                    raise PoolBroken(
                        "market_source_unavailable", "免费行情源暂不可用", 503
                    ) from exc
            except DomainError:
                raise
            except Exception as exc:
                self._discard()
                if attempt >= _RETRIES:
                    raise DomainError(
                        "market_source_unavailable", "免费行情源暂不可用", 503
                    ) from exc

    def close(self):
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.warning("fetch_pool_close_failed", exc_info=True)


_default_fetcher: PoolFetcher | None = None
_broken_until = 0.0
_default_lock = threading.Lock()


def pool_fetch(feed, symbol, timeout=90):
    """Process-wide default pool; worker count comes from the active settings."""
    global _default_fetcher, _broken_until
    with _default_lock:
        if _broken_until > time.monotonic():
            raise PoolBroken("market_source_unavailable", "免费行情源暂不可用", 503)
        if _default_fetcher is None:
            from portfolio_backend.config import settings

            _default_fetcher = PoolFetcher(workers=settings().data_fetch_workers)
        fetcher = _default_fetcher
    try:
        return fetcher.fetch(feed, symbol, timeout)
    except PoolBroken:
        with _default_lock:
            _broken_until = time.monotonic() + _POOL_BROKEN_COOLDOWN
        raise


def close_default_pool():
    """Release resident workers; safe to call at shutdown and in tests."""
    global _default_fetcher
    with _default_lock:
        fetcher, _default_fetcher = _default_fetcher, None
    if fetcher is not None:
        fetcher.close()
