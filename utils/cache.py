"""Cross-process cache with lazy initialization.

On Python 3.14 the default multiprocessing start method on Linux flipped
from ``fork`` to ``forkserver``. Creating a ``multiprocessing.Manager()``
at *import time* (as the previous version did) fails with EOFError because
the importing process has not finished its bootstrap phase yet.

We defer Manager creation until the first cache operation, and — more
importantly — fall back to an in-process ``dict`` when we're inside a
multiprocessing worker (the ``strk_bot_parsing`` / ``strk_bot_notification``
workers spawned by ``main.py``). Each worker does its own Starknet
fetches, so intra-process TTL is fine; cross-process cache coherence was
never enforced strictly anyway.
"""
from __future__ import annotations

import multiprocessing
from datetime import datetime, timedelta
from typing import Any, Optional

from data.all_paths import FILES_DIR

CACHE_DIR = FILES_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


_manager: Any = None
_cache: dict[str, tuple[Any, datetime]] | Any = None


def _ensure_backing_store() -> None:
    """Lazily create the backing store.

    - In the main process (``MainProcess``) we start a Manager so spawned
      workers see the same cache (as before the refactor).
    - In an already-spawned worker we use a plain dict; the fork/forkserver
      parent proxy is unavailable here and re-creating a Manager inside a
      worker would just be per-worker local state anyway.
    """
    global _manager, _cache
    if _cache is not None:
        return
    proc = multiprocessing.current_process()
    if proc.name == "MainProcess":
        try:
            _manager = multiprocessing.Manager()
            _cache = _manager.dict()
            return
        except Exception:
            # Any Manager failure (e.g. docker seccomp, readonly rootfs)
            # degrades to an in-process dict — better than crashing import.
            _manager = None
    _cache = {}


class SharedCache:
    def __init__(self, ttl: int = 300) -> None:
        self.ttl = ttl

    async def get(self, key: str) -> Optional[Any]:
        _ensure_backing_store()
        if key not in _cache:
            return None
        value, expiry = _cache[key]
        if datetime.now() > expiry:
            await self.delete(key)
            return None
        return value

    async def set(self, key: str, value: Any) -> None:
        _ensure_backing_store()
        expiry = datetime.now() + timedelta(seconds=self.ttl)
        _cache[key] = (value, expiry)

    async def delete(self, key: str) -> None:
        _ensure_backing_store()
        if key in _cache:
            del _cache[key]

    async def keys(self, pattern: str) -> list[str]:
        _ensure_backing_store()
        return [key for key in _cache.keys() if pattern in key]


cache = SharedCache(ttl=300)


def get_cache_key(user_id: int, command: str) -> str:
    return f"{user_id}_{command}"


async def clear_user_cache(user_id: int) -> None:
    all_keys = await cache.keys(f"{user_id}_")
    for key in all_keys:
        await cache.delete(key)
