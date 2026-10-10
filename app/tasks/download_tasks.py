import asyncio
import csv
import io
import zipfile
from datetime import datetime

from celery import shared_task
from celery.utils.log import get_task_logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.models.models import InferenceResult, DownloadJob, DownloadJobStatus
from app.services.minio_client import get_minio_client

logger = get_task_logger(__name__)
settings = get_settings()
minio = get_minio_client()


async def _save(Session, job_id: str, **values):
    async with Session() as db:
        await db.execute(update(DownloadJob).where(DownloadJob.job_id == job_id).values(**values))
        await db.commit()


def _label_value(label) -> str:
    return getattr(label, "value", label)


async def _export(job_id: str):
    # Celery 一次性任务: 专用 NullPool 引擎, 在本次事件循环内创建/销毁,
    # 避免复用绑定到其它 loop 的连接池。
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _save(Session, job_id, status=DownloadJobStatus.RUNNING, progress=5,
                    started_at=datetime.utcnow())

        async with Session() as db:
            job = (
                await db.execute(select(DownloadJob).where(DownloadJob.job_id == job_id))
            ).scalar_one_or_none()
            if not job:
                raise RuntimeError(f"DownloadJob {job_id} not found")
            device_name = job.device_name
            rows = (
                await db.execute(
                    select(InferenceResult)
                    .where(InferenceResult.device_name == device_name)
                    .order_by(InferenceResult.recorded_at.desc())
                    .limit(job.data_limit)
                )
            ).scalars().all()

        rows = list(reversed(rows))  # 按时间升序编号 0001..N

        metrics = io.StringIO()
        writer = csv.writer(metrics)
        writer.writerow([
            "序号", "文件名", "时间", "温度(℃)", "湿度(%)",
            "失王概率", "标签", "置信度", "模型版本", "原始音频键",
        ])

        # 直接平铺：<zip>/audio/0001.wav ... <zip>/metrics.csv（不再嵌套内层 zip，避免“看起来是空的”）
        outer = io.BytesIO()
        missing = 0
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as bundle:
            for idx, r in enumerate(rows, start=1):
                seq = f"{idx:04d}"
                arcname = f"audio/{seq}.wav"
                try:
                    data = minio.download_file(r.audio_object_key)
                    bundle.writestr(arcname, data)
                except Exception as e:  # noqa: BLE001
                    missing += 1
                    logger.warning(f"audio missing: {r.audio_object_key}: {e}")
                writer.writerow([
                    idx, f"{seq}.wav",
                    r.recorded_at.isoformat() if r.recorded_at else "",
                    r.temperature, r.humidity,
                    r.probability_no_queen, _label_value(r.label), r.confidence,
                    r.model_version, r.audio_object_key,
                ])
            bundle.writestr("metrics.csv", metrics.getvalue().encode("utf-8-sig"))

        await _save(Session, job_id, progress=60)

        object_key = f"exports/{device_name}/{job_id}.zip"
        minio.upload_file(object_key, outer.getvalue(), content_type="application/zip")

        await _save(Session, job_id, status=DownloadJobStatus.COMPLETED, progress=100,
                    object_key=object_key, completed_at=datetime.utcnow())
        logger.info(f"Export done: {object_key} records={len(rows)} missing_audio={missing}")
        return {"job_id": job_id, "object_key": object_key,
                "records": len(rows), "missing_audio": missing}

    except Exception as e:  # noqa: BLE001
        logger.exception("Export failed")
        try:
            await _save(Session, job_id, status=DownloadJobStatus.FAILED, error_message=str(e))
        except Exception:
            pass
        raise
    finally:
        await engine.dispose()


@shared_task(bind=True, max_retries=0)
def export_device_data(self, job_id: str, device_name: str, limit: int):
    """打包某设备最近 N 条记录: 音频(按序号命名) + 指标表格 → MinIO。"""
    logger.info(f"Export start: job={job_id} device={device_name} limit={limit}")
    return asyncio.run(_export(job_id))
