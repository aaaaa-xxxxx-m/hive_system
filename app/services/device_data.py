"""设备数据管理：占用统计、清除、遗留数据检测。

数据分布：
- MinIO: features/<name>/、models/<name>/、audio/<Y>/<m>/<d>/<name>/、exports/<name>/
- DB: inference_results / raw_features / training_jobs / download_jobs（按 device_name）
"""
from typing import Dict, Iterable, List, Set, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Device,
    DownloadJob,
    InferenceResult,
    ModelVersion,
    RawFeature,
    TrainingJob,
)
from app.schemas.schemas import DATA_CATEGORIES
from app.services.minio_client import get_minio_client

AUDIO_PREFIX = "audio/"
FEATURE_PREFIX = "features/"
MODEL_PREFIX = "models/"
EXPORT_PREFIX = "exports/"


def _minio():
    return get_minio_client()


def _device_from_audio_key(key: str) -> str | None:
    # audio/<YYYY>/<MM>/<DD>/<device>/<file>
    parts = key.split("/")
    if len(parts) >= 5:
        return parts[4]
    return None


def list_audio_keys(device_name: str) -> List[str]:
    return [k for k in _minio().list_files(AUDIO_PREFIX) if _device_from_audio_key(k) == device_name]


def list_minio_device_names() -> Set[str]:
    m = _minio()
    names: Set[str] = set()
    for prefix in (FEATURE_PREFIX, MODEL_PREFIX, EXPORT_PREFIX):
        for k in m.list_files(prefix):
            p = k.split("/")
            if len(p) >= 2 and p[1]:
                names.add(p[1])
    for k in m.list_files(AUDIO_PREFIX):
        n = _device_from_audio_key(k)
        if n:
            names.add(n)
    return names


async def usage(db: AsyncSession, device_name: str) -> Dict[str, int]:
    inference = (
        await db.execute(
            select(func.count(InferenceResult.id)).where(InferenceResult.device_name == device_name)
        )
    ).scalar() or 0
    training = (
        await db.execute(
            select(func.count(TrainingJob.id)).where(TrainingJob.device_name == device_name)
        )
    ).scalar() or 0
    downloads = (
        await db.execute(
            select(func.count(DownloadJob.id)).where(DownloadJob.device_name == device_name)
        )
    ).scalar() or 0
    m = _minio()
    features = len(m.list_files(f"{FEATURE_PREFIX}{device_name}/"))
    models = len(m.list_files(f"{MODEL_PREFIX}{device_name}/"))
    audio = len(list_audio_keys(device_name))
    return {
        "audio": audio,
        "models": models,
        "features": features,
        "inference": int(inference),
        "training": int(training),
        "downloads": int(downloads),
    }


async def has_leftover(db: AsyncSession, device_name: str) -> bool:
    """该设备名是否存在遗留数据（DB 关联 或 MinIO 特征/模型对象）。"""
    for model in (InferenceResult, TrainingJob, DownloadJob, RawFeature):
        cnt = (
            await db.execute(
                select(func.count(model.id)).where(model.device_name == device_name)
            )
        ).scalar() or 0
        if cnt:
            return True
    m = _minio()
    if m.list_files(f"{FEATURE_PREFIX}{device_name}/"):
        return True
    if m.list_files(f"{MODEL_PREFIX}{device_name}/"):
        return True
    return False


async def clear_device_data(
    db: AsyncSession, device_name: str, categories: Iterable[str]
) -> Dict[str, int]:
    cats = [c for c in categories if c in DATA_CATEGORIES]
    m = _minio()
    deleted: Dict[str, int] = {c: 0 for c in cats}

    def _rm(keys: List[str]) -> None:
        for k in keys:
            try:
                m.delete_file(k)
            except Exception:  # noqa: BLE001
                pass

    if "inference" in cats:
        res = await db.execute(
            delete(InferenceResult).where(InferenceResult.device_name == device_name)
        )
        deleted["inference"] = res.rowcount or 0

    if "training" in cats:
        res = await db.execute(
            delete(TrainingJob).where(TrainingJob.device_name == device_name)
        )
        deleted["training"] = res.rowcount or 0

    if "downloads" in cats:
        rows = (
            await db.execute(select(DownloadJob).where(DownloadJob.device_name == device_name))
        ).scalars().all()
        for j in rows:
            if j.object_key:
                try:
                    m.delete_file(j.object_key)
                except Exception:  # noqa: BLE001
                    pass
            await db.delete(j)
        deleted["downloads"] = len(rows)
        # 该设备的导出包一并清理
        _rm(m.list_files(f"{EXPORT_PREFIX}{device_name}/"))

    if "audio" in cats:
        keys = list_audio_keys(device_name)
        _rm(keys)
        deleted["audio"] = len(keys)

    if "features" in cats:
        keys = m.list_files(f"{FEATURE_PREFIX}{device_name}/")
        _rm(keys)
        deleted["features"] = len(keys)

    if "models" in cats:
        keys = m.list_files(f"{MODEL_PREFIX}{device_name}/")
        _rm(keys)
        deleted["models"] = len(keys)
        await db.execute(
            delete(ModelVersion).where(
                ModelVersion.minio_object_key.like(f"{MODEL_PREFIX}{device_name}/%")
            )
        )

    await db.commit()
    return deleted


async def detect_orphans(db: AsyncSession) -> List[Tuple[str, Dict[str, int]]]:
    """无对应设备行的遗留数据（name + 占用）。"""
    existing = set((await db.execute(select(Device.device_name))).scalars().all())
    names: Set[str] = set()
    for model in (InferenceResult, TrainingJob, DownloadJob, RawFeature):
        rows = (
            await db.execute(
                select(model.device_name).where(model.device_name.isnot(None)).distinct()
            )
        ).scalars().all()
        names.update(r for r in rows if r)
    names.update(list_minio_device_names())
    names -= existing
    out: List[Tuple[str, Dict[str, int]]] = []
    for n in sorted(names):
        out.append((n, await usage(db, n)))
    return out
