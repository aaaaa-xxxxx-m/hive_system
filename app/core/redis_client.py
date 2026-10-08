"""异步 Redis 客户端单例（延迟初始化，避免导入期绑定事件循环）。"""
from typing import Optional

from redis import asyncio as aioredis

from app.core.config import get_settings

settings = get_settings()

_client: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
