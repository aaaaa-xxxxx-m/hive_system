from celery import shared_task
from celery.utils.log import get_task_logger
import joblib
import io
import numpy as np
import hashlib
import json
from datetime import datetime
from sklearn.svm import SVC
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from app.tasks.celery_app import celery_app
from app.services.minio_client import get_minio_client
from app.services.model_service import get_model_manager
from app.core.database import async_session_maker
from app.core.config import get_settings
from app.models.models import TrainingJob, ModelVersion, ModelStatus, TrainingJobStatus, ModelType
from app.services.feature_extract import FEATURE_DIM
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
import asyncio
from datetime import timedelta, timezone

settings = get_settings()

# 按设备训练：正样本来自设备上传(正常类,label=0)，负样本为服务器预置(失王类,label=1)
REQUIRED_POSITIVE = 600
REQUIRED_NEGATIVE = 600
NEG_PREFIX = "datasets/preset/negative/"
WAIT_TIMEOUT_HOURS = 24

logger = get_task_logger(__name__)
minio_client = get_minio_client()
model_manager = get_model_manager()


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def train_model_task(self, job_id: str, config: dict, positive_keys: list, negative_keys: list):
    """异步训练模型任务"""
    logger.info(f"Starting training job {job_id}")

    async def _train():
        async with async_session_maker() as db:
            # 更新任务状态为运行中
            await db.execute(
                update(TrainingJob)
                .where(TrainingJob.job_id == job_id)
                .values(
                    status=TrainingJobStatus.RUNNING,
                    started_at=datetime.utcnow(),
                    progress=10,
                )
            )
            await db.commit()

            try:
                # 1. 下载训练数据
                logger.info(f"Downloading {len(positive_keys)} positive + {len(negative_keys)} negative samples")
                X_pos = []
                X_neg = []

                for key in positive_keys:
                    data = minio_client.download_file(key)
                    features = joblib.load(io.BytesIO(data))
                    if isinstance(features, np.ndarray):
                        X_pos.append(features.flatten())
                    elif isinstance(features, list):
                        X_pos.append(np.array(features).flatten())

                for key in negative_keys:
                    data = minio_client.download_file(key)
                    features = joblib.load(io.BytesIO(data))
                    if isinstance(features, np.ndarray):
                        X_neg.append(features.flatten())
                    elif isinstance(features, list):
                        X_neg.append(np.array(features).flatten())

                if not X_pos or not X_neg:
                    raise ValueError("No valid training samples downloaded")

                X = np.vstack([X_pos, X_neg])
                y = np.hstack([np.ones(len(X_pos)), np.zeros(len(X_neg))])

                await db.execute(
                    update(TrainingJob)
                    .where(TrainingJob.job_id == job_id)
                    .values(progress=30)
                )
                await db.commit()

                # 2. 数据标准化
                scaler = StandardScaler()
                X_scaled = scaler.fit_transform(X)

                await db.execute(
                    update(TrainingJob)
                    .where(TrainingJob.job_id == job_id)
                    .values(progress=50)
                )
                await db.commit()

                # 3. 划分训练/验证集
                test_size = config.get("test_size", 0.2)
                random_state = config.get("random_state", 42)
                X_train, X_val, y_train, y_val = train_test_split(
                    X_scaled, y, test_size=test_size, random_state=random_state, stratify=y
                )

                # 4. 训练 SVM
                model = SVC(
                    kernel=config.get("kernel", "rbf"),
                    C=config.get("C", 1.0),
                    gamma=config.get("gamma", "scale"),
                    class_weight=config.get("class_weight", "balanced"),
                    probability=True,
                    random_state=random_state,
                )
                model.fit(X_train, y_train)

                await db.execute(
                    update(TrainingJob)
                    .where(TrainingJob.job_id == job_id)
                    .values(progress=70)
                )
                await db.commit()

                # 5. 评估
                y_pred = model.predict(X_val)
                y_proba = model.predict_proba(X_val)

                train_acc = accuracy_score(y_train, model.predict(X_train))
                val_acc = accuracy_score(y_val, y_pred)
                report = classification_report(y_val, y_pred, output_dict=True, target_names=["no_queen", "normal"])
                cm = confusion_matrix(y_val, y_pred).tolist()

                await db.execute(
                    update(TrainingJob)
                    .where(TrainingJob.job_id == job_id)
                    .values(progress=85)
                )
                await db.commit()

                # 6. 打包并签名模型
                metadata = {
                    "model_type": config.get("model_type", "svm"),
                    "training_samples": len(X),
                    "positive_samples": len(X_pos),
                    "negative_samples": len(X_neg),
                    "config": config,
                }

                model_bytes, scaler_bytes, model_sha256, signature = model_manager.package_model(
                    model, scaler, metadata
                )

                # 7. 生成版本号
                version = f"v{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

                # 8. 上传到 MinIO
                model_key, scaler_key = model_manager.upload_model(version, model_bytes, scaler_bytes, signature)

                # 9. 创建模型版本记录
                model_version = ModelVersion(
                    version=version,
                    model_type=ModelType(config.get("model_type", "svm")),
                    minio_object_key=model_key,
                    scaler_object_key=scaler_key,
                    signature=signature,
                    file_size=len(model_bytes),
                    sha256_hash=model_sha256,
                    training_dataset_size=len(X),
                    training_accuracy=float(train_acc),
                    validation_accuracy=float(val_acc),
                    metrics={
                        "precision": report.get("no_queen", {}).get("precision"),
                        "recall": report.get("no_queen", {}).get("recall"),
                        "f1": report.get("no_queen", {}).get("f1-score"),
                        "confusion_matrix": cm,
                        "classification_report": report,
                    },
                    status=ModelStatus.VALIDATING,
                    trained_at=datetime.utcnow(),
                )

                db.add(model_version)
                await db.flush()
                model_version_id = model_version.id

                # 关联训练任务
                await db.execute(
                    update(TrainingJob)
                    .where(TrainingJob.job_id == job_id)
                    .values(
                        model_version_id=model_version_id,
                        progress=95,
                    )
                )
                await db.commit()

                logger.info(f"Training completed: version={version}, val_acc={val_acc:.4f}")

                # 10. 自动部署如果验证通过
                if val_acc >= 0.85:
                    await deploy_model_task(model_version_id)

                return {
                    "version": version,
                    "model_version_id": model_version_id,
                    "train_accuracy": float(train_acc),
                    "val_accuracy": float(val_acc),
                    "metrics": report,
                }

            except Exception as e:
                logger.exception(f"Training failed: {e}")
                await db.execute(
                    update(TrainingJob)
                    .where(TrainingJob.job_id == job_id)
                    .values(
                        status=TrainingJobStatus.FAILED,
                        error_message=str(e),
                        completed_at=datetime.utcnow(),
                    )
                )
                await db.commit()
                raise

    return asyncio.run(_train())


