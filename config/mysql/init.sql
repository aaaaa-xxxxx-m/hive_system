-- MySQL 初始化脚本
-- 创建业务表、索引、分区策略

USE bee_db;

-- ========== 设备表 ==========
CREATE TABLE IF NOT EXISTS devices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    device_name VARCHAR(64) NOT NULL COMMENT '设备唯一标识 (如: bee-001)',
    api_key_hash CHAR(64) NOT NULL COMMENT 'API Key SHA256 哈希',
    device_fingerprint VARCHAR(128) COMMENT '设备指纹 (硬件唯一标识)',
    name VARCHAR(128) COMMENT '设备别名',
    location_lat DECIMAL(10, 7) COMMENT '纬度',
    location_lng DECIMAL(10, 7) COMMENT '经度',
    location_desc VARCHAR(255) COMMENT '位置描述',
    firmware_version VARCHAR(32) COMMENT '当前固件版本',
    target_firmware_version VARCHAR(32) COMMENT '目标固件版本 (OTA)',
    status ENUM('online', 'offline', 'maintenance', 'decommissioned') NOT NULL DEFAULT 'offline' COMMENT '设备状态',
    last_seen_at TIMESTAMP NULL COMMENT '最后心跳时间',
    registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_device_name (device_name),
    UNIQUE KEY uk_api_key_hash (api_key_hash),
    KEY idx_status (status),
    KEY idx_last_seen (last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='设备注册表';

-- ========== 推理结果表 (按月分区) —— 同时承载环境指标 (方案 C: 合并原 sensor_data) ==========
CREATE TABLE IF NOT EXISTS inference_results (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    device_id BIGINT UNSIGNED NOT NULL COMMENT '设备 ID',
    device_name VARCHAR(64) NOT NULL COMMENT '设备名',
    audio_object_key VARCHAR(256) NOT NULL COMMENT 'MinIO 音频对象键',
    feature_object_key VARCHAR(256) COMMENT 'MinIO 特征对象键',
    model_version VARCHAR(64) NOT NULL COMMENT '模型版本',
    probability_no_queen DECIMAL(5, 4) NOT NULL COMMENT '无蜂王概率 [0,1]',
    probability_normal DECIMAL(5, 4) NOT NULL COMMENT '正常蜂群概率 [0,1]',
    label ENUM('no_queen', 'normal', 'uncertain') NOT NULL COMMENT '推理标签',
    confidence DECIMAL(5, 4) NOT NULL COMMENT '置信度',
    inference_time_ms INT UNSIGNED COMMENT '推理耗时 (ms)',
    temperature DECIMAL(5, 2) COMMENT '温度 (°C)',
    humidity DECIMAL(5, 2) COMMENT '湿度 (%)',
    battery_voltage DECIMAL(5, 3) COMMENT '电池电压 (V)',
    signal_strength INT COMMENT '信号强度 (dBm)',
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '采集时间',
    received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '服务器接收时间',
    processed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '处理完成时间',
    PRIMARY KEY (id, recorded_at),
    KEY idx_device_recorded (device_id, recorded_at),
    KEY idx_model_recorded (model_version, recorded_at),
    KEY idx_label (label),
    KEY idx_recorded (recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='推理结果'
PARTITION BY RANGE (UNIX_TIMESTAMP(recorded_at)) (
    PARTITION p202601 VALUES LESS THAN (UNIX_TIMESTAMP('2026-02-01')),
    PARTITION p202602 VALUES LESS THAN (UNIX_TIMESTAMP('2026-03-01')),
    PARTITION p202603 VALUES LESS THAN (UNIX_TIMESTAMP('2026-04-01')),
    PARTITION p202604 VALUES LESS THAN (UNIX_TIMESTAMP('2026-05-01')),
    PARTITION p202605 VALUES LESS THAN (UNIX_TIMESTAMP('2026-06-01')),
    PARTITION p202606 VALUES LESS THAN (UNIX_TIMESTAMP('2026-07-01')),
    PARTITION p202607 VALUES LESS THAN (UNIX_TIMESTAMP('2026-08-01')),
    PARTITION p202608 VALUES LESS THAN (UNIX_TIMESTAMP('2026-09-01')),
    PARTITION p202609 VALUES LESS THAN (UNIX_TIMESTAMP('2026-10-01')),
    PARTITION p202610 VALUES LESS THAN (UNIX_TIMESTAMP('2026-11-01')),
    PARTITION p202611 VALUES LESS THAN (UNIX_TIMESTAMP('2026-12-01')),
    PARTITION p202612 VALUES LESS THAN (UNIX_TIMESTAMP('2027-01-01')),
    PARTITION p_future VALUES LESS THAN MAXVALUE
);

-- ========== 模型版本表 ==========
CREATE TABLE IF NOT EXISTS model_versions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    version VARCHAR(64) NOT NULL COMMENT '版本号 (如: v1.2.0-20260926)',
    model_type ENUM('svm', 'cnn', 'transformer') NOT NULL DEFAULT 'svm' COMMENT '模型类型',
    minio_object_key VARCHAR(256) NOT NULL COMMENT 'MinIO 模型文件键',
    scaler_object_key VARCHAR(256) COMMENT 'MinIO 标准化器键',
    signature VARCHAR(512) NOT NULL COMMENT 'Ed25519 签名 (Base64)',
    file_size BIGINT UNSIGNED NOT NULL COMMENT '文件大小 (bytes)',
    sha256_hash CHAR(64) NOT NULL COMMENT 'SHA256 哈希',
    training_dataset_size INT UNSIGNED COMMENT '训练样本数',
    training_accuracy DECIMAL(5, 4) COMMENT '训练集准确率',
    validation_accuracy DECIMAL(5, 4) COMMENT '验证集准确率',
    metrics JSON COMMENT '详细指标 (precision, recall, f1 等)',
    status ENUM('training', 'validating', 'active', 'deprecated', 'failed') NOT NULL DEFAULT 'training' COMMENT '状态',
    is_active BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否为当前生产模型',
    trained_at TIMESTAMP NULL COMMENT '训练完成时间',
    deployed_at TIMESTAMP NULL COMMENT '部署时间',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_version (version),
    UNIQUE KEY uk_sha256 (sha256_hash),
    KEY idx_status (status),
    KEY idx_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='模型版本管理';

-- ========== 训练任务表 ==========
CREATE TABLE IF NOT EXISTS training_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    job_id CHAR(36) NOT NULL COMMENT 'Celery Task ID',
    model_version_id BIGINT UNSIGNED COMMENT '关联模型版本',
    device_name VARCHAR(64) COMMENT '关联设备名 (按设备触发训练)',
    requested_at TIMESTAMP NULL COMMENT '触发时刻 (取其后样本)',
    feature_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已收集正样本数',
    status ENUM('pending', 'waiting', 'running', 'completed', 'failed', 'cancelled') NOT NULL DEFAULT 'pending' COMMENT '任务状态',
    progress INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '进度百分比',
    config JSON COMMENT '训练配置 (超参数等)',
    logs TEXT COMMENT '训练日志',
    error_message TEXT COMMENT '错误信息',
    started_at TIMESTAMP NULL COMMENT '开始时间',
    completed_at TIMESTAMP NULL COMMENT '完成时间',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_job_id (job_id),
    KEY idx_status (status),
    KEY idx_model_version (model_version_id),
    FOREIGN KEY (model_version_id) REFERENCES model_versions(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='训练任务记录';

-- ========== 数据下载任务表 ==========
CREATE TABLE IF NOT EXISTS download_jobs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    job_id CHAR(36) NOT NULL COMMENT '任务 UUID',
    device_name VARCHAR(64) NOT NULL COMMENT '设备名',
    username VARCHAR(64) COMMENT '触发账号 (主界面接入后写入)',
    data_limit INT UNSIGNED NOT NULL COMMENT '打包最近 N 条记录',
    status ENUM('pending', 'running', 'completed', 'failed') NOT NULL DEFAULT 'pending' COMMENT '任务状态',
    progress INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '进度百分比',
    object_key VARCHAR(256) COMMENT 'MinIO 导出包键',
    error_message TEXT COMMENT '错误信息',
    started_at TIMESTAMP NULL COMMENT '开始时间',
    completed_at TIMESTAMP NULL COMMENT '完成时间',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_job_id (job_id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='数据下载任务记录';

-- ========== 登录账号表 (普通用户 / 管理员) ==========
CREATE TABLE IF NOT EXISTS accounts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    username VARCHAR(64) NOT NULL COMMENT '登录名',
    password_hash VARCHAR(255) NOT NULL COMMENT 'bcrypt 密码哈希',
    role ENUM('user', 'admin') NOT NULL DEFAULT 'user' COMMENT '角色',
    enabled BOOLEAN NOT NULL DEFAULT TRUE COMMENT '是否启用',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    last_login_at TIMESTAMP NULL COMMENT '最后登录时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='登录账号';

-- ========== 特征数据表 (原始特征, 仅保留最近 30 天) ==========
CREATE TABLE IF NOT EXISTS raw_features (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    device_id BIGINT UNSIGNED NOT NULL COMMENT '设备 ID',
    device_name VARCHAR(64) NOT NULL COMMENT '设备名',
    feature_vector JSON NOT NULL COMMENT '特征向量 (JSON 数组)',
    label ENUM('no_queen', 'normal', 'unknown') NOT NULL DEFAULT 'unknown' COMMENT '标签',
    minio_object_key VARCHAR(256) COMMENT 'MinIO 原始特征文件键',
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '采集时间',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    PRIMARY KEY (id, recorded_at),
    KEY idx_device_recorded (device_id, recorded_at),
    KEY idx_label (label),
    KEY idx_recorded (recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='原始特征数据 (30 天保留)'
PARTITION BY RANGE (UNIX_TIMESTAMP(recorded_at)) (
    PARTITION p202609 VALUES LESS THAN (UNIX_TIMESTAMP('2026-10-01')),
    PARTITION p202610 VALUES LESS THAN (UNIX_TIMESTAMP('2026-11-01')),
    PARTITION p202611 VALUES LESS THAN (UNIX_TIMESTAMP('2026-12-01')),
    PARTITION p202612 VALUES LESS THAN (UNIX_TIMESTAMP('2027-01-01')),
    PARTITION p_future VALUES LESS THAN MAXVALUE
);

-- ========== 审计日志表 ==========
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键',
    trace_id CHAR(36) NOT NULL COMMENT '链路追踪 ID',
    user_id BIGINT UNSIGNED COMMENT '操作用户 ID (预留)',
    device_id BIGINT UNSIGNED COMMENT '关联设备',
    action VARCHAR(64) NOT NULL COMMENT '操作类型',
    resource_type VARCHAR(64) COMMENT '资源类型',
    resource_id VARCHAR(128) COMMENT '资源 ID',
    request_ip VARCHAR(45) COMMENT '请求 IP',
    request_ua TEXT COMMENT 'User-Agent',
    request_body JSON COMMENT '请求体 (脱敏)',
    response_code INT COMMENT '响应码',
    duration_ms INT UNSIGNED COMMENT '耗时 (ms)',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id, created_at),
    KEY idx_trace (trace_id),
    KEY idx_device (device_id),
    KEY idx_action (action),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='审计日志'
PARTITION BY RANGE (UNIX_TIMESTAMP(created_at)) (
    PARTITION p202609 VALUES LESS THAN (UNIX_TIMESTAMP('2026-10-01')),
    PARTITION p202610 VALUES LESS THAN (UNIX_TIMESTAMP('2026-11-01')),
    PARTITION p202611 VALUES LESS THAN (UNIX_TIMESTAMP('2026-12-01')),
    PARTITION p202612 VALUES LESS THAN (UNIX_TIMESTAMP('2027-01-01')),
    PARTITION p_future VALUES LESS THAN MAXVALUE
);

-- ========== 创建视图: 设备最新状态 ==========
CREATE OR REPLACE VIEW v_device_latest AS
SELECT
    t.id, t.device_name, t.name, t.status, t.last_seen_at, t.firmware_version,
    t.location_lat, t.location_lng, t.location_desc,
    t.temperature, t.humidity, t.battery_voltage, t.signal_strength,
    t.recorded_at AS sensor_recorded_at,
    t.probability_no_queen, t.probability_normal, t.label AS inference_label,
    t.confidence, t.recorded_at AS inference_recorded_at, t.model_version
FROM (
    SELECT d.id, d.device_name, d.name, d.status, d.last_seen_at, d.firmware_version,
           d.location_lat, d.location_lng, d.location_desc,
           ir.temperature, ir.humidity, ir.battery_voltage, ir.signal_strength,
           ir.probability_no_queen, ir.probability_normal, ir.label, ir.confidence,
           ir.model_version, ir.recorded_at,
           ROW_NUMBER() OVER (PARTITION BY d.id ORDER BY ir.recorded_at DESC) AS rn
    FROM devices d
    LEFT JOIN inference_results ir ON d.id = ir.device_id
) t
WHERE t.rn = 1;

-- ========== 创建视图: 每日推理统计 ==========
CREATE OR REPLACE VIEW v_daily_inference_stats AS
SELECT
    DATE(recorded_at) AS stat_date,
    device_name,
    COUNT(*) AS total_count,
    SUM(CASE WHEN label = 'no_queen' THEN 1 ELSE 0 END) AS no_queen_count,
    SUM(CASE WHEN label = 'normal' THEN 1 ELSE 0 END) AS normal_count,
    SUM(CASE WHEN label = 'uncertain' THEN 1 ELSE 0 END) AS uncertain_count,
    AVG(confidence) AS avg_confidence,
    AVG(inference_time_ms) AS avg_inference_time_ms
FROM inference_results
GROUP BY DATE(recorded_at), device_name;

-- ========== 创建存储过程: 清理过期分区 (可由 Event Scheduler 定时调用) ==========
DELIMITER //
CREATE PROCEDURE IF NOT EXISTS sp_cleanup_old_partitions()
BEGIN
    DECLARE done INT DEFAULT FALSE;
    DECLARE tbl_name VARCHAR(64);
    DECLARE part_name VARCHAR(64);
    DECLARE cutoff_date DATE;
    DECLARE cur CURSOR FOR
        SELECT TABLE_NAME, PARTITION_NAME
        FROM INFORMATION_SCHEMA.PARTITIONS
        WHERE TABLE_SCHEMA = 'bee_db'
          AND PARTITION_NAME != 'p_future'
          AND PARTITION_DESCRIPTION < UNIX_TIMESTAMP(DATE_SUB(CURDATE(), INTERVAL 90 DAY));
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;

    OPEN cur;
    read_loop: LOOP
        FETCH cur INTO tbl_name, part_name;
        IF done THEN LEAVE read_loop; END IF;
        SET @sql = CONCAT('ALTER TABLE `', tbl_name, '` DROP PARTITION `', part_name, '`');
        PREPARE stmt FROM @sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END LOOP;
    CLOSE cur;
END//
DELIMITER ;

-- ========== 创建 Event: 每天凌晨 3 点清理 90 天前的分区 ==========
SET GLOBAL event_scheduler = ON;
CREATE EVENT IF NOT EXISTS evt_cleanup_partitions
ON SCHEDULE EVERY 1 DAY
STARTS TIMESTAMP(CURDATE() + INTERVAL 1 DAY, '03:00:00')
DO CALL sp_cleanup_old_partitions();

-- ========== 插入初始设备 (示例, 生产环境请通过 API 注册) ==========
-- INSERT INTO devices (device_name, api_key_hash, device_fingerprint, name, status)
-- VALUES ('bee-001', SHA2('initial-api-key-change-me', 256), 'fingerprint-001', '蜂箱-001', 'offline');