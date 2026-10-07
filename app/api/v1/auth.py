from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import create_access_token, verify_password
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

    account.last_login_at = datetime.utcnow()
    await db.commit()

    token = create_access_token(account.username, account.role.value)
    return TokenResponse(token=token, username=account.username, role=account.role)


@router.get("/me", response_model=AccountResponse)
async def me(account: Account = Depends(get_current_account)):
    return AccountResponse.model_validate(account)


@router.post("/guest", response_model=TokenResponse)
async def guest_login():
    """游客令牌：用于只读访问（无账号，仅查看）。"""
    token = create_access_token("guest", "guest")
    return TokenResponse(token=token, username="游客", role="guest")
