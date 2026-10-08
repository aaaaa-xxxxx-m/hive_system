from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime
import hashlib
import io

from app.core.database import get_db
from app.models.models import ModelVersion, TrainingJob, ModelStatus, TrainingJobStatus, ModelType
from app.schemas.schemas import (
    ModelVersionCreate, ModelVersionUpdate, ModelVersionResponse,
    TrainingJobCreate, TrainingJobResponse,
    ModelDeployRequest, ModelSignRequest,
    PageParams, PageResponse, APIResponse,
    TrainingConfig,
)
from app.services.minio_client import get_minio_client, MinIOClient
from app.services.model_service import get_model_manager, ModelManager
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/models", tags=["Models"])


# ========== 模型版本列表 ==========
@router.get("", response_model=PageResponse)
async def list_model_versions(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: Optional[ModelStatus] = None,
    is_active: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(ModelVersion)
    count_query = select(func.count(ModelVersion.id))

    if status:
        query = query.where(ModelVersion.status == status)
        count_query = count_query.where(ModelVersion.status == status)
    if is_active is not None:
        query = query.where(ModelVersion.is_active == is_active)
        count_query = count_query.where(ModelVersion.is_active == is_active)

    total = (await db.execute(count_query)).scalar()
    query = query.order_by(ModelVersion.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    items = result.scalars().all()

    return PageResponse.create(
        items=[ModelVersionResponse.model_validate(i).model_dump() for i in items],
        total=total,
        page=page,
        size=size,
    )


# ========== 当前生产模型 ==========
@router.get("/current", response_model=ModelVersionResponse)
async def get_current_model(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ModelVersion)
        .where(ModelVersion.is_active == True, ModelVersion.status == ModelStatus.ACTIVE)
        .order_by(ModelVersion.deployed_at.desc())
    )
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="No active production model")
    return ModelVersionResponse.model_validate(model)


# ========== 获取模型详情 ==========
@router.get("/{version}", response_model=ModelVersionResponse)
async def get_model_version(version: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelVersion).where(ModelVersion.version == version))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model version not found")
    return ModelVersionResponse.model_validate(model)


