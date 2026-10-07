from fastapi import APIRouter, Depends, HTTPException, status, Query, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from typing import List, Optional
from datetime import datetime
import hashlib
import secrets

from app.core.database import get_db
from app.models.models import Device, InferenceResult, DeviceStatus, InferenceLabel
from app.schemas.schemas import (
    DeviceCreate, DeviceUpdate, DeviceResponse, DeviceWithDetails, DeviceSummary,
    DeviceDownloadCatalog,
    InferenceResultCreate, InferenceResultResponse, DeviceRecordResponse,
    PageResponse, APIResponse,
)
from app.services.minio_client import get_minio_client, MinIOClient

router = APIRouter(prefix="/devices", tags=["Devices"])


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    return f"bee_{secrets.token_urlsafe(32)}"


async def get_device_by_api_key(db: AsyncSession, api_key: str) -> Optional[Device]:
    key_hash = hash_api_key(api_key)
    result = await db.execute(select(Device).where(Device.api_key_hash == key_hash))
    return result.scalar_one_or_none()


async def verify_device_auth(
    db: AsyncSession = Depends(get_db),
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> Device:
    device = await get_device_by_api_key(db, x_api_key)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    if device.status == DeviceStatus.DECOMMISSIONED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Device decommissioned",
        )
    return device


