#!/bin/bash
# 生成随机密码
MYSQL_ROOT_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
MYSQL_APP_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
REDIS_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
MINIO_ROOT_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
GF_ADMIN_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
JWT_SECRET=$(openssl rand -base64 64 | tr -d "=+/" | cut -c1-64)
API_KEY_SALT=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# 生成 Ed25519 密钥对
MODEL_SIGNING_PRIVATE_KEY=$(openssl genpkey -algorithm Ed25519 -outform PEM | base64 -w0)
MODEL_SIGNING_PUBLIC_KEY=$(echo "${MODEL_SIGNING_PRIVATE_KEY}" | base64 -d | openssl pkey -pubout -outform PEM | base64 -w0)

# 设置 MySQL root 密码
mysql -u root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}'; FLUSH PRIVILEGES;" 2>/dev/null || true

# 创建应用数据库和用户
mysql -u root -p"${MYSQL_ROOT_PASSWORD}" <<MYSQL_EOF
CREATE DATABASE IF NOT EXISTS bee_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'bee_user'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON bee_db.* TO 'bee_user'@'%';
FLUSH PRIVILEGES;
MYSQL_EOF

# 写入 .env
cat > /opt/bee-monitor/.env <<ENV_EOF
# === 数据库 ===
MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
MYSQL_DATABASE=bee_db
MYSQL_USER=bee_user
MYSQL_PASSWORD=${MYSQL_APP_PASSWORD}

# === Redis ===
REDIS_PASSWORD=${REDIS_PASSWORD}

# === MinIO ===
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}

# === Grafana ===
GF_SECURITY_ADMIN_USER=admin
GF_SECURITY_ADMIN_PASSWORD=${GF_ADMIN_PASSWORD}

# === JWT / API ===
JWT_SECRET=${JWT_SECRET}
API_KEY_SALT=${API_KEY_SALT}

# === 模型签名 ===
MODEL_SIGNING_PRIVATE_KEY=${MODEL_SIGNING_PRIVATE_KEY}
MODEL_SIGNING_PUBLIC_KEY=${MODEL_SIGNING_PUBLIC_KEY}
ENV_EOF

chmod 600 /opt/bee-monitor/.env
echo '.env created successfully'
cat /opt/bee-monitor/.env