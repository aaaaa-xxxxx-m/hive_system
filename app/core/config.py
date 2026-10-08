from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional
import base64


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    # App
    APP_NAME: str = "Bee Monitor API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    TZ: str = "Asia/Shanghai"

    # Database
    MYSQL_HOST: str = "mysql"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str
    MYSQL_PASSWORD: str
    MYSQL_DATABASE: str
    MYSQL_ROOT_PASSWORD: str

    @property
    def DATABASE_URL(self) -> str:
        return f"mysql+asyncmy://{self.MYSQL_USER}:{self.MYSQL_PASSWORD}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}?charset=utf8mb4"

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str
    REDIS_DB: int = 0

    @property
    def REDIS_URL(self) -> str:
        return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def REDIS_URL_BEAT(self) -> str:
        return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/1"

    # MinIO
    MINIO_ENDPOINT: str = "minio:9000"
    MINIO_ACCESS_KEY: str
    MINIO_SECRET_KEY: str
    MINIO_BUCKET: str = "audio"
    MINIO_SECURE: bool = False
    # 对外预签名地址 (设备直传用); 为空时回退到 MINIO_ENDPOINT
    MINIO_PUBLIC_ENDPOINT: str = ""
    MINIO_PUBLIC_SECURE: bool = False
    # 显式指定区域, 避免 SDK 发起 GetBucketLocation 网络探测
    # (预签名时若探测公网地址会 hairpin 阻塞)
    MINIO_REGION: str = "us-east-1"

    # Auth / JWT
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    API_KEY_SALT: str
    # 单账号单会话：空闲多久（分钟）自动释放登录态
    SESSION_IDLE_MINUTES: int = 30
    # 登录时若已存在会话但「超过该秒数无活动」，视为陈旧并可被顶替（避免死会话挡住重登）
    SESSION_TAKEOVER_GRACE_SECONDS: int = 90

    # 初始管理员 (accounts 表为空时自动创建)
    BOOTSTRAP_ADMIN_USER: str = "admin"
    BOOTSTRAP_ADMIN_PASSWORD: str = "bee@admin"

    # Model Signing (Ed25519)
    MODEL_SIGNING_PRIVATE_KEY_B64: str = Field(alias="MODEL_SIGNING_PRIVATE_KEY")
    MODEL_SIGNING_PUBLIC_KEY_B64: str = Field(alias="MODEL_SIGNING_PUBLIC_KEY")

    @property
    def MODEL_SIGNING_PRIVATE_KEY(self) -> bytes:
        return base64.b64decode(self.MODEL_SIGNING_PRIVATE_KEY_B64)

    @property
    def MODEL_SIGNING_PUBLIC_KEY(self) -> bytes:
        return base64.b64decode(self.MODEL_SIGNING_PUBLIC_KEY_B64)

    # Monitoring
    PROMETHEUS_METRICS_PATH: str = "/metrics"

    # CORS
    CORS_ORIGINS: list[str] = ["*"]

    # Pagination
    DEFAULT_PAGE_SIZE: int = 20
    MAX_PAGE_SIZE: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()