import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Account, Device, DownloadJob, DownloadJobStatus
from app.api.deps import get_current_account
from app.schemas.schemas import (
    DownloadJobCreate,
    DownloadJobResponse,
    APIResponse,
)
from app.services.minio_client import get_minio_client, MinIOClient
from app.tasks.download_tasks import export_device_data

router = APIRouter(prefix="/download", tags=["Download"])


@router.post("", response_model=APIResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_download(
    req: DownloadJobCreate,
    db: AsyncSession = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    """创建一个数据打包任务 (异步)。"""
    device = (
        await db.execute(select(Device).where(Device.device_name == req.device_name))
    ).scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    job = DownloadJob(
        job_id=str(uuid.uuid4()),
        device_name=device.device_name,
        username=account.username,
        account_id=account.id,
        data_limit=req.limit,
        status=DownloadJobStatus.PENDING,
        progress=0,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    export_device_data.delay(job.job_id, device.device_name, req.limit)

    return APIResponse(data={"job_id": job.job_id}, message="Download job accepted")


@router.get("/{job_id}", response_model=DownloadJobResponse)
async def get_download_job(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    minio: MinIOClient = Depends(get_minio_client),
    _account: Account = Depends(get_current_account),
):
    job = (
        await db.execute(select(DownloadJob).where(DownloadJob.job_id == job_id))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Download job not found")

    download_url = None
    if job.status == DownloadJobStatus.COMPLETED and job.object_key:
        download_url = minio.get_presigned_url(job.object_key, expires_seconds=3600)

    return DownloadJobResponse(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        download_url=download_url,
        error=job.error_message,
    )
