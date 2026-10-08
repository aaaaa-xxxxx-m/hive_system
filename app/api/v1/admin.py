from typing import List, Optional
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import hash_password
from app.models.models import (
    Account,
    AccountRole,
    DataExportJob,
    Device,
    DownloadJob,
    DownloadJobStatus,
    ModelVersion,
    TrainingJob,
    TrainingJobStatus,
)
from app.schemas.schemas import (
    DATA_CATEGORIES,
    AccountAdminItem,
    AccountCreate,
    AccountPasswordUpdate,
    AccountResponse,
    AccountRoleUpdate,
    AdminDeviceItem,
    DataExportCreate,
    DataExportJobResponse,
    DeviceDataUsage,
    DeviceOrphanItem,
    DownloadJobAdminItem,
    DownloadStats,
    PageResponse,
    APIResponse,
)
from app.api.deps import require_admin
from app.core.redis_client import get_redis
from app.core.session import clear_session, session_key
from app.services.minio_client import get_minio_client, MinIOClient
from app.services import device_data
from app.tasks.export_tasks import export_device_data_task

REQUIRED_POSITIVE_SAMPLES = 600

router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin)])


# ========== 账号管理 ==========
@router.get("/accounts", response_model=List[AccountAdminItem])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Account).order_by(Account.created_at.desc()))).scalars().all()
    redis = get_redis()
    items: List[AccountAdminItem] = []
    for a in rows:
        item = AccountAdminItem.model_validate(a)
        item.online = bool(await redis.exists(session_key(a.id)))
        items.append(item)
    return items


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


@router.put("/accounts/{account_id}/role", response_model=AccountResponse)
async def update_account_role(
    account_id: int,
    body: AccountRoleUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Account = Depends(require_admin),
):
    """调整账号等级（普通用户 / 管理员）。"""
    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    if account.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能修改自己的角色")

    new_role = AccountRole(body.role.value)
    if account.role == AccountRole.ADMIN and new_role != AccountRole.ADMIN:
        admin_count = (
            await db.execute(select(func.count(Account.id)).where(Account.role == AccountRole.ADMIN))
        ).scalar()
        if (admin_count or 0) <= 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能移除最后一个管理员")

    account.role = new_role
    await db.commit()
    await db.refresh(account)
    return AccountResponse.model_validate(account)


