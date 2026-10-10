from celery import Celery
from celery.schedules import crontab
from app.core.config import get_settings
import logging

logger = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery(
    "bee_monitor",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.tasks.training_tasks",
        "app.tasks.inference_tasks",
        "app.tasks.cleanup_tasks",
        "app.tasks.download_tasks",
        "app.tasks.export_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    result_expires=86400,
    result_compression="gzip",
    task_routes={
        "app.tasks.training_tasks.*": {"queue": "training"},
        "app.tasks.inference_tasks.*": {"queue": "inference"},
        "app.tasks.cleanup_tasks.*": {"queue": "cleanup"},
        # 导出任务走 inference 队列 (worker 已消费该队列, 免改 compose)
        "app.tasks.download_tasks.*": {"queue": "inference"},
        "app.tasks.export_tasks.*": {"queue": "inference"},
    },
    beat_schedule={
        "cleanup-old-raw-features": {
            "task": "app.tasks.cleanup_tasks.cleanup_old_raw_features",
            "schedule": crontab(hour=3, minute=0),
        },
        "cleanup-old-audit-logs": {
            "task": "app.tasks.cleanup_tasks.cleanup_old_audit_logs",
            "schedule": crontab(hour=3, minute=30),
        },
        "check-device-heartbeat": {
            "task": "app.tasks.cleanup_tasks.check_device_heartbeat",
            "schedule": crontab(minute="*/5"),
        },
        # 采集进度近实时刷新：设备采集周期约 30s，这里每 30s 重扫一次 feature_count，
        # 使前端「等待数据 X/600」与实际采集速度同步（原为 */20 分钟，明显滞后）。
        "scan-waiting-model-updates": {
            "task": "app.tasks.training_tasks.scan_waiting_model_updates",
            "schedule": 30.0,
        },
    },
)

celery_app.autodiscover_tasks()


@celery_app.task(bind=True, ignore_result=True)
def debug_task(self):
    logger.info(f"Request: {self.request!r}")