# ========== 手动注册模型版本 (已有文件在 MinIO) ==========
@router.post("", response_model=ModelVersionResponse, status_code=status.HTTP_201_CREATED)
async def register_model_version(
    model_in: ModelVersionCreate,
    db: AsyncSession = Depends(get_db),
    minio: MinIOClient = Depends(get_minio_client),
):
    # 验证文件存在
    if not minio.file_exists(model_in.minio_object_key):
        raise HTTPException(status_code=400, detail=f"Model file not found in MinIO: {model_in.minio_object_key}")
    if model_in.scaler_object_key and not minio.file_exists(model_in.scaler_object_key):
        raise HTTPException(status_code=400, detail=f"Scaler file not found in MinIO: {model_in.scaler_object_key}")

    # 检查版本号唯一
    existing = await db.execute(select(ModelVersion).where(ModelVersion.version == model_in.version))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Version {model_in.version} already exists")

    # 检查 SHA256 唯一
    existing = await db.execute(select(ModelVersion).where(ModelVersion.sha256_hash == model_in.sha256_hash))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Model with same SHA256 already exists")

    # 这里需要签名，实际应由训练任务或签名服务生成
    # 暂时允许手动指定签名 (生产环境应强制签名)
    signature = "manual_registration_placeholder"

    model = ModelVersion(
        version=model_in.version,
        model_type=model_in.model_type,
        minio_object_key=model_in.minio_object_key,
        scaler_object_key=model_in.scaler_object_key,
        signature=signature,
        file_size=model_in.file_size,
        sha256_hash=model_in.sha256_hash,
        training_dataset_size=model_in.training_dataset_size,
        training_accuracy=model_in.training_accuracy,
        validation_accuracy=model_in.validation_accuracy,
        metrics=model_in.metrics.model_dump() if model_in.metrics else None,
        status=ModelStatus.VALIDATING,
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return ModelVersionResponse.model_validate(model)


# ========== 触发模型训练 ==========
# 已弃用：改为「按设备触发」——见 devices.py 的 POST /devices/{device_name}/model-update
# （旧的 keys 方式训练入口已移除）


# ========== 训练任务列表 ==========
@router.get("/training/jobs", response_model=PageResponse)
async def list_training_jobs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: Optional[TrainingJobStatus] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(TrainingJob).options(selectinload(TrainingJob.model_version))
    count_query = select(func.count(TrainingJob.id))

    if status:
        query = query.where(TrainingJob.status == status)
        count_query = count_query.where(TrainingJob.status == status)

    total = (await db.execute(count_query)).scalar()
    query = query.order_by(TrainingJob.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    items = result.scalars().all()

    return PageResponse.create(
        items=[TrainingJobResponse.model_validate(i).model_dump() for i in items],
        total=total,
        page=page,
        size=size,
    )


# ========== 训练任务详情 ==========
@router.get("/training/jobs/{job_id}", response_model=TrainingJobResponse)
async def get_training_job(job_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TrainingJob).options(selectinload(TrainingJob.model_version)).where(TrainingJob.job_id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Training job not found")
    return TrainingJobResponse.model_validate(job)


# ========== 部署模型 ==========
@router.post("/{version}/deploy", response_model=APIResponse)
async def deploy_model(
    version: str,
    db: AsyncSession = Depends(get_db),
    model_manager: ModelManager = Depends(get_model_manager),
):
    result = await db.execute(select(ModelVersion).where(ModelVersion.version == version))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model version not found")

    if model.status == ModelStatus.FAILED:
        raise HTTPException(status_code=400, detail="Cannot deploy failed model")

    # 验证模型完整性
    try:
        model_manager.download_model(version)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Model verification failed: {e}")

    # 取消当前生产模型
    await db.execute(
        update(ModelVersion)
        .where(ModelVersion.is_active == True)
        .values(is_active=False, status=ModelStatus.DEPRECATED)
    )

    # 设置新模型
    await db.execute(
        update(ModelVersion)
        .where(ModelVersion.version == version)
        .values(is_active=True, status=ModelStatus.ACTIVE, deployed_at=datetime.utcnow())
    )
    await db.commit()

    return APIResponse(message=f"Model {version} deployed successfully")


# ========== 撤销部署 ==========
@router.post("/{version}/undeploy", response_model=APIResponse)
async def undeploy_model(version: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelVersion).where(ModelVersion.version == version))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model version not found")

    if not model.is_active:
        raise HTTPException(status_code=400, detail="Model is not currently deployed")

    await db.execute(
        update(ModelVersion)
        .where(ModelVersion.version == version)
        .values(is_active=False, status=ModelStatus.DEPRECATED)
    )
    await db.commit()

    return APIResponse(message=f"Model {version} undeployed")


# ========== 获取模型公钥 (设备端验证用) ==========
@router.get("/public-key")
async def get_public_key(model_manager: ModelManager = Depends(get_model_manager)):
    return {"public_key_pem": model_manager.get_public_key_pem()}


# ========== 签名模型 (仅内部/测试用) ==========
@router.post("/sign", response_model=APIResponse)
async def sign_model(
    request: ModelSignRequest,
    model_manager: ModelManager = Depends(get_model_manager),
):
    import base64
    model_data = base64.b64decode(request.model_data_b64)
    scaler_data = base64.b64decode(request.scaler_data_b64) if request.scaler_data_b64 else None

    metadata = {"model_type": "svm", "version": request.version}
    model_bytes, scaler_bytes, model_sha256, signature = model_manager.package_model(
        model_data, scaler_data, metadata
    )

    # 上传到 MinIO
    model_key, scaler_key = model_manager.upload_model(request.version, model_bytes, scaler_bytes, signature)

    return APIResponse(data={
        "model_key": model_key,
        "scaler_key": scaler_key,
        "signature": signature,
        "sha256": model_sha256,
    })


# ========== 获取模型下载 URL (预签名) ==========
@router.get("/{version}/download")
async def get_model_download_url(
    version: str,
    expires: int = Query(3600, ge=60, le=86400),
    db: AsyncSession = Depends(get_db),
    minio: MinIOClient = Depends(get_minio_client),
):
    result = await db.execute(select(ModelVersion).where(ModelVersion.version == version))
    model = result.scalar_one_or_none()
    if not model:
        raise HTTPException(status_code=404, detail="Model version not found")

    model_url = minio.get_presigned_url(model.minio_object_key, expires_seconds=expires)
    scaler_url = None
    if model.scaler_object_key:
        scaler_url = minio.get_presigned_url(model.scaler_object_key, expires_seconds=expires)

    return APIResponse(data={
        "model_url": model_url,
        "scaler_url": scaler_url,
        "version": version,
        "sha256": model.sha256_hash,
        "signature": model.signature,
        "expires_in": expires,
    })