@router.post("/accounts/{account_id}/force-logout", response_model=APIResponse)
async def force_logout_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
):
    """强制某账号下线（清其服务端会话，已发出的令牌立即失效）。"""
    account = (
        await db.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")

    was_online = bool(await get_redis().exists(session_key(account.id)))
    await clear_session(account.id)
    return APIResponse(
        data={"was_online": was_online},
        message="已强制下线" if was_online else "该账号当前不在线",
    )


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
    device_name: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(DownloadJob)
    count_query = select(func.count(DownloadJob.id))
    if username:
        # 模糊筛选：账号名包含该内容即匹配（转义 % / _ 以免被当通配符）
        cond = DownloadJob.username.contains(username, autoescape=True)
        query = query.where(cond)
        count_query = count_query.where(cond)
    if device_name:
        cond = DownloadJob.device_name.contains(device_name, autoescape=True)
        query = query.where(cond)
        count_query = count_query.where(cond)

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

    device_rows = (
        await db.execute(
            select(
                DownloadJob.device_name.label("device_name"),
                func.count(DownloadJob.id).label("total"),
                func.sum(case((DownloadJob.status == DownloadJobStatus.COMPLETED, 1), else_=0)).label("completed"),
                func.sum(case((DownloadJob.status == DownloadJobStatus.FAILED, 1), else_=0)).label("failed"),
            ).group_by(DownloadJob.device_name)
        )
    ).all()

    by_device = [
        {
            "device_name": r.device_name,
            "total": int(r.total or 0),
            "completed": int(r.completed or 0),
            "failed": int(r.failed or 0),
        }
        for r in device_rows
    ]
    return DownloadStats(
        total_jobs=int(total),
        completed_jobs=int(completed),
        failed_jobs=int(failed),
        by_account=by_account,
        by_device=by_device,
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


# ========== 设备管理 / 设备数据管理 ==========
def _tjob_status(job: Optional[TrainingJob]) -> str:
    if job is None:
        return "idle"
    return getattr(job.status, "value", job.status)


@router.get("/devices", response_model=List[AdminDeviceItem])
async def list_devices_admin(db: AsyncSession = Depends(get_db)):
    """设备列表：模型更新状态 + 各分类数据占用。"""
    devices = (await db.execute(select(Device).order_by(Device.device_name))).scalars().all()
    items: List[AdminDeviceItem] = []
    for d in devices:
        job = (
            await db.execute(
                select(TrainingJob)
                .where(TrainingJob.device_name == d.device_name)
                .order_by(TrainingJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        version = None
        if job and job.model_version_id:
            version = (
                await db.execute(select(ModelVersion.version).where(ModelVersion.id == job.model_version_id))
            ).scalar_one_or_none()
        u = await device_data.usage(db, d.device_name)
        active = bool(
            job
            and job.status
            in (TrainingJobStatus.WAITING, TrainingJobStatus.PENDING, TrainingJobStatus.RUNNING)
        )
        items.append(
            AdminDeviceItem(
                device_name=d.device_name,
                status=d.status.value if d.status else None,
                last_seen_at=d.last_seen_at,
                model_update_status=_tjob_status(job),
                progress=(job.progress or 0) if job else 0,
                feature_count=(job.feature_count or 0) if job else 0,
                required=REQUIRED_POSITIVE_SAMPLES,
                model_version=version,
                requested_at=job.requested_at if job else None,
                has_active=active,
                usage=DeviceDataUsage(**u),
            )
        )
    return items


@router.get("/device-data/orphans", response_model=List[DeviceOrphanItem])
async def list_device_orphans(db: AsyncSession = Depends(get_db)):
    """无对应设备的遗留数据（软提示）。"""
    rows = await device_data.detect_orphans(db)
    return [DeviceOrphanItem(device_name=n, usage=DeviceDataUsage(**u)) for n, u in rows]


@router.post("/devices/{device_name}/data/export", response_model=APIResponse)
async def export_device_data_admin(
    device_name: str,
    body: DataExportCreate,
    db: AsyncSession = Depends(get_db),
    admin: Account = Depends(require_admin),
):
    """按分类异步打包该设备数据 → zip（完成后经 /admin/data-export/{job_id} 取链接）。"""
    cats = [c for c in (body.categories or list(DATA_CATEGORIES)) if c in DATA_CATEGORIES]
    if not cats:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请至少选择一个分类")

    job = DataExportJob(
        job_id=str(uuid.uuid4()),
        device_name=device_name,
        categories=cats,
        username=admin.username,
        status=DownloadJobStatus.PENDING,
        progress=0,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    export_device_data_task.delay(job.job_id, device_name, cats)
    return APIResponse(data={"job_id": job.job_id}, message="Export job accepted")


@router.get("/data-export/{job_id}", response_model=DataExportJobResponse)
async def get_data_export_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    minio: MinIOClient = Depends(get_minio_client),
):
    job = (
        await db.execute(select(DataExportJob).where(DataExportJob.job_id == job_id))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="导出任务不存在")

    url = None
    if job.status == DownloadJobStatus.COMPLETED and job.object_key:
        url = minio.get_presigned_url(job.object_key, expires_seconds=3600)
    return DataExportJobResponse(
        job_id=job.job_id,
        status=getattr(job.status, "value", job.status),
        progress=job.progress or 0,
        download_url=url,
        error=job.error_message,
    )


@router.delete("/devices/{device_name}/data", response_model=APIResponse)
async def clear_device_data_admin(
    device_name: str,
    categories: str = Query("", description="逗号分隔的分类；留空=全部"),
    db: AsyncSession = Depends(get_db),
):
    """清除该设备在 MinIO + DB 中选定分类的数据。"""
    picked = [c.strip() for c in categories.split(",") if c.strip()]
    cats = [c for c in (picked or list(DATA_CATEGORIES)) if c in DATA_CATEGORIES]
    if not cats:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="分类无效")
    deleted = await device_data.clear_device_data(db, device_name, cats)
    return APIResponse(data={"deleted": deleted}, message="已清除")


@router.post("/devices/{device_name}/model-update/cancel", response_model=APIResponse)
async def cancel_model_update(
    device_name: str,
    db: AsyncSession = Depends(get_db),
):
    """取消该设备进行中的模型更新任务。"""
    res = await db.execute(
        update(TrainingJob)
        .where(
            TrainingJob.device_name == device_name,
            TrainingJob.status.in_(
                [TrainingJobStatus.WAITING, TrainingJobStatus.PENDING, TrainingJobStatus.RUNNING]
            ),
        )
        .values(
            status=TrainingJobStatus.CANCELLED,
            error_message="管理员取消",
            completed_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return APIResponse(data={"cancelled": res.rowcount or 0})
