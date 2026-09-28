from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
import structlog
import time
import uuid

from app.core.config import get_settings
from app.core.database import init_db, close_db, engine
from app.api.v1 import devices, models, health
from app.tasks.celery_app import celery_app

settings = get_settings()

# 结构化日志配置
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()

# Prometheus 指标
REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)
ACTIVE_REQUESTS = Gauge("http_requests_active", "Active HTTP requests", ["method", "endpoint"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    logger.info("Starting Bee Monitor API", version=settings.APP_VERSION)
    await init_db()
    yield
    # 关闭
    logger.info("Shutting down Bee Monitor API")
    await close_db()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="智能蜂箱音频监测系统 - 云端 API",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 请求中间件：日志、指标、Trace ID
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
    start_time = time.time()

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        trace_id=trace_id,
        method=request.method,
        path=request.url.path,
        client_ip=request.client.host if request.client else None,
    )

    ACTIVE_REQUESTS.labels(method=request.method, endpoint=request.url.path).inc()

    try:
        response = await call_next(request)
        duration = time.time() - start_time

        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code,
        ).inc()
        REQUEST_LATENCY.labels(
            method=request.method,
            endpoint=request.url.path,
        ).observe(duration)

        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Response-Time"] = f"{duration:.4f}"

        logger.info(
            "HTTP request completed",
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        return response
    except Exception as e:
        duration = time.time() - start_time
        REQUEST_COUNT.labels(
            method=request.method,
            endpoint=request.url.path,
            status=500,
        ).inc()
        logger.exception("HTTP request failed", duration_ms=round(duration * 1000, 2))
        raise
    finally:
        ACTIVE_REQUESTS.labels(method=request.method, endpoint=request.url.path).dec()
        structlog.contextvars.clear_contextvars()


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex)
    logger.exception("Unhandled exception", trace_id=trace_id, path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Internal server error",
            "code": "INTERNAL_ERROR",
            "trace_id": trace_id,
        },
    )


# 注册路由
app.include_router(health.router, prefix="")
app.include_router(devices.router, prefix="/api/v1")
app.include_router(models.router, prefix="/api/v1")


# Prometheus metrics endpoint (单独路由，不经过中间件记录)
@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# 根路径
@app.get("/")
async def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/health",
        "ready": "/ready",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        workers=1,
        reload=settings.DEBUG,
    )