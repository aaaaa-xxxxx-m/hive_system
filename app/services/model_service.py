from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
import base64
import joblib
import io
import hashlib
import logging
from typing import Tuple, Optional, Dict, Any
from app.core.config import get_settings
from app.services.minio_client import get_minio_client

logger = logging.getLogger(__name__)
settings = get_settings()


class ModelSigner:
    def __init__(self):
        self.private_key = Ed25519PrivateKey.from_private_bytes(settings.MODEL_SIGNING_PRIVATE_KEY)
        self.public_key = Ed25519PublicKey.from_public_bytes(settings.MODEL_SIGNING_PUBLIC_KEY)

    def sign(self, data: bytes) -> str:
        """签名数据，返回 Base64 编码的签名"""
        signature = self.private_key.sign(data)
        return base64.b64encode(signature).decode()

    def verify(self, data: bytes, signature_b64: str) -> bool:
        """验证签名"""
        try:
            signature = base64.b64decode(signature_b64)
            self.public_key.verify(signature, data)
            return True
        except Exception:
            return False

    def get_public_key_pem(self) -> str:
        """获取 PEM 格式公钥"""
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def get_private_key_pem(self) -> str:
        """获取 PEM 格式私钥 (仅 Celery Worker 使用)"""
        return self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ).decode()


class ModelManager:
    def __init__(self):
        self.signer = ModelSigner()
        self.minio = get_minio_client()

    def package_model(
        self,
        model_obj: Any,
        scaler_obj: Optional[Any] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bytes, bytes, str, str]:
        """
        打包模型并签名
        Returns: (model_bytes, scaler_bytes, model_sha256, signature)
        """
        model_buffer = io.BytesIO()
        joblib.dump(model_obj, model_buffer, compress=3)
        model_bytes = model_buffer.getvalue()

        scaler_bytes = b""
        if scaler_obj is not None:
            scaler_buffer = io.BytesIO()
            joblib.dump(scaler_obj, scaler_buffer, compress=3)
            scaler_bytes = scaler_buffer.getvalue()

        # 计算模型文件 SHA256
        model_sha256 = hashlib.sha256(model_bytes).hexdigest()

        # 签名数据包含: model_sha256 + scaler_sha256 + metadata
        sign_data = model_sha256.encode()
        if scaler_bytes:
            scaler_sha256 = hashlib.sha256(scaler_bytes).hexdigest()
            sign_data += scaler_sha256.encode()
        if metadata:
            import json
            sign_data += json.dumps(metadata, sort_keys=True).encode()

        signature = self.signer.sign(sign_data)

        return model_bytes, scaler_bytes, model_sha256, signature

    def verify_model_package(
        self,
        model_bytes: bytes,
        scaler_bytes: Optional[bytes],
        signature: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """验证模型包完整性"""
        model_sha256 = hashlib.sha256(model_bytes).hexdigest()

        sign_data = model_sha256.encode()
        if scaler_bytes:
            scaler_sha256 = hashlib.sha256(scaler_bytes).hexdigest()
            sign_data += scaler_sha256.encode()
        if metadata:
            import json
            sign_data += json.dumps(metadata, sort_keys=True).encode()

        return self.signer.verify(sign_data, signature)

    def upload_model(
        self,
        version: str,
        model_bytes: bytes,
        scaler_bytes: Optional[bytes],
        signature: str,
    ) -> Tuple[str, Optional[str]]:
        """上传模型到 MinIO，返回 (model_key, scaler_key)"""
        model_key = f"models/{version}/model.pkl"
        self.minio.upload_file(
            model_key,
            model_bytes,
            content_type="application/octet-stream",
            metadata={"signature": signature, "version": version},
        )

        scaler_key = None
        if scaler_bytes:
            scaler_key = f"models/{version}/scaler.pkl"
            self.minio.upload_file(
                scaler_key,
                scaler_bytes,
                content_type="application/octet-stream",
                metadata={"version": version},
            )

        return model_key, scaler_key

    def download_model(self, version: str) -> Tuple[bytes, Optional[bytes], str]:
        """下载并验证模型"""
        model_key = f"models/{version}/model.pkl"
        scaler_key = f"models/{version}/scaler.pkl"

        model_bytes = self.minio.download_file(model_key)
        model_info = self.minio.get_file_info(model_key)
        signature = model_info.get("metadata", {}).get("signature", "") if model_info else ""

        scaler_bytes = None
        if self.minio.file_exists(scaler_key):
            scaler_bytes = self.minio.download_file(scaler_key)

        # 验证签名
        if not self.verify_model_package(model_bytes, scaler_bytes, signature):
            raise ValueError(f"Model signature verification failed for version {version}")

        return model_bytes, scaler_bytes, signature

    def load_model(self, version: str) -> Tuple[Any, Optional[Any]]:
        """加载模型对象"""
        model_bytes, scaler_bytes, _ = self.download_model(version)
        model_obj = joblib.load(io.BytesIO(model_bytes))
        scaler_obj = joblib.load(io.BytesIO(scaler_bytes)) if scaler_bytes else None
        return model_obj, scaler_obj

    def get_public_key_pem(self) -> str:
        return self.signer.get_public_key_pem()


model_manager = ModelManager()


def get_model_manager() -> ModelManager:
    return model_manager