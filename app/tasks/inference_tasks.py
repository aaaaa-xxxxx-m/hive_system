from celery import shared_task
from celery.utils.log import get_task_logger
import numpy as np
import joblib
import io
import time
from app.tasks.celery_app import celery_app
from app.services.minio_client import get_minio_client
from app.services.model_service import get_model_manager
from app.core.database import async_session_maker
from app.models.models import InferenceResult, Device, InferenceLabel
from sqlalchemy import select, update
import asyncio

logger = get_task_logger(__name__)
minio_client = get_minio_client()
model_manager = get_model_manager()

# 全局缓存当前生产模型
_cached_model = None
_cached_scaler = None
_cached_version = None


async def _get_production_model():
    """获取当前生产模型"""
    global _cached_model, _cached_scaler, _cached_version

    async with async_session_maker() as db:
        from app.models.models import ModelVersion, ModelStatus
        from sqlalchemy import select
        result = await db.execute(
            select(ModelVersion)
            .where(ModelVersion.is_active == True, ModelVersion.status == ModelStatus.ACTIVE)
            .order_by(ModelVersion.deployed_at.desc())
        )
        model_version = result.scalar_one_or_none()

    if not model_version:
        raise RuntimeError("No active production model found")

    if _cached_version != model_version.version:
        logger.info(f"Loading production model: {model_version.version}")
        _cached_model, _cached_scaler = model_manager.load_model(model_version.version)
        _cached_version = model_version.version

    return _cached_model, _cached_scaler, model_version.version


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def async_inference_task(self, device_id: int, device_name: str, audio_object_key: str, feature_object_key: str = None):
    """异步推理任务"""
    logger.info(f"Running inference for device {device_name}, audio: {audio_object_key}")

    async def _infer():
        start_time = time.time()

        # 1. 获取生产模型
        model, scaler, model_version = await _get_production_model()

        # 2. 下载音频/特征文件
        if feature_object_key and minio_client.file_exists(feature_object_key):
            feature_data = minio_client.download_file(feature_object_key)
            features = joblib.load(io.BytesIO(feature_data))
        else:
            # 从音频提取特征 (简化版，实际应调用特征提取服务)
            audio_data = minio_client.download_file(audio_object_key)
            # TODO: 实现音频特征提取
            # 暂时使用模拟特征
            features = np.random.randn(1, 193).astype(np.float32)

        # 3. 预处理
        if scaler:
            features = scaler.transform(features.reshape(1, -1))

        # 4. 推理
        inference_start = time.time()
        proba = model.predict_proba(features)[0]
        inference_time_ms = int((time.time() - inference_start) * 1000)

        # SVM classes_: [0: no_queen, 1: normal]
        prob_no_queen = float(proba[0]) if len(proba) > 0 else 0.0
        prob_normal = float(proba[1]) if len(proba) > 1 else 0.0

        # 确定标签
        if prob_no_queen > 0.7:
            label = InferenceLabel.NO_QUEEN
            confidence = prob_no_queen
        elif prob_normal > 0.7:
            label = InferenceLabel.NORMAL
            confidence = prob_normal
        else:
            label = InferenceLabel.UNCERTAIN
            confidence = max(prob_no_queen, prob_normal)

        total_time_ms = int((time.time() - start_time) * 1000)

        # 5. 保存结果
        async with async_session_maker() as db:
            result = InferenceResult(
                device_id=device_id,
                device_name=device_name,
                audio_object_key=audio_object_key,
                feature_object_key=feature_object_key,
                model_version=model_version,
                probability_no_queen=prob_no_queen,
                probability_normal=prob_normal,
                label=label,
                confidence=confidence,
                inference_time_ms=inference_time_ms,
            )
            db.add(result)

            # 更新设备最后心跳
            await db.execute(
                update(Device)
                .where(Device.id == device_id)
                .values(last_seen_at=result.processed_at)
            )

            await db.commit()

        logger.info(f"Inference completed: device={device_name}, label={label.value}, confidence={confidence:.4f}, time={total_time_ms}ms")

        return {
            "device_id": device_id,
            "label": label.value,
            "confidence": confidence,
            "probability_no_queen": prob_no_queen,
            "probability_normal": prob_normal,
            "model_version": model_version,
            "inference_time_ms": inference_time_ms,
        }

    return asyncio.run(_infer())


@shared_task
def batch_inference_task(device_ids: list, audio_keys: list):
    """批量推理任务"""
    results = []
    for device_id, audio_key in zip(device_ids, audio_keys):
        # 这里简化处理，实际应该并行
        result = async_inference_task(device_id, f"device-{device_id}", audio_key)
        results.append(result)
    return results