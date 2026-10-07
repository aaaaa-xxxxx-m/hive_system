from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import hash_password
from app.models.models import Account, AccountRole, DownloadJob, DownloadJobStatus
from app.schemas.schemas import (
    AccountCreate,
    AccountResponse,
    DownloadJobAdminItem,
    DownloadStats,
    PageResponse,
)
from app.api.deps import require_admin

router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])


# ========== 账号管理 ==========
@router.get("/accounts", response_model=List[AccountResponse])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Account).order_by(Account.created_at.desc()))).scalars().all()
    return [AccountResponse.model_validate(a) for a in rows]


@router.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
async def create_account(body: AccountCreate, db: AsyncSession = Depends(get_db)):
    existing = (
        await db.execute(select(Account).where(Account.username == body.username))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"账号 {body.username} 已存在")

    account = Account(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        enabled=True,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return AccountResponse.model_validate(account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    admin: Account = Depends(require_admin),
):
    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    if account.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除当前登录账号")
    if account.role == AccountRole.ADMIN:
        admin_count = (
            await db.execute(select(func.count(Account.id)).where(Account.role == AccountRole.ADMIN))
        ).scalar()
        if (admin_count or 0) <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除最后一个管理员")

    await db.delete(account)
    await db.commit()


# ========== 数据下载日志 ==========
@router.get("/download-jobs", response_model=PageResponse)
async def list_download_jobs(
    username: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(DownloadJob)
    count_query = select(func.count(DownloadJob.id))
    if username:
        query = query.where(DownloadJob.username == username)
        count_query = count_query.where(DownloadJob.username == username)

    total = (await db.execute(count_query)).scalar()
    query = query.order_by(DownloadJob.created_at.desc()).offset((page - 1) * size).limit(size)
    rows = (await db.execute(query)).scalars().all()

    items = [
        DownloadJobAdminItem(
            job_id=j.job_id,
            username=j.username,
            device_name=j.device_name,
            data_limit=j.data_limit,
            status=j.status,
            progress=j.progress,
            object_key=j.object_key,
            error=j.error_message,
            created_at=j.created_at,
            completed_at=j.completed_at,
        ).model_dump()
        for j in rows
    ]
    return PageResponse.create(items=items, total=total, page=page, size=size)


@router.get("/download-stats", response_model=DownloadStats)
async def download_stats(db: AsyncSession = Depends(get_db)):
    total = (await db.execute(select(func.count(DownloadJob.id)))).scalar() or 0
    completed = (
        await db.execute(
            select(func.count(DownloadJob.id)).where(DownloadJob.status == DownloadJobStatus.COMPLETED)
        )
    ).scalar() or 0
    failed = (
        await db.execute(
            select(func.count(DownloadJob.id)).where(DownloadJob.status == DownloadJobStatus.FAILED)
        )
    ).scalar() or 0

    rows = (
        await db.execute(
            select(
                func.coalesce(DownloadJob.username, "未指定").label("username"),
                func.count(DownloadJob.id).label("total"),
                func.sum(case((DownloadJob.status == DownloadJobStatus.COMPLETED, 1), else_=0)).label("completed"),
                func.sum(case((DownloadJob.status == DownloadJobStatus.FAILED, 1), else_=0)).label("failed"),
            ).group_by(func.coalesce(DownloadJob.username, "未指定"))
        )
    ).all()

    by_account = [
        {
            "username": r.username,
            "total": int(r.total or 0),
            "completed": int(r.completed or 0),
            "failed": int(r.failed or 0),
        }
        for r in rows
    ]
    return DownloadStats(
        total_jobs=int(total),
        completed_jobs=int(completed),
        failed_jobs=int(failed),
        by_account=by_account,
    )
