import asyncio
import csv
import io
import zipfile
from datetime import datetime

from celery import shared_task
from celery.utils.log import get_task_logger
from sqlalchemy import select, update

from app.core.database import async_session_maker
from app.models.models import InferenceResult, DownloadJob, DownloadJobStatus
from app.services.minio_client import get_minio_client

logger = get_task_logger(__name__)
minio = get_minio_client()


async def _save(job_id: str, **values):
    async with async_session_maker() as db:
        await db.execute(update(DownloadJob).where(DownloadJob.job_id == job_id).values(**values))
        await db.commit()


def _label_value(label) -> str:
    return getattr(label, "value", label)


async def _export(job_id: str):
    """全部 DB / IO 都在同一事件循环内完成 (避免跨 loop 的 Future 错误)。"""
    try:
        await _save(job_id, status=DownloadJobStatus.RUNNING, progress=5,
                    started_at=datetime.utcnow())

        async with async_session_maker() as db:
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
            "序号", "文件名", "时间", "温度(℃)", "湿度(%)", "电池电压(V)",
            "信号强度(dBm)", "失王概率", "标签", "置信度", "模型版本", "原始音频键",
        ])

        audio_buf = io.BytesIO()
        missing = 0
        with zipfile.ZipFile(audio_buf, "w", zipfile.ZIP_DEFLATED) as audio_zip:
            for idx, r in enumerate(rows, start=1):
                seq = f"{idx:04d}"
                fname = f"{seq}.wav"
                try:
                    data = minio.download_file(r.audio_object_key)
                    audio_zip.writestr(fname, data)
                except Exception as e:  # noqa: BLE001
                    missing += 1
                    logger.warning(f"audio missing: {r.audio_object_key}: {e}")
                writer.writerow([
                    idx, fname,
                    r.recorded_at.isoformat() if r.recorded_at else "",
                    r.temperature, r.humidity, r.battery_voltage, r.signal_strength,
                    r.probability_no_queen, _label_value(r.label), r.confidence,
                    r.model_version, r.audio_object_key,
                ])

        await _save(job_id, progress=60)

        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("audio.zip", audio_buf.getvalue())
            bundle.writestr("metrics.csv", metrics.getvalue().encode("utf-8-sig"))

        object_key = f"exports/{device_name}/{job_id}.zip"
        minio.upload_file(object_key, outer.getvalue(), content_type="application/zip")

        await _save(job_id, status=DownloadJobStatus.COMPLETED, progress=100,
                    object_key=object_key, completed_at=datetime.utcnow())
        logger.info(f"Export done: {object_key} records={len(rows)} missing_audio={missing}")
        return {"job_id": job_id, "object_key": object_key,
                "records": len(rows), "missing_audio": missing}

    except Exception as e:  # noqa: BLE001
        logger.exception("Export failed")
        try:
            await _save(job_id, status=DownloadJobStatus.FAILED, error_message=str(e))
        except Exception:
            pass
        raise


@shared_task(bind=True, max_retries=0)
def export_device_data(self, job_id: str, device_name: str, limit: int):
    """打包某设备最近 N 条记录: 音频(按序号命名) + 指标表格 → MinIO。"""
    logger.info(f"Export start: job={job_id} device={device_name} limit={limit}")
    return asyncio.run(_export(job_id))
