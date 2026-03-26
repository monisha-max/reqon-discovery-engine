import json
from typing import Any, Optional

import redis.asyncio as redis

from config.settings import settings


class RedisStateManager:
    """Shared state manager using Redis. Used by all agents to read/write state."""

    def __init__(self, namespace: str = "reqon"):
        self.namespace = namespace
        self._redis: Optional[redis.Redis] = None

    async def connect(self):
        self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        await self._redis.ping()

    async def disconnect(self):
        if self._redis:
            await self._redis.aclose()

    async def set(self, key: str, value: Any, ttl: int = 3600):
        full_key = f"{self.namespace}:{key}"
        await self._redis.set(full_key, json.dumps(value, default=str), ex=ttl)

    async def get(self, key: str) -> Optional[Any]:
        full_key = f"{self.namespace}:{key}"
        data = await self._redis.get(full_key)
        return json.loads(data) if data else None

    async def push_to_stream(self, stream: str, data: dict):
        full_stream = f"{self.namespace}:stream:{stream}"
        await self._redis.xadd(full_stream, {k: json.dumps(v, default=str) for k, v in data.items()})

    async def read_stream(self, stream: str, last_id: str = "0-0", count: int = 10) -> list:
        full_stream = f"{self.namespace}:stream:{stream}"
        messages = await self._redis.xread({full_stream: last_id}, count=count)
        results = []
        for stream_name, entries in messages:
            for entry_id, fields in entries:
                parsed = {k: json.loads(v) for k, v in fields.items()}
                results.append({"id": entry_id, **parsed})
        return results

    async def add_to_set(self, key: str, *values: str):
        full_key = f"{self.namespace}:{key}"
        await self._redis.sadd(full_key, *values)

    async def is_in_set(self, key: str, value: str) -> bool:
        full_key = f"{self.namespace}:{key}"
        return await self._redis.sismember(full_key, value)

    async def set_size(self, key: str) -> int:
        full_key = f"{self.namespace}:{key}"
        return await self._redis.scard(full_key)
