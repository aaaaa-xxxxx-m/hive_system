"""SQLAlchemy ORM 模型与枚举定义。

表结构与 config/mysql/init.sql 保持一致。
注意：所有 Enum 列使用 values_callable 让数据库保存成员 value（如 "online"），
而不是默认的成员 name（如 "ONLINE"），以匹配 init.sql 中的 ENUM 定义。
"""

import enum
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ============================================================
# 枚举
# ============================================================
class DeviceStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


class InferenceLabel(str, enum.Enum):
    NO_QUEEN = "no_queen"
    NORMAL = "normal"
    UNCERTAIN = "uncertain"


class ModelType(str, enum.Enum):
    SVM = "svm"
    CNN = "cnn"
    TRANSFORMER = "transformer"


class ModelStatus(str, enum.Enum):
    TRAINING = "training"
    VALIDATING = "validating"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    FAILED = "failed"


class TrainingJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RawFeatureLabel(str, enum.Enum):
    NO_QUEEN = "no_queen"
    NORMAL = "normal"
    UNKNOWN = "unknown"


def _enum(enum_cls: type, name: str) -> SAEnum:
    """构造以枚举 value 落库的 SQLAlchemy Enum 列。"""
    return SAEnum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


# ============================================================
# 设备注册表
# ============================================================
class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_fingerprint: Mapped[Optional[str]] = mapped_column(String(128))
    name: Mapped[Optional[str]] = mapped_column(String(128))
    location_lat: Mapped[Optional[float]] = mapped_column(Numeric(10, 7))
    location_lng: Mapped[Optional[float]] = mapped_column(Numeric(10, 7))
    location_desc: Mapped[Optional[str]] = mapped_column(String(255))
    firmware_version: Mapped[Optional[str]] = mapped_column(String(32))
    target_firmware_version: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[DeviceStatus] = mapped_column(
        _enum(DeviceStatus, "device_status"), default=DeviceStatus.OFFLINE, nullable=False
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ============================================================
# 推理结果 (按月分区) —— 同时承载环境指标 (方案 C: 合并 sensor_data)
# ============================================================
class InferenceResult(Base):
    __tablename__ = "inference_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(64), nullable=False)
    audio_object_key: Mapped[str] = mapped_column(String(256), nullable=False)
    feature_object_key: Mapped[Optional[str]] = mapped_column(String(256))
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    probability_no_queen: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    probability_normal: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    label: Mapped[InferenceLabel] = mapped_column(
        _enum(InferenceLabel, "inference_label"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    inference_time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    # 环境指标 (随音频一并上报)
    temperature: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    humidity: Mapped[Optional[float]] = mapped_column(Numeric(5, 2))
    battery_voltage: Mapped[Optional[float]] = mapped_column(Numeric(5, 3))
    signal_strength: Mapped[Optional[int]] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


# ============================================================
# 模型版本管理
# ============================================================
class ModelVersion(Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    model_type: Mapped[ModelType] = mapped_column(
        _enum(ModelType, "model_type"), default=ModelType.SVM, nullable=False
    )
    minio_object_key: Mapped[str] = mapped_column(String(256), nullable=False)
    scaler_object_key: Mapped[Optional[str]] = mapped_column(String(256))
    signature: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    training_dataset_size: Mapped[Optional[int]] = mapped_column(Integer)
    training_accuracy: Mapped[Optional[float]] = mapped_column(Numeric(5, 4))
    validation_accuracy: Mapped[Optional[float]] = mapped_column(Numeric(5, 4))
    metrics: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)
    status: Mapped[ModelStatus] = mapped_column(
        _enum(ModelStatus, "model_status"), default=ModelStatus.TRAINING, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trained_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    deployed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    training_jobs: Mapped[List["TrainingJob"]] = relationship(back_populates="model_version")


# ============================================================
# 训练任务
# ============================================================
class TrainingJob(Base):
    __tablename__ = "training_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    model_version_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    status: Mapped[TrainingJobStatus] = mapped_column(
        _enum(TrainingJobStatus, "training_job_status"),
        default=TrainingJobStatus.PENDING,
        nullable=False,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    config: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)
    logs: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    model_version: Mapped[Optional["ModelVersion"]] = relationship(back_populates="training_jobs")


# ============================================================
# 数据下载任务
# ============================================================
class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    device_name: Mapped[str] = mapped_column(String(64), nullable=False)
    data_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DownloadJobStatus] = mapped_column(
        _enum(DownloadJobStatus, "download_job_status"),
        default=DownloadJobStatus.PENDING,
        nullable=False,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    object_key: Mapped[Optional[str]] = mapped_column(String(256))
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


# ============================================================
# 原始特征数据 (30 天保留)
# ============================================================
class RawFeature(Base):
    __tablename__ = "raw_features"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    device_name: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_vector: Mapped[Any] = mapped_column(JSON, nullable=False)
    label: Mapped[RawFeatureLabel] = mapped_column(
        _enum(RawFeatureLabel, "raw_feature_label"),
        default=RawFeatureLabel.UNKNOWN,
        nullable=False,
    )
    minio_object_key: Mapped[Optional[str]] = mapped_column(String(256))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


# ============================================================
# 审计日志
# ============================================================
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    device_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(64))
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    request_ip: Mapped[Optional[str]] = mapped_column(String(45))
    request_ua: Mapped[Optional[str]] = mapped_column(Text)
    request_body: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSON)
    response_code: Mapped[Optional[int]] = mapped_column(Integer)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


__all__ = [
    "DeviceStatus",
    "InferenceLabel",
    "ModelType",
    "ModelStatus",
    "TrainingJobStatus",
    "DownloadJobStatus",
    "RawFeatureLabel",
    "Device",
    "InferenceResult",
    "ModelVersion",
    "TrainingJob",
    "DownloadJob",
    "RawFeature",
    "AuditLog",
]