@shared_task(bind=True)
def deploy_model_task(self, model_version_id: int):
    """部署模型为生产版本"""
    logger.info(f"Deploying model version {model_version_id}")

    async def _deploy():
        async with async_session_maker() as db:
            # 获取模型版本
            result = await db.execute(select(ModelVersion).where(ModelVersion.id == model_version_id))
            model_version = result.scalar_one_or_none()

            if not model_version:
                raise ValueError(f"Model version {model_version_id} not found")

            # 验证模型
            model_manager.download_model(model_version.version)

            # 取消当前生产模型
            await db.execute(
                update(ModelVersion)
                .where(ModelVersion.is_active == True)
                .values(is_active=False, status=ModelStatus.DEPRECATED)
            )

            # 设置新模型为生产
            await db.execute(
                update(ModelVersion)
                .where(ModelVersion.id == model_version_id)
                .values(
                    is_active=True,
                    status=ModelStatus.ACTIVE,
                    deployed_at=datetime.utcnow(),
                )
            )

            await db.commit()
            logger.info(f"Model {model_version.version} deployed as production")

            return {"status": "deployed", "version": model_version.version}

    return asyncio.run(_deploy())


@shared_task
def retrain_scheduled_task():
    """定时重训练任务 (由 Celery Beat 触发)"""
    logger.info("Checking for scheduled retraining...")
    pass


# ============================================================================
# 按设备触发训练 (MinIO 交换站): 设备正样本 + 服务器预置负样本 → SVM+Scaler
# ============================================================================
async def _save(Session, job_id: str, **values):
    async with Session() as db:
        await db.execute(update(TrainingJob).where(TrainingJob.job_id == job_id).values(**values))
        await db.commit()


def _load_npy(key: str) -> np.ndarray:
    data = minio_client.download_file(key)
    arr = np.load(io.BytesIO(data)).astype(np.float64).reshape(-1)
    if arr.shape[0] != FEATURE_DIM:
        raise ValueError(f"特征维度不符: {key} dim={arr.shape[0]} 期望 {FEATURE_DIM}")
    return arr


