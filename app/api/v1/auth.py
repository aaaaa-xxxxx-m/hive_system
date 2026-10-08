from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import create_access_token, verify_password
from app.core.session import clear_session, start_session
from app.models.models import Account
from app.schemas.schemas import AccountResponse, LoginRequest, TokenResponse
from app.api.deps import get_current_account

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    account = (
        await db.execute(select(Account).where(Account.username == req.username))
    ).scalar_one_or_none()
    if not account or not account.enabled or not verify_password(req.password, account.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误")

    # 单账号单会话：已有有效会话则拒绝再次登录
    jti = uuid.uuid4().hex
    if not await start_session(account.id, jti):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前账号已处于登录状态，请先在原设备退出登录后再试",
        )

    account.last_login_at = datetime.utcnow()
    await db.commit()

    token = create_access_token(account.username, account.role.value, jti=jti)
    return TokenResponse(token=token, username=account.username, role=account.role)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(account: Account = Depends(get_current_account)):
    """退出登录：释放服务端会话。"""
    await clear_session(account.id)


@router.get("/me", response_model=AccountResponse)
async def me(account: Account = Depends(get_current_account)):
    return AccountResponse.model_validate(account)


@router.post("/guest", response_model=TokenResponse)
async def guest_login():
    """游客令牌：用于只读访问（无账号，仅查看）。"""
    token = create_access_token("guest", "guest")
    return TokenResponse(token=token, username="游客", role="guest")
