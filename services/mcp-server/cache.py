"""
services/mcp-server/cache.py   (NEW FILE)

Generic Redis-backed cache-aside abstraction for the DIP data-access
boundary. Intentionally has zero knowledge of "roles", "parties", or
any specific tool -- it is a plain key/value store with TTLs. DipClient
depends on THIS interface, not on redis directly, so the data layer
stays decoupled and mockable in tests.

Fail-gracefully by design: any backend error on get/set is logged and
swallowed, never propagated -- a cache outage must degrade to "always
call the live API", not crash the request.
"""

import json
import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self, redis_client, default_ttl_seconds: int = 1800):
        self._redis = redis_client
        self.default_ttl_seconds = default_ttl_seconds

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(key)
        except Exception:
            logger.exception("cache backend unavailable on GET %r; treating as miss", key)
            return None
        if raw is None:
            logger.info("cache MISS: %s", key)
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            logger.warning("corrupt cache entry at %r; ignoring", key)
            return None
        logger.info("cache HIT: %s", key)
        return value



    
    # --- REPLACE the set() method with: ---
    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        base_ttl = ttl_seconds or self.default_ttl_seconds
        jittered_ttl = int(base_ttl * random.uniform(0.9, 1.1))  # ±10%
        try:
            await self._redis.set(key, json.dumps(value), ex=jittered_ttl)
        except Exception:
            logger.exception("cache backend unavailable on SET %r; continuing without caching", key)
