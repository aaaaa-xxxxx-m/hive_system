# 智能蜂箱音频监测系统 · 云端服务器

面向**蜂群失王监测**的端云系统云端部分。边缘节点采集蜂箱音频，提取声学特征并推理
「失王概率」；云端负责**数据接收与存储、前端可视化、模型训练与下发、设备与数据管理**。

系统采用 **Docker Compose 容器化**部署：`Nginx` 反向代理、`FastAPI` 后端、`Celery`
异步任务、`MySQL`、`Redis` 与 `MinIO` 对象存储。音频 / 特征 / 模型等大文件走 MinIO，
温湿度与推理等结构化数据经 HTTP 写入 MySQL，实现计算、存储与异步任务的解耦。

---

## 目录

- [架构总览](#架构总览)
- [功能特性](#功能特性)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [主要接口](#主要接口)
- [模型训练流程](#模型训练流程)
- [部署与 CI/CD](#部署与-cicd)
- [安全](#安全)

---

## 架构总览

```
           浏览器 (Vue 3 单页应用)
                     │  HTTP :80 / HTTPS :443
             ┌───────▼────────┐
             │  bee-nginx     │  反向代理 + 静态前端托管
             └───┬────────┬───┘
      /api/v1    │        │  /        直传 9000 (预签名)
     ┌───────────▼──┐  ┌───▼────────┐
     │ bee-backend  │  │  bee-minio │◄──── 边缘设备 PUT/GET
     │  (FastAPI)   │  │  对象存储   │      (音频/特征/模型)
     └──┬────┬────┬─┘  └────────────┘
        │    │    │
   MySQL│Redis│   │ 调度
        │    │   └──────────────┐
        │    │        ┌─────────▼─────────┐
        │    └───────►│ celery-worker/beat │ 异步任务(训练/导出/清理)
        │             └───────────────────┘
        └── 结构化数据(温湿度/推理/账号/日志)
```

- **对象存储 (MinIO)**：`audio/<日期>/<设备>/`、`features/<设备>/`、`models/<设备>/`、`exports/<设备>/`
- **关系库 (MySQL)**：设备、推理结果、模型版本、训练任务、下载任务、账号、审计、密钥申请等

---

## 功能特性

**数据接入与存储**
- 边缘设备经 `X-API-Key` 鉴权上报推理结果 + 环境指标（温湿度 / 电池电压 / 信号强度），一次入库
- 音频、特征（`.npy`）、模型（`.pkl`）等大文件由设备经**预签名 URL 直传 MinIO**，后端不中转带宽
- 提供 `/health`、`/ready` 健康检查

**前端可视化（Vue 3 + Vite）**
- 登录门（账号登录 / 游客浏览），路由级权限
- 设备总览、设备详情、数据看板（失王概率曲线 + 各指标变化，ECharts）
- 数据下载（按设备导出音频+指标，异步打包 zip）

**模型训练与下发**
- 按设备触发：累积 600 条设备正样本 + 服务器预置 600 条失王负样本 → 训练 `SVM + StandardScaler`
- 全程 **Celery 异步**执行，写回 MinIO `models/<设备>/`，设备侧按对象修改时间拉取
- 训练可被用户端 / 管理端取消；写回前复核任务状态，取消后不产出模型

**管理控制台**
- 账号与权限：新增 / 删除 / 改密 / 调整普通用户 ↔ 管理员；**唯一超级管理员**不可删除、不可强制下线、角色与密码受保护
- 下载日志：按账号 / 按设备名模糊筛选、聚合统计、单条删除（连带导出文件）
- 设备管理：模型更新状态监控、分类**下载 / 清除**设备后端数据、更新日志查看与清空、遗留数据软提示
- 设备密钥重置申请审批、账号在线状态、强制下线

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 反向代理 | Nginx 1.25 |
| 后端 | Python 3.11 · FastAPI · SQLAlchemy 2.0 (async) · Pydantic v2 · Uvicorn |
| 异步任务 | Celery 5 + celery-redbeat（Redis broker/backend） |
| 数据库 | MySQL 8.0（`asyncmy` 驱动） |
| 缓存 / 会话 | Redis 7 |
| 对象存储 | MinIO（S3 兼容） |
| 机器学习 | scikit-learn（SVM）· NumPy · SciPy · python-speech-features |
| 鉴权 | JWT（python-jose）· bcrypt |
| 前端 | Vue 3 · TypeScript · Vite · Vue Router · ECharts |
| 日志 / 监控 | structlog（JSON）· prometheus-client |
| 部署 | Docker Compose · GitHub Actions → GHCR |

---

## 目录结构

```
.
├── app/
│   ├── main.py                 # FastAPI 入口（lifespan、CORS、中间件、路由注册）
│   ├── api/v1/                 # 路由：devices / auth / admin / download / models / health
│   ├── core/                   # 配置、数据库、安全(JWT/bcrypt)、Redis、会话
│   ├── models/                 # SQLAlchemy ORM
│   ├── schemas/                # Pydantic 模型
│   ├── services/               # MinIO 客户端、特征提取、设备数据、模型服务
│   ├── tasks/                  # Celery：训练 / 推理 / 下载导出 / 数据导出 / 清理
│   └── frontend/dist/          # 已构建的前端静态资源（Nginx 托管）
├── config/                     # nginx / mysql / (prometheus|grafana|loki|promtail 预留)
├── scripts/                    # bootstrap、环境初始化、负样本转换等
├── .github/workflows/          # 镜像同步 + 后端镜像构建
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
# 编辑 .env：数据库/Redis/MinIO 口令、JWT_SECRET、API_KEY_SALT、模型签名密钥等
```

### 2. 启动

```bash
# 方式 A：使用 GHCR 预构建镜像（推荐）
docker compose pull
docker compose up -d

# 方式 B：本地构建后端镜像
docker compose build backend
docker compose up -d
```

启动的核心服务：`nginx` · `backend` · `celery-worker` · `celery-beat` · `mysql` · `redis` · `minio`。

### 3. 访问

- 前端主页：`http://<服务器地址>/`
- 管理控制台：`http://<服务器地址>/#/admin`
- 健康检查：`GET /health`、`GET /ready`

> 首次启动时若账号表为空，将按 `BOOTSTRAP_ADMIN_USER` / `BOOTSTRAP_ADMIN_PASSWORD`
> 自动创建初始**超级管理员**（默认 `admin`，请在 `.env` 中改为强口令）。

---

## 主要接口

前缀统一为 `/api/v1`。

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/auth/login` `/auth/logout` `/auth/guest` | 登录 / 退出 / 游客 | 单会话 |
| POST | `/devices` | 注册设备（返回一次性 API Key） | 账号 |
| DELETE | `/devices/{id}` | 删除设备（停任务、留数据） | 账号 |
| POST | `/devices/inference` | 上报推理 + 环境指标 | 设备 Key |
| GET | `/devices/summary` `/devices/by-name/{name}` | 聚合 / 详情 | 只读 |
| GET | `/devices/{id}/presigned-url` | 预签名上传（`kind=audio\|feature`） | 设备 Key |
| POST | `/devices/{name}/model-update` | 触发模型更新 | 账号 |
| POST | `/devices/{name}/model-update/cancel` | 取消更新 | 账号 |
| POST | `/devices/{name}/key-reset-request` | 申请重置设备密钥 | 账号 |
| POST/GET | `/download` `/download/{job_id}` | 数据导出任务 | 账号 |
| — | `/admin/*` | 账号、下载日志、设备管理、密钥申请等 | 管理员 |

交互式文档：设置 `DEBUG=true` 后访问 `/docs`（Swagger）。

---

## 模型训练流程

1. 用户在设备详情页点击**「更新检测模型」** → 后端登记 `training_jobs`（`waiting`）
2. 设备将 16kHz 音频提取的 **60 维特征**经预签名直传 `features/<设备>/`
3. worker 初查 + beat 每 20 分钟扫描：样本 ≥ 600 → 训练 `SVM(rbf) + StandardScaler`
4. 结果写回 `models/<设备>/{svm,scaler}.pkl` 并登记 `model_versions`
5. 设备侧比对 `Last-Modified` 拉取新模型自动替换；超 24h 样本不足则置 `cancelled`
6. 训练期间可由用户端 / 管理端**取消**（写回前复核，取消则不产出）

---

## 部署与 CI/CD

镜像构建与同步由 GitHub Actions 完成：

- `mirror-base-images.yml`：同步基础镜像（nginx / mysql / redis / minio 等）到 GHCR
- `docker-build.yml`：`app/**` 变更时构建后端镜像并推送

镜像地址：`ghcr.io/aaaaa-xxxxx-m/hive_system/*`。服务器更新流程：

```bash
docker compose pull && docker compose up -d
```

> 重建 `backend` 容器后，若 Nginx 缓存了旧的容器 IP，执行 `docker exec bee-nginx nginx -s reload`。

---

## 安全

- SSH 密钥登录，数据库使用专用最小权限账号；敏感配置统一经 `.env` 管理，不入库、不入提交历史
- 登录采用 `bcrypt` + `JWT`；**单账号单会话**（Redis 记录活动时间，空闲超时自动释放，陈旧会话可被顶替）
- 敏感操作（删除设备、清除数据、强制下线、密钥重置）均需管理员操作与二次确认
- 模型训练、数据导出等重任务全部异步执行，保障在线接口稳定
