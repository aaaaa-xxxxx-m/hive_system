"""单账号单会话：登录态在 Redis 中的存取与校验。

键：auth:sess:{account_id} -> <jti>
- 登录成功 SET NX EX(空闲超时)；已存在则视为「已在登录状态」
- 每次带令牌请求校验 jti 并滑动续期 EXPIRE
- 退出登录 / 改密 / 删号 时删除键，即时失效旧令牌
"""
from typing import Optional

from app.core.config import get_settings
from app.core.redis_client import get_redis

settings = get_settings()

PREFIX = "auth:sess:"


def session_key(account_id: int) -> str:
    return f"{PREFIX}{account_id}"


def _ttl_seconds() -> int:
    return max(1, settings.SESSION_IDLE_MINUTES) * 60


async def start_session(account_id: int, jti: str) -> bool:
    """占用会话；若已存在返回 False（账号已处于登录状态）。"""
    ok = await get_redis().set(session_key(account_id), jti, nx=True, ex=_ttl_seconds())
    return bool(ok)


async def validate_session(account_id: int, jti: Optional[str]) -> bool:
    """校验会话是否有效；有效则滑动续期。"""
    if not jti:
        return False
    r = get_redis()
    key = session_key(account_id)
    current = await r.get(key)
    if current != jti:
        return False
    await r.expire(key, _ttl_seconds())
    return True


async def clear_session(account_id: int) -> None:
    await get_redis().delete(session_key(account_id))
