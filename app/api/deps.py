from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.core.session import validate_session
from app.models.models import Account, AccountRole


async def get_current_account(
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
) -> Account:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_token(token)
    subject = payload.get("sub") if payload else None
    if not subject or payload.get("role") == "guest":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    account = (
        await db.execute(select(Account).where(Account.username == subject))
    ).scalar_one_or_none()
    if not account or not account.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
    if not await validate_session(account.id, payload.get("jti")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已失效（会话超时或账号已在其他位置登录）",
        )
    return account


async def require_admin(account: Account = Depends(get_current_account)) -> Account:
    if account.role != AccountRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return account


async def require_viewer(
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
):
    """只读访问：接受任意有效令牌（含游客）。返回 dict 或 Account。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    payload = decode_token(authorization.split(" ", 1)[1].strip())
    subject = payload.get("sub") if payload else None
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")
    if payload.get("role") == "guest":
        return {"username": subject, "role": "guest"}
    account = (
        await db.execute(select(Account).where(Account.username == subject))
    ).scalar_one_or_none()
    if not account or not account.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
    if not await validate_session(account.id, payload.get("jti")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录状态已失效（会话超时或账号已在其他位置登录）",
        )
    return account