async def _check_and_train(device_name: str):
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    job_id = None
    try:
        async with Session() as db:
            job = (
                await db.execute(
                    select(TrainingJob)
                    .where(TrainingJob.device_name == device_name)
                    .order_by(TrainingJob.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if not job or job.status not in (
                TrainingJobStatus.WAITING,
                TrainingJobStatus.PENDING,
                TrainingJobStatus.RUNNING,
            ):
                logger.info(f"[train] {device_name}: no active job")
                return "no-active-job"
            job_id = job.job_id
            requested_at = job.requested_at or job.created_at

        after_aware = requested_at.replace(tzinfo=timezone.utc)

        objs = minio_client.list_objects_meta(f"features/{device_name}/")
        after = [o for o in objs if o.get("last_modified") and o["last_modified"] > after_aware]
        count = len(after)

        if count < REQUIRED_POSITIVE:
            if datetime.utcnow() - requested_at > timedelta(hours=WAIT_TIMEOUT_HOURS):
                await _save(Session, job_id, status=TrainingJobStatus.CANCELLED,
                            feature_count=count, error_message="超时：24h 内正样本不足 600",
                            completed_at=datetime.utcnow())
                logger.warning(f"[train] {device_name}: cancelled (timeout) {count}/{REQUIRED_POSITIVE}")
                return "cancelled"
            await _save(Session, job_id, status=TrainingJobStatus.WAITING, feature_count=count)
            logger.info(f"[train] {device_name}: waiting {count}/{REQUIRED_POSITIVE}")
            return f"waiting {count}/{REQUIRED_POSITIVE}"

        await _save(Session, job_id, status=TrainingJobStatus.RUNNING, feature_count=count)

        # 正样本(正常类, label=0)：取 requested_at 之后最早的 600 条
        selected = sorted(after, key=lambda o: o["last_modified"])[:REQUIRED_POSITIVE]
        X_pos = np.vstack([_load_npy(o["name"]) for o in selected])

        # 负样本(失王类, label=1)：服务器预置
        neg_objs = sorted(minio_client.list_objects_meta(NEG_PREFIX), key=lambda o: o["name"])[:REQUIRED_NEGATIVE]
        if len(neg_objs) < REQUIRED_NEGATIVE:
            raise RuntimeError(f"预置负样本不足: {len(neg_objs)}/{REQUIRED_NEGATIVE} (前缀 {NEG_PREFIX})")
        X_neg = np.vstack([_load_npy(o["name"]) for o in neg_objs])

        X = np.vstack([X_pos, X_neg])
        y = np.hstack([np.zeros(len(X_pos), dtype=int), np.ones(len(X_neg), dtype=int)])

        scaler = StandardScaler().fit(X)
        X_scaled = scaler.transform(X)
        model = SVC(kernel="rbf", C=1.0, gamma="scale", class_weight="balanced",
                    probability=True, random_state=42).fit(X_scaled, y)

        m_buf = io.BytesIO(); joblib.dump(model, m_buf, compress=3); model_bytes = m_buf.getvalue()
        s_buf = io.BytesIO(); joblib.dump(scaler, s_buf, compress=3); scaler_bytes = s_buf.getvalue()

        model_sha = hashlib.sha256(model_bytes).hexdigest()
        scaler_sha = hashlib.sha256(scaler_bytes).hexdigest()
        signature = model_manager.signer.sign((model_sha + scaler_sha).encode())

        svm_key = f"models/{device_name}/svm.pkl"
        scaler_key = f"models/{device_name}/scaler.pkl"
        minio_client.upload_file(svm_key, model_bytes, content_type="application/octet-stream",
                                 metadata={"signature": signature, "device": device_name})
        minio_client.upload_file(scaler_key, scaler_bytes, content_type="application/octet-stream",
                                 metadata={"device": device_name})

        version = f"v{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        async with Session() as db:
            mv = ModelVersion(
                version=version, model_type=ModelType.SVM,
                minio_object_key=svm_key, scaler_object_key=scaler_key,
                signature=signature, file_size=len(model_bytes), sha256_hash=model_sha,
                training_dataset_size=len(X),
                status=ModelStatus.VALIDATING, is_active=False, trained_at=datetime.utcnow(),
            )
            db.add(mv)
            await db.commit()
            await db.refresh(mv)
            await db.execute(update(TrainingJob).where(TrainingJob.job_id == job_id).values(
                status=TrainingJobStatus.COMPLETED, progress=100, feature_count=count,
                model_version_id=mv.id, completed_at=datetime.utcnow(),
            ))
            await db.commit()

        logger.info(f"[train] {device_name}: completed version={version} samples={len(X)}")
        return {"status": "completed", "version": version, "samples": int(len(X))}

    except Exception as e:  # noqa: BLE001
        logger.exception(f"[train] {device_name} failed")
        if job_id:
            try:
                await _save(Session, job_id, status=TrainingJobStatus.FAILED,
                            error_message=str(e), completed_at=datetime.utcnow())
            except Exception:
                pass
        raise
    finally:
        await engine.dispose()


@shared_task(bind=True, max_retries=0)
def check_and_train_device(self, device_name: str):
    return asyncio.run(_check_and_train(device_name))


@shared_task
def scan_waiting_model_updates():
    """beat 定时扫描：把处于 waiting 的设备重新入队检查。"""
    async def _scan():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as db:
                rows = (
                    await db.execute(
                        select(TrainingJob.device_name).where(TrainingJob.status == TrainingJobStatus.WAITING)
                    )
                ).scalars().all()
            devices = sorted({d for d in rows if d})
            for d in devices:
                check_and_train_device.delay(d)
            logger.info(f"[train] scan waiting: {devices}")
            return devices
        finally:
            await engine.dispose()
    return asyncio.run(_scan())