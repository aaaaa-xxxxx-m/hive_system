"""单账号单会话：登录态在 Redis 中的存取与校验。

键：auth:sess:{account_id} -> "<jti>:<last_seen_epoch>"
- 登录成功 SET NX EX(空闲超时)；已存在则视为「已在登录状态」
- 每次带令牌请求校验 jti 并滑动续期，同时刷新 last_seen（活动时间）
- 退出登录 / 改密 / 删号 时删除键，即时失效旧令牌
- 登录时若已存在会话但「超过宽限期无活动」，视为陈旧会话并顶替，
  避免客户端已退出（本地会话丢失）却因服务端会话未清而长期挡住重登
"""
import time
from typing import Optional, Tuple

from app.core.config import get_settings
from app.core.redis_client import get_redis

settings = get_settings()

PREFIX = "auth:sess:"


def session_key(account_id: int) -> str:
    return f"{PREFIX}{account_id}"


def _ttl_seconds() -> int:
    return max(1, settings.SESSION_IDLE_MINUTES) * 60


def _takeover_grace() -> int:
    return max(0, settings.SESSION_TAKEOVER_GRACE_SECONDS)


def _encode(jti: str, ts: float) -> str:
    return f"{jti}:{int(ts)}"


def _decode(value: Optional[str]) -> Tuple[Optional[str], float]:
    if not value:
        return None, 0.0
    if ":" in value:
        jti, _, ts = value.partition(":")
        try:
            return jti, float(ts)
        except ValueError:
            return jti, 0.0
    # 兼容旧格式（仅 jti 值，无活动时间）
    return value, 0.0


async def start_session(account_id: int, jti: str) -> bool:
    """占用会话；若已存在且仍在活动期内返回 False（账号已处于登录状态）。"""
    r = get_redis()
    key = session_key(account_id)
    now = time.time()
    if await r.set(key, _encode(jti, now), nx=True, ex=_ttl_seconds()):
        return True
    # 已存在会话：若超过宽限期无活动，说明对方已退出/失联 → 顶替
    cur = await r.get(key)
    _, ts = _decode(cur)
    if now - ts > _takeover_grace():
        await r.set(key, _encode(jti, now), ex=_ttl_seconds())
        return True
    return False


async def validate_session(account_id: int, jti: Optional[str]) -> bool:
    """校验会话是否有效；有效则滑动续期并刷新活动时间。"""
    if not jti:
        return False
    r = get_redis()
    key = session_key(account_id)
    cur = await r.get(key)
    cur_jti, _ = _decode(cur)
    if cur_jti != jti:
        return False
    await r.set(key, _encode(jti, time.time()), ex=_ttl_seconds())
    return True


async def clear_session(account_id: int) -> None:
    await get_redis().delete(session_key(account_id))
