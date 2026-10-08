from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import hash_password
from app.models.models import Account, AccountRole, DownloadJob, DownloadJobStatus
from app.schemas.schemas import (
    AccountCreate,
    AccountPasswordUpdate,
    AccountResponse,
    DownloadJobAdminItem,
    DownloadStats,
    PageResponse,
)
from app.api.deps import require_admin
from app.core.session import clear_session
from app.services.minio_client import get_minio_client, MinIOClient

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


@router.put("/accounts/{account_id}/password", response_model=AccountResponse)
async def update_account_password(
    account_id: int,
    body: AccountPasswordUpdate,
    db: AsyncSession = Depends(get_db),
):
    """重置指定账号的密码（管理员操作，可作用于任意账号）。"""
    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")

    account.password_hash = hash_password(body.password)
    await db.commit()
    await db.refresh(account)
    # 改密后强制该账号下线
    await clear_session(account.id)
    return AccountResponse.model_validate(account)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    records: str = Query(
        "keep",
        pattern="^(delete|keep)$",
        description="下载记录处理: delete=一并删除记录与文件, keep=保留并解除绑定(标注已注销)",
    ),
    db: AsyncSession = Depends(get_db),
    minio: MinIOClient = Depends(get_minio_client),
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

    # 该账号名下的下载记录（严格按 account_id 绑定；不再按用户名兜底，
    # 避免同名账号被重建后误删旧账号遗留的「已注销」记录）
    jobs = (
        await db.execute(
            select(DownloadJob).where(DownloadJob.account_id == account.id)
        )
    ).scalars().all()

    if records == "delete":
        for job in jobs:
            if job.object_key:
                try:
                    minio.delete_file(job.object_key)
                except Exception:
                    pass  # 文件缺失不影响删除
            await db.delete(job)
    else:
        # 保留记录：解除绑定（account_id 置空），username 快照保留用于展示「已注销」
        for job in jobs:
            job.account_id = None

    await db.delete(account)
    await db.commit()
    # 删号后清除其登录会话
    await clear_session(account_id)


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
            account_id=j.account_id,
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


@router.delete("/download-jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_download_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    minio: MinIOClient = Depends(get_minio_client),
):
    """删除一条下载日志（同时删除其已生成的导出文件）。"""
    job = (
        await db.execute(select(DownloadJob).where(DownloadJob.job_id == job_id))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    if job.status == DownloadJobStatus.RUNNING:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="任务进行中，暂不能删除")

    if job.object_key:
        try:
            minio.delete_file(job.object_key)
        except Exception:
            pass  # 文件缺失不影响记录删除

    await db.delete(job)
    await db.commit()