# ========== 设备注册 (管理端) ==========
@router.post("", response_model=APIResponse, status_code=status.HTTP_201_CREATED)
async def register_device(device_in: DeviceCreate, db: AsyncSession = Depends(get_db)):
    """注册新设备 (管理员接口)"""
    existing = await db.execute(select(Device).where(Device.device_name == device_in.device_name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device {device_in.device_name} already exists",
        )

    api_key = device_in.api_key if hasattr(device_in, 'api_key') and device_in.api_key else generate_api_key()
    api_key_hash = hash_api_key(api_key)

    device = Device(
        device_name=device_in.device_name,
        api_key_hash=api_key_hash,
        device_fingerprint=device_in.device_fingerprint,
        name=device_in.name,
        location_lat=device_in.location_lat,
        location_lng=device_in.location_lng,
        location_desc=device_in.location_desc,
        status=DeviceStatus.OFFLINE,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)

    response_data = DeviceResponse.model_validate(device).model_dump()
    response_data["api_key"] = api_key

    return APIResponse(data=response_data, message="Device registered successfully")


# ========== 设备列表 ==========
@router.get("", response_model=PageResponse)
async def list_devices(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status: Optional[DeviceStatus] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Device)
    count_query = select(func.count(Device.id))

    if status:
        query = query.where(Device.status == status)
        count_query = count_query.where(Device.status == status)

    total = (await db.execute(count_query)).scalar()
    query = query.order_by(Device.registered_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    devices = result.scalars().all()

    return PageResponse.create(
        items=[DeviceResponse.model_validate(d).model_dump() for d in devices],
        total=total,
        page=page,
        size=size,
    )


# ========== 首页聚合列表 (每设备最新一条记录) ==========
@router.get("/summary", response_model=List[DeviceSummary])
async def devices_summary(db: AsyncSession = Depends(get_db)):
    """返回所有设备名 + 最新温湿度 (来自合并后的 inference_results)。"""
    latest = (
        select(
            InferenceResult.device_id.label("device_id"),
            InferenceResult.temperature.label("temperature"),
            InferenceResult.humidity.label("humidity"),
            InferenceResult.recorded_at.label("recorded_at"),
            func.row_number().over(
                partition_by=InferenceResult.device_id,
                order_by=InferenceResult.recorded_at.desc(),
            ).label("rn"),
        ).subquery()
    )
    result = await db.execute(
        select(Device.device_name, latest.c.temperature, latest.c.humidity, latest.c.recorded_at)
        .outerjoin(latest, (latest.c.device_id == Device.id) & (latest.c.rn == 1))
        .order_by(Device.device_name)
    )
    return [
        DeviceSummary(
            device_name=r.device_name,
            temperature=r.temperature,
            humidity=r.humidity,
            recorded_at=r.recorded_at,
        )
        for r in result.all()
    ]


# ========== 数据下载设备目录 (每设备记录数) ==========
@router.get("/download-catalog", response_model=List[DeviceDownloadCatalog])
async def device_download_catalog(db: AsyncSession = Depends(get_db)):
    """返回每个设备名 + 可用记录条数。"""
    counts = (
        select(InferenceResult.device_id.label("device_id"), func.count().label("cnt"))
        .group_by(InferenceResult.device_id)
        .subquery()
    )
    result = await db.execute(
        select(Device.device_name, func.coalesce(counts.c.cnt, 0).label("record_count"))
        .outerjoin(counts, counts.c.device_id == Device.id)
        .order_by(Device.device_name)
    )
    return [
        DeviceDownloadCatalog(device_name=r.device_name, record_count=int(r.record_count or 0))
        for r in result.all()
    ]


async def _device_with_details(device: Device, db: AsyncSession) -> DeviceWithDetails:
    # 方案 C: 环境指标与推理同在一行, 取该设备最新一条记录
    record = (
        await db.execute(
            select(InferenceResult)
            .where(InferenceResult.device_id == device.id)
            .order_by(InferenceResult.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    data = DeviceResponse.model_validate(device).model_dump()
    data.update(
        temperature=record.temperature if record else None,
        humidity=record.humidity if record else None,
        battery_voltage=record.battery_voltage if record else None,
        signal_strength=record.signal_strength if record else None,
        sensor_recorded_at=record.recorded_at if record else None,
        probability_no_queen=record.probability_no_queen if record else None,
        probability_normal=record.probability_normal if record else None,
        inference_label=record.label if record else None,
        confidence=record.confidence if record else None,
        inference_recorded_at=record.recorded_at if record else None,
        model_version=record.model_version if record else None,
    )
    return DeviceWithDetails(**data)


# ========== 按设备名查询详情 ==========
@router.get("/by-name/{device_name}", response_model=DeviceWithDetails)
async def get_device_by_name(device_name: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.device_name == device_name))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return await _device_with_details(device, db)


# ========== 设备详情 (含最新记录) ==========
@router.get("/{device_id}", response_model=DeviceWithDetails)
async def get_device(device_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return await _device_with_details(device, db)


# ========== 更新设备 ==========
@router.patch("/{device_id}", response_model=DeviceResponse)
async def update_device(device_id: int, device_in: DeviceUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    update_data = device_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(device, field, value)

    device.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(device)
    return DeviceResponse.model_validate(device)


# ========== 删除设备 ==========
@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(device_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    await db.delete(device)
    await db.commit()


# ========== 设备上传推理结果 (含环境指标, 一次上报) ==========
@router.post("/inference", response_model=InferenceResultResponse, status_code=status.HTTP_201_CREATED)
async def upload_inference(
    data: InferenceResultCreate,
    device: Device = Depends(verify_device_auth),
    db: AsyncSession = Depends(get_db),
):
    inference = InferenceResult(
        device_id=device.id,
        device_name=device.device_name,
        audio_object_key=data.audio_object_key,
        feature_object_key=data.feature_object_key,
        model_version=data.model_version,
        probability_no_queen=data.probability_no_queen,
        probability_normal=data.probability_normal,
        label=data.label,
        confidence=data.confidence,
        inference_time_ms=data.inference_time_ms,
        temperature=data.temperature,
        humidity=data.humidity,
        battery_voltage=data.battery_voltage,
        signal_strength=data.signal_strength,
        recorded_at=data.recorded_at or datetime.utcnow(),
    )
    db.add(inference)

    device.last_seen_at = datetime.utcnow()
    device.status = DeviceStatus.ONLINE

    await db.commit()
    await db.refresh(inference)
    return InferenceResultResponse.model_validate(inference)


# ========== 获取设备记录 (含环境指标, 供看板曲线) ==========
@router.get("/{device_id}/records", response_model=PageResponse)
async def get_device_records(
    device_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(InferenceResult).where(InferenceResult.device_id == device_id)
    count_query = select(func.count(InferenceResult.id)).where(InferenceResult.device_id == device_id)

    if start_time:
        query = query.where(InferenceResult.recorded_at >= start_time)
        count_query = count_query.where(InferenceResult.recorded_at >= start_time)
    if end_time:
        query = query.where(InferenceResult.recorded_at <= end_time)
        count_query = count_query.where(InferenceResult.recorded_at <= end_time)

    total = (await db.execute(count_query)).scalar()
    query = query.order_by(InferenceResult.recorded_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    items = result.scalars().all()

    return PageResponse.create(
        items=[DeviceRecordResponse.model_validate(i).model_dump() for i in items],
        total=total,
        page=page,
        size=size,
    )


# ========== 获取推理历史 ==========
@router.get("/{device_id}/inferences", response_model=PageResponse)
async def get_inference_history(
    device_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    label: Optional[InferenceLabel] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(InferenceResult).where(InferenceResult.device_id == device_id)
    count_query = select(func.count(InferenceResult.id)).where(InferenceResult.device_id == device_id)

    if label:
        query = query.where(InferenceResult.label == label)
        count_query = count_query.where(InferenceResult.label == label)
    if start_time:
        query = query.where(InferenceResult.recorded_at >= start_time)
        count_query = count_query.where(InferenceResult.recorded_at >= start_time)
    if end_time:
        query = query.where(InferenceResult.recorded_at <= end_time)
        count_query = count_query.where(InferenceResult.recorded_at <= end_time)

    total = (await db.execute(count_query)).scalar()
    query = query.order_by(InferenceResult.recorded_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    items = result.scalars().all()

    return PageResponse.create(
        items=[InferenceResultResponse.model_validate(i).model_dump() for i in items],
        total=total,
        page=page,
        size=size,
    )


# ========== 获取预签名上传 URL ==========
@router.get("/{device_id}/presigned-url")
async def get_presigned_upload_url(
    device_id: int,
    filename: str = Query(..., description="文件名"),
    content_type: str = Query("audio/wav", description="MIME 类型"),
    expires: int = Query(3600, ge=60, le=86400, description="过期时间(秒)"),
    device: Device = Depends(verify_device_auth),
    minio: MinIOClient = Depends(get_minio_client),
):
    """获取预签名上传 URL，设备直传 MinIO"""
    if device.id != device_id:
        raise HTTPException(status_code=403, detail="Device ID mismatch")

    object_key = f"audio/{datetime.utcnow().strftime('%Y/%m/%d')}/{device.device_name}/{filename}"

    upload_url = minio.get_presigned_put_url(object_key, expires_seconds=expires)

    return APIResponse(data={
        "upload_url": upload_url,
        "object_key": object_key,
        "expires_in": expires,
        "method": "PUT",
        "headers": {"Content-Type": content_type},
    })


# ========== 获取预签名下载 URL ==========
@router.get("/{device_id}/download-url/{object_key:path}")
async def get_presigned_download_url(
    device_id: int,
    object_key: str,
    expires: int = Query(3600, ge=60, le=86400),
    device: Device = Depends(verify_device_auth),
    minio: MinIOClient = Depends(get_minio_client),
):
    if not minio.file_exists(object_key):
        raise HTTPException(status_code=404, detail="File not found")

    download_url = minio.get_presigned_url(object_key, expires_seconds=expires)
    return APIResponse(data={"download_url": download_url, "expires_in": expires})
