from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from redis import asyncio as aioredis
from minio import Minio
from minio.error import S3Error
from typing import Dict
from datetime import datetime

from app.core.database import get_db, engine
from app.core.config import get_settings
from app.schemas.schemas import HealthResponse, ReadyResponse, APIResponse

router = APIRouter(tags=["Health"])

settings = get_settings()


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """基础健康检查 - 不检查依赖，仅确认进程存活"""
    return HealthResponse(
        status="healthy",
        version=settings.APP_VERSION,
        timestamp=datetime.utcnow(),
        checks={"app": "ok"},
    )


@router.get("/ready", response_model=ReadyResponse)
async def readiness_check(db: AsyncSession = Depends(get_db)):
    """就绪检查 - 验证所有依赖服务可用"""
    checks: Dict[str, bool] = {}
    all_ready = True

    # 检查数据库
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as e:
        checks["database"] = False
        all_ready = False

    # 检查 Redis
    try:
        redis = aioredis.from_url(settings.REDIS_URL)
        await redis.ping()
        await redis.close()
        checks["redis"] = True
    except Exception:
        checks["redis"] = False
        all_ready = False

    # 检查 MinIO
    try:
        minio = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        minio.list_buckets()
        checks["minio"] = True
    except S3Error:
        checks["minio"] = False
        all_ready = False

    return ReadyResponse(ready=all_ready, checks=checks)


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus metrics endpoint"""
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from fastapi.responses import Response
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)