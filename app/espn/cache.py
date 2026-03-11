"""
cache.py - Local JSON file cache with TTL.

Stores ESPN API responses in ./cache/ as JSON files.
Checks file mtime against CACHE_TTL_SECONDS before returning cached data.
"""

import json
import time
import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CacheManager:
    def __init__(self, cache_dir: Path, ttl_seconds: int = 600):
        self.cache_dir = cache_dir
        self.ttl = ttl_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe_key = key.replace("/", "_").replace("\\", "_")
        return self.cache_dir / f"{safe_key}.json"

    def is_fresh(self, key: str) -> bool:
        p = self._path(key)
        if not p.exists():
            return False
        age = time.time() - p.stat().st_mtime
        return age < self.ttl

    def get(self, key: str) -> Optional[dict]:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def set(self, key: str, data: Any) -> None:
        p = self._path(key)
        p.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        logger.debug("Cache SET: %s", key)

    def get_or_fetch(self, key: str, fetch_fn: Callable[[], Any]) -> Any:
        """Return cached data if fresh, else call fetch_fn, cache, and return."""
        if self.is_fresh(key):
            cached = self.get(key)
            if cached is not None:
                logger.debug("Cache HIT: %s", key)
                return cached
        logger.debug("Cache MISS: %s — fetching fresh data", key)
        data = fetch_fn()
        self.set(key, data)
        return data

    def invalidate(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()
            logger.debug("Cache INVALIDATED: %s", key)

    def invalidate_all(self) -> int:
        """Remove all cached files. Returns count deleted."""
        count = 0
        for f in self.cache_dir.glob("*.json"):
            f.unlink()
            count += 1
        logger.info("Cache cleared: %d files removed", count)
        return count

    def cache_age_str(self, key: str) -> str:
        """Human-readable age of a cache entry."""
        p = self._path(key)
        if not p.exists():
            return "no cache"
        age = time.time() - p.stat().st_mtime
        if age < 60:
            return f"{int(age)}s ago"
        if age < 3600:
            return f"{int(age // 60)}m ago"
        return f"{age / 3600:.1f}h ago"
