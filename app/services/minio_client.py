from minio import Minio
from minio.error import S3Error
from app.core.config import get_settings
from typing import Optional, BinaryIO, List
import io
import hashlib
import logging

logger = logging.getLogger(__name__)
settings = get_settings()


class MinIOClient:
    def __init__(self):
        self.client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        # 预签名 URL 必须按设备实际访问的地址签名 (公网 IP:端口)
        self.public_endpoint = settings.MINIO_PUBLIC_ENDPOINT or settings.MINIO_ENDPOINT
        self.public_client = Minio(
            self.public_endpoint,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_PUBLIC_SECURE,
        )
        self.bucket = settings.MINIO_BUCKET
        self._ensure_bucket()

    def _ensure_bucket(self):
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                logger.info(f"Created bucket: {self.bucket}")
        except S3Error as e:
            logger.error(f"Failed to ensure bucket: {e}")
            raise

    def upload_file(
        self,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> str:
        """上传字节数据，返回 object_key"""
        try:
            data_stream = io.BytesIO(data)
            self.client.put_object(
                self.bucket,
                object_key,
                data_stream,
                length=len(data),
                content_type=content_type,
                metadata=metadata or {},
            )
            logger.debug(f"Uploaded {object_key} ({len(data)} bytes)")
            return object_key
        except S3Error as e:
            logger.error(f"Upload failed: {e}")
            raise

    def upload_fileobj(
        self,
        object_key: str,
        fileobj: BinaryIO,
        length: int,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict] = None,
    ) -> str:
        """上传文件对象"""
        try:
            self.client.put_object(
                self.bucket,
                object_key,
                fileobj,
                length=length,
                content_type=content_type,
                metadata=metadata or {},
            )
            return object_key
        except S3Error as e:
            logger.error(f"Upload fileobj failed: {e}")
            raise

    def download_file(self, object_key: str) -> bytes:
        """下载文件为字节"""
        try:
            response = self.client.get_object(self.bucket, object_key)
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except S3Error as e:
            logger.error(f"Download failed: {e}")
            raise

    def download_to_file(self, object_key: str, file_path: str) -> None:
        """下载文件到本地路径"""
        try:
            self.client.fget_object(self.bucket, object_key, file_path)
        except S3Error as e:
            logger.error(f"Download to file failed: {e}")
            raise

    def delete_file(self, object_key: str) -> bool:
        """删除文件"""
        try:
            self.client.remove_object(self.bucket, object_key)
            return True
        except S3Error as e:
            logger.error(f"Delete failed: {e}")
            return False

    def file_exists(self, object_key: str) -> bool:
        """检查文件是否存在"""
        try:
            self.client.stat_object(self.bucket, object_key)
            return True
        except S3Error:
            return False

    def get_file_info(self, object_key: str) -> Optional[dict]:
        """获取文件元信息"""
        try:
            stat = self.client.stat_object(self.bucket, object_key)
            return {
                "size": stat.size,
                "etag": stat.etag,
                "last_modified": stat.last_modified,
                "content_type": stat.content_type,
                "metadata": stat.metadata,
            }
        except S3Error:
            return None

    def list_files(self, prefix: str = "", recursive: bool = True) -> List[str]:
        """列出文件键"""
        try:
            objects = self.client.list_objects(self.bucket, prefix=prefix, recursive=recursive)
            return [obj.object_name for obj in objects]
        except S3Error as e:
            logger.error(f"List files failed: {e}")
            return []

    def get_presigned_url(
        self,
        object_key: str,
        expires_seconds: int = 3600,
        method: str = "GET",
    ) -> str:
        """生成预签名 URL (按对外地址签名)"""
        from datetime import timedelta
        try:
            return self.public_client.presigned_get_object(self.bucket, object_key, expires=timedelta(seconds=expires_seconds))
        except S3Error as e:
            logger.error(f"Presigned URL failed: {e}")
            raise

    def get_presigned_put_url(
        self,
        object_key: str,
        expires_seconds: int = 3600,
    ) -> str:
        """生成预签名上传 URL (按对外地址签名)"""
        from datetime import timedelta
        try:
            return self.public_client.presigned_put_object(self.bucket, object_key, expires=timedelta(seconds=expires_seconds))
        except S3Error as e:
            logger.error(f"Presigned PUT URL failed: {e}")
            raise

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def generate_object_key(prefix: str, device_name: str, filename: str) -> str:
        from datetime import datetime
        import uuid
        date_str = datetime.utcnow().strftime("%Y/%m/%d")
        unique_id = uuid.uuid4().hex[:8]
        ext = filename.split(".")[-1] if "." in filename else ""
        ext = f".{ext}" if ext else ""
        return f"{prefix}/{date_str}/{device_name}/{unique_id}{ext}"


minio_client = MinIOClient()


def get_minio_client() -> MinIOClient:
    return minio_client