from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum
import uuid


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


class InferenceLabel(str, Enum):
    NO_QUEEN = "no_queen"
    NORMAL = "normal"
    UNCERTAIN = "uncertain"


class ModelType(str, Enum):
    SVM = "svm"
    CNN = "cnn"
    TRANSFORMER = "transformer"


class ModelStatus(str, Enum):
    TRAINING = "training"
    VALIDATING = "validating"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    FAILED = "failed"


class TrainingJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DownloadJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ========== Device Schemas ==========
class DeviceBase(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=64, pattern=r"^[\w\u4e00-\u9fa5-]+$")
    name: Optional[str] = Field(None, max_length=128)
    location_lat: Optional[float] = Field(None, ge=-90, le=90)
    location_lng: Optional[float] = Field(None, ge=-180, le=180)
    location_desc: Optional[str] = Field(None, max_length=255)


class DeviceCreate(DeviceBase):
    api_key: Optional[str] = Field(None, min_length=32, max_length=128, description="设备 API Key (可选; 缺省自动生成, 仅创建时返回)")
    device_fingerprint: Optional[str] = Field(None, max_length=128)


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    location_lat: Optional[float] = Field(None, ge=-90, le=90)
    location_lng: Optional[float] = Field(None, ge=-180, le=180)
    location_desc: Optional[str] = Field(None, max_length=255)
    target_firmware_version: Optional[str] = Field(None, max_length=32)
    status: Optional[DeviceStatus] = None


