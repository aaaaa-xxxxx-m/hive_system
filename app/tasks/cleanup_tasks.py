from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timedelta
from app.core.database import task_session
from app.models.models import RawFeature, AuditLog, Device, DeviceStatus
from sqlalchemy import delete, update, select, func
import asyncio

logger = get_task_logger(__name__)


@shared_task
def cleanup_old_raw_features():
    """清理 30 天前的原始特征数据"""
    logger.info("Cleaning up old raw features...")

    async def _cleanup():
        cutoff = datetime.utcnow() - timedelta(days=30)

        async with task_session() as db:
            # 先获取要删除的文件键，以便从 MinIO 删除
            from app.services.minio_client import get_minio_client
            minio = get_minio_client()

            result = await db.execute(
                select(RawFeature.minio_object_key)
                .where(RawFeature.recorded_at < cutoff, RawFeature.minio_object_key.isnot(None))
            )
            keys_to_delete = [row[0] for row in result.all()]

            # 删除数据库记录
            await db.execute(
                delete(RawFeature).where(RawFeature.recorded_at < cutoff)
            )
            await db.commit()

            # 删除 MinIO 文件
            for key in keys_to_delete:
                try:
                    minio.delete_file(key)
                except Exception as e:
                    logger.warning(f"Failed to delete MinIO file {key}: {e}")

            logger.info(f"Cleaned up {len(keys_to_delete)} raw feature records and files")

    asyncio.run(_cleanup())


@shared_task
def cleanup_old_audit_logs():
    """清理 90 天前的审计日志"""
    logger.info("Cleaning up old audit logs...")

    async def _cleanup():
        cutoff = datetime.utcnow() - timedelta(days=90)

        async with task_session() as db:
            result = await db.execute(
                delete(AuditLog).where(AuditLog.created_at < cutoff)
            )
            await db.commit()
            logger.info(f"Cleaned up {result.rowcount} audit log records")

    asyncio.run(_cleanup())


@shared_task
def check_device_heartbeat():
    """检查设备心跳，标记离线设备"""
    logger.debug("Checking device heartbeats...")

    async def _check():
        threshold = datetime.utcnow() - timedelta(minutes=10)

        async with task_session() as db:
            # 找出超过 10 分钟无心跳的在线设备
            result = await db.execute(
                select(Device.id, Device.device_name)
                .where(
                    Device.status == DeviceStatus.ONLINE,
                    Device.last_seen_at < threshold,
                )
            )
            offline_devices = result.all()

            if offline_devices:
                device_ids = [d[0] for d in offline_devices]
                await db.execute(
                    update(Device)
                    .where(Device.id.in_(device_ids))
                    .values(status=DeviceStatus.OFFLINE)
                )
                await db.commit()
                logger.info(f"Marked {len(offline_devices)} devices as offline: {[d[1] for d in offline_devices]}")

    asyncio.run(_check())


@shared_task
def cleanup_orphaned_minio_files():
    """清理数据库中不存在的 MinIO 孤儿文件"""
    logger.info("Checking for orphaned MinIO files...")

    async def _cleanup():
        from app.services.minio_client import get_minio_client
        minio = get_minio_client()

        async with task_session() as db:
            # 获取所有被引用的 object_key
            from app.models.models import InferenceResult, RawFeature, ModelVersion
            from sqlalchemy import union_all

            referenced_keys = set()

            # InferenceResult audio keys
            result = await db.execute(select(InferenceResult.audio_object_key))
            referenced_keys.update(r[0] for r in result.all())

            # InferenceResult feature keys
            result = await db.execute(select(InferenceResult.feature_object_key).where(InferenceResult.feature_object_key.isnot(None)))
            referenced_keys.update(r[0] for r in result.all())

            # RawFeature keys
            result = await db.execute(select(RawFeature.minio_object_key).where(RawFeature.minio_object_key.isnot(None)))
            referenced_keys.update(r[0] for r in result.all())

            # ModelVersion keys
            result = await db.execute(select(ModelVersion.minio_object_key))
            referenced_keys.update(r[0] for r in result.all())

            result = await db.execute(select(ModelVersion.scaler_object_key).where(ModelVersion.scaler_object_key.isnot(None)))
            referenced_keys.update(r[0] for r in result.all())

            # 扫描 MinIO 找出孤儿文件
            all_keys = minio.list_files()
            orphaned = [k for k in all_keys if k not in referenced_keys]

            if orphaned:
                logger.warning(f"Found {len(orphaned)} orphaned files (not deleting automatically)")
                # 实际生产中可考虑移到归档桶或标记删除
                for key in orphaned[:10]:  # 只记录前 10 个
                    logger.warning(f"  Orphaned: {key}")

    asyncio.run(_cleanup())