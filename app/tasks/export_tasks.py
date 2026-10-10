"""管理员设备数据导出：按分类将设备数据打包为 zip 上传 MinIO。"""
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
from app.models.models import (
    DataExportJob,
    DownloadJob,
    DownloadJobStatus,
    InferenceResult,
    ModelVersion,
    TrainingJob,
)
from app.services.device_data import FEATURE_PREFIX, MODEL_PREFIX, list_audio_keys
from app.services.minio_client import get_minio_client

logger = get_task_logger(__name__)
settings = get_settings()
minio = get_minio_client()


def _enum_val(v) -> str:
    return getattr(v, "value", v) if v is not None else ""


def _iso(v) -> str:
    return v.isoformat() if v else ""


async def _save(Session, job_id: str, **values):
    async with Session() as db:
        await db.execute(
            update(DataExportJob).where(DataExportJob.job_id == job_id).values(**values)
        )
        await db.commit()


async def _export(job_id: str):
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _save(Session, job_id, status=DownloadJobStatus.RUNNING, progress=5)

        async with Session() as db:
            job = (
                await db.execute(select(DataExportJob).where(DataExportJob.job_id == job_id))
            ).scalar_one_or_none()
            if not job:
                raise RuntimeError(f"DataExportJob {job_id} not found")
            device_name = job.device_name
            cats = set(job.categories or [])

        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as z:
            # ---- MinIO 文件类 ----
            if "audio" in cats:
                for k in list_audio_keys(device_name):
                    try:
                        z.writestr(k, minio.download_file(k))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"export audio skip {k}: {e}")
            if "features" in cats:
                for k in minio.list_files(f"{FEATURE_PREFIX}{device_name}/"):
                    try:
                        z.writestr(k, minio.download_file(k))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"export feature skip {k}: {e}")
            if "models" in cats:
                for k in minio.list_files(f"{MODEL_PREFIX}{device_name}/"):
                    try:
                        z.writestr(k, minio.download_file(k))
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"export model skip {k}: {e}")

            await _save(Session, job_id, progress=60)

            # ---- DB 记录类（CSV）----
            async with Session() as db:
                if "inference" in cats:
                    rows = (
                        await db.execute(
                            select(InferenceResult)
                            .where(InferenceResult.device_name == device_name)
                            .order_by(InferenceResult.recorded_at.asc())
                        )
                    ).scalars().all()
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    w.writerow(["时间", "温度(℃)", "湿度(%)",
                                "失王概率", "标签", "置信度", "模型版本", "音频键"])
                    for r in rows:
                        w.writerow([_iso(r.recorded_at), r.temperature, r.humidity,
                                    r.probability_no_queen, _enum_val(r.label),
                                    r.confidence, r.model_version, r.audio_object_key])
                    z.writestr("inference_results.csv", buf.getvalue().encode("utf-8-sig"))

                if "training" in cats:
                    trows = (
                        await db.execute(
                            select(TrainingJob).where(TrainingJob.device_name == device_name)
                            .order_by(TrainingJob.created_at.asc())
                        )
                    ).scalars().all()
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    w.writerow(["job_id", "状态", "特征数", "进度", "请求时间",
                                "完成时间", "错误", "模型版本ID"])
                    for r in trows:
                        w.writerow([r.job_id, _enum_val(r.status), r.feature_count, r.progress,
                                    _iso(r.requested_at), _iso(r.completed_at),
                                    r.error_message, r.model_version_id])
                    z.writestr("training_jobs.csv", buf.getvalue().encode("utf-8-sig"))

                    mvrows = (
                        await db.execute(
                            select(ModelVersion).where(
                                ModelVersion.minio_object_key.like(f"{MODEL_PREFIX}{device_name}/%")
                            )
                        )
                    ).scalars().all()
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    w.writerow(["id", "version", "模型键", "大小(字节)", "训练样本数",
                                "状态", "是否生产", "训练时间"])
                    for r in mvrows:
                        w.writerow([r.id, r.version, r.minio_object_key, r.file_size,
                                    r.training_dataset_size, _enum_val(r.status),
                                    r.is_active, _iso(r.trained_at)])
                    z.writestr("model_versions.csv", buf.getvalue().encode("utf-8-sig"))

                if "downloads" in cats:
                    drows = (
                        await db.execute(
                            select(DownloadJob).where(DownloadJob.device_name == device_name)
                            .order_by(DownloadJob.created_at.asc())
                        )
                    ).scalars().all()
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    w.writerow(["job_id", "账号", "数量", "状态", "创建时间", "完成时间", "导出键"])
                    for r in drows:
                        w.writerow([r.job_id, r.username, r.data_limit, _enum_val(r.status),
                                    _iso(r.created_at), _iso(r.completed_at), r.object_key])
                    z.writestr("download_jobs.csv", buf.getvalue().encode("utf-8-sig"))

        object_key = f"exports/{device_name}/{job_id}.zip"
        minio.upload_file(object_key, outer.getvalue(), content_type="application/zip")

        await _save(Session, job_id, status=DownloadJobStatus.COMPLETED, progress=100,
                    object_key=object_key, completed_at=datetime.utcnow())
        logger.info(f"[export] {device_name} done cats={sorted(cats)} key={object_key}")
        return {"job_id": job_id, "object_key": object_key, "categories": sorted(cats)}

    except Exception as e:  # noqa: BLE001
        logger.exception("[export] failed")
        try:
            await _save(Session, job_id, status=DownloadJobStatus.FAILED, error_message=str(e))
        except Exception:
            pass
        raise
    finally:
        await engine.dispose()


@shared_task(bind=True, max_retries=0)
def export_device_data_task(self, job_id: str, device_name: str, categories: list):
    logger.info(f"[export] start job={job_id} device={device_name} cats={categories}")
    return asyncio.run(_export(job_id))