class DeviceResponse(DeviceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_fingerprint: Optional[str]
    firmware_version: Optional[str]
    target_firmware_version: Optional[str]
    status: DeviceStatus
    last_seen_at: Optional[datetime]
    registered_at: datetime
    updated_at: datetime


class DeviceWithDetails(DeviceResponse):
    temperature: Optional[float]
    humidity: Optional[float]
    battery_voltage: Optional[float]
    signal_strength: Optional[int]
    sensor_recorded_at: Optional[datetime]
    probability_no_queen: Optional[float]
    probability_normal: Optional[float]
    inference_label: Optional[InferenceLabel]
    confidence: Optional[float]
    inference_recorded_at: Optional[datetime]
    model_version: Optional[str]


class DeviceSummary(BaseModel):
    """首页聚合列表条目 (旧前端 get_name 接口适配)"""
    device_name: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None


class DeviceDownloadCatalog(BaseModel):
    """数据下载设备目录: 每设备的记录条数"""
    device_name: str
    record_count: int = 0


# ========== Device Record Schema (方案 C: 音频与指标合并后的每事件记录) ==========
class DeviceRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_name: str
    temperature: Optional[float]
    humidity: Optional[float]
    battery_voltage: Optional[float]
    signal_strength: Optional[int]
    recorded_at: datetime
    received_at: datetime


# ========== Inference Schemas ==========
class InferenceResultCreate(BaseModel):
    audio_object_key: str = Field(..., max_length=256)
    feature_object_key: Optional[str] = Field(None, max_length=256)
    model_version: str = Field(..., max_length=64)
    probability_no_queen: float = Field(..., ge=0, le=1)
    probability_normal: float = Field(..., ge=0, le=1)
    label: InferenceLabel
    confidence: float = Field(..., ge=0, le=1)
    inference_time_ms: Optional[int] = Field(None, ge=0)
    # 环境指标 (随音频一并上报, 方案 C)
    temperature: Optional[float] = Field(None, ge=-50, le=100)
    humidity: Optional[float] = Field(None, ge=0, le=100)
    battery_voltage: Optional[float] = Field(None, ge=0, le=5)
    signal_strength: Optional[int] = Field(None, ge=-120, le=0)
    recorded_at: Optional[datetime] = None


class InferenceResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: int
    device_name: str
    audio_object_key: str
    feature_object_key: Optional[str]
    model_version: str
    probability_no_queen: float
    probability_normal: float
    label: InferenceLabel
    confidence: float
    inference_time_ms: Optional[int]
    temperature: Optional[float]
    humidity: Optional[float]
    battery_voltage: Optional[float]
    signal_strength: Optional[int]
    recorded_at: datetime
    received_at: datetime
    processed_at: datetime


# ========== Model Schemas ==========
class ModelMetrics(BaseModel):
    precision: Optional[Dict[str, float]] = None
    recall: Optional[Dict[str, float]] = None
    f1: Optional[Dict[str, float]] = None
    confusion_matrix: Optional[List[List[int]]] = None


class ModelVersionCreate(BaseModel):
    version: str = Field(..., max_length=64, pattern=r"^v?\d+\.\d+\.\d+(-.+)?$")
    model_type: ModelType = ModelType.SVM
    minio_object_key: str = Field(..., max_length=256)
    scaler_object_key: Optional[str] = Field(None, max_length=256)
    file_size: int = Field(..., gt=0)
    sha256_hash: str = Field(..., min_length=64, max_length=64)
    training_dataset_size: Optional[int] = Field(None, ge=0)
    training_accuracy: Optional[float] = Field(None, ge=0, le=1)
    validation_accuracy: Optional[float] = Field(None, ge=0, le=1)
    metrics: Optional[ModelMetrics] = None


class ModelVersionUpdate(BaseModel):
    status: Optional[ModelStatus] = None
    is_active: Optional[bool] = None
    deployed_at: Optional[datetime] = None
    training_accuracy: Optional[float] = Field(None, ge=0, le=1)
    validation_accuracy: Optional[float] = Field(None, ge=0, le=1)
    metrics: Optional[ModelMetrics] = None


class ModelVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: str
    model_type: ModelType
    minio_object_key: str
    scaler_object_key: Optional[str]
    signature: str
    file_size: int
    sha256_hash: str
    training_dataset_size: Optional[int]
    training_accuracy: Optional[float]
    validation_accuracy: Optional[float]
    metrics: Optional[Dict[str, Any]]
    status: ModelStatus
    is_active: bool
    trained_at: Optional[datetime]
    deployed_at: Optional[datetime]
    created_at: datetime


class ModelDeployRequest(BaseModel):
    version: str = Field(..., max_length=64)


class ModelSignRequest(BaseModel):
    version: str
    model_data_b64: str
    scaler_data_b64: Optional[str] = None


# ========== Training Job Schemas ==========
class TrainingConfig(BaseModel):
    model_type: ModelType = ModelType.SVM
    kernel: str = "rbf"
    C: float = 1.0
    gamma: str = "scale"
    class_weight: str = "balanced"
    test_size: float = Field(0.2, ge=0.1, le=0.5)
    random_state: int = 42
    feature_params: Dict[str, Any] = {}


class TrainingJobCreate(BaseModel):
    config: TrainingConfig
    positive_keys: List[str] = Field(..., min_length=1, description="正样本 MinIO keys")
    negative_keys: List[str] = Field(..., min_length=1, description="负样本 MinIO keys")


class TrainingJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: uuid.UUID
    model_version_id: Optional[int]
    status: TrainingJobStatus
    progress: int
    config: Optional[Dict[str, Any]]
    logs: Optional[str]
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime


# ========== Pagination ==========
class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)


class PageResponse(BaseModel):
    items: List[Any]
    total: int
    page: int
    size: int
    pages: int

    @classmethod
    def create(cls, items: List[Any], total: int, page: int, size: int) -> "PageResponse":
        return cls(
            items=items,
            total=total,
            page=page,
            size=size,
            pages=(total + size - 1) // size
        )


# ========== Download Schemas ==========
class DownloadJobCreate(BaseModel):
    device_name: str = Field(..., min_length=1, max_length=64)
    limit: int = Field(..., ge=1, le=10000, description="打包最近 N 条音频记录")


class DownloadJobResponse(BaseModel):
    job_id: str
    status: DownloadJobStatus
    progress: int
    download_url: Optional[str] = None
    error: Optional[str] = None


# ========== Common Response ==========
class APIResponse(BaseModel):
    success: bool = True
    message: str = "OK"
    data: Optional[Any] = None


class ErrorResponse(BaseModel):
    success: bool = False
    message: str
    code: str
    details: Optional[Dict[str, Any]] = None


# ========== Health / Ready ==========
class HealthResponse(BaseModel):
    status: str = "healthy"
    version: str
    timestamp: datetime
    checks: Dict[str, str] = {}


class ReadyResponse(BaseModel):
    ready: bool
    checks: Dict[str, bool]