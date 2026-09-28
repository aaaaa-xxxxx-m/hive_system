#!/bin/bash
# bootstrap.sh - 服务器基础设施强化脚本
# 运行环境: 阿里云 ECS (CentOS/RHEL 类)
# 作用: 修改 MySQL 密码、创建应用用户、配置防火墙、内核参数、安装 Docker

set -euo pipefail

# ========== 颜色输出 ==========
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ========== 0. 预检 ==========
if [[ $EUID -ne 0 ]]; then
    log_error "必须以 root 运行"
    exit 1
fi

log_info "=== 智能蜂箱云服务器基础设施强化开始 ==="

# ========== 1. 系统更新与基础包 ==========
log_info "更新系统并安装基础工具..."
yum update -y
yum install -y curl wget vim git net-tools lsof htop jq unzip tar gzip \
    ca-certificates gnupg2 lsb-release device-mapper-persistent-data lvm2 \
    policycoreutils-python-utils

# ========== 2. 内核参数优化 ==========
log_info "配置内核参数 (网络、文件描述符、内存)..."
cat > /etc/sysctl.d/99-bee-monitor.conf <<'EOF'
# 网络优化
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_keepalive_time = 300
net.ipv4.ip_local_port_range = 1024 65535

# 文件描述符
fs.file-max = 1000000
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 524288

# 内存/交换
vm.swappiness = 10
vm.overcommit_memory = 1
EOF
sysctl --system

# ========== 3. 文件描述符限制 ==========
log_info "设置 ulimit..."
cat > /etc/security/limits.d/99-bee-monitor.conf <<'EOF'
* soft nofile 1000000
* hard nofile 1000000
* soft nproc 65535
* hard nproc 65535
root soft nofile 1000000
root hard nofile 1000000
EOF

# ========== 4. 时区与 NTP ==========
log_info "设置时区为 Asia/Shanghai 并同步时间..."
timedatectl set-timezone Asia/Shanghai
yum install -y chrony
systemctl enable --now chronyd
chronyc makestep

# ========== 5. SSH 加固 ==========
log_info "加固 SSH 配置..."
cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak.$(date +%F)
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
# 仅允许密钥登录
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin prohibit-password
AuthorizedKeysFile .ssh/authorized_keys

# 协议与加密
Protocol 2
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-gcm@openssh.com
MACs hmac-sha2-512-etm@openssh.com,hmac-sha2-256-etm@openssh.com,umac-128-etm@openssh.com
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org,diffie-hellman-group16-sha512

# 会话
ClientAliveInterval 300
ClientAliveCountMax 2
MaxAuthTries 3
MaxSessions 2
LoginGraceTime 30

# 禁用不需要的功能
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
PermitTunnel no
DebianBanner no
EOF
systemctl reload sshd

# ========== 6. 防火墙 ==========
log_info "配置 firewalld..."
systemctl enable --now firewalld
firewall-cmd --permanent --set-default-zone=public
firewall-cmd --permanent --add-service=ssh
firewall-cmd --permanent --add-port=80/tcp   # HTTP (Nginx)
firewall-cmd --permanent --add-port=443/tcp  # HTTPS (Nginx)
firewall-cmd --permanent --add-port=3000/tcp # Grafana (仅内网/受限)
firewall-cmd --permanent --add-port=9090/tcp # Prometheus (仅内网/受限)
firewall-cmd --permanent --add-port=9001/tcp # MinIO Console (仅内网/受限)
firewall-cmd --reload

# ========== 7. Fail2Ban ==========
log_info "安装并配置 Fail2Ban..."
yum install -y epel-release
yum install -y fail2ban
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/secure
maxretry = 3
bantime = 3600
findtime = 600
ignoreip = 127.0.0.1/8
EOF
systemctl enable --now fail2ban

# ========== 8. MySQL 加固 (修改 root 密码、创建应用用户) ==========
log_info "加固 MySQL..."

# 生成随机密码 (若 .env 已存在则读取)
MYSQL_ROOT_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
MYSQL_APP_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# 修改 root 密码 (首次运行时 root 无密码)
mysql -u root -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}'; FLUSH PRIVILEGES;" 2>/dev/null || \
mysql -u root -p"${MYSQL_ROOT_PASSWORD}" -e "SELECT 1;" 2>/dev/null || {
    log_warn "MySQL root 密码已设置，跳过修改"
}

# 创建应用数据库和用户
mysql -u root -p"${MYSQL_ROOT_PASSWORD}" <<EOF
CREATE DATABASE IF NOT EXISTS bee_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'bee_user'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, INDEX, ALTER ON bee_db.* TO 'bee_user'@'%';
FLUSH PRIVILEGES;
EOF

# 保存密码到 .env (仅首次)
if [[ ! -f /opt/bee-monitor/.env ]]; then
    cat > /opt/bee-monitor/.env <<EOF
# === 数据库 ===
MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
MYSQL_DATABASE=bee_db
MYSQL_USER=bee_user
MYSQL_PASSWORD=${MYSQL_APP_PASSWORD}

# === Redis ===
REDIS_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# === MinIO ===
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# === Grafana ===
GF_SECURITY_ADMIN_USER=admin
GF_SECURITY_ADMIN_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# === JWT / API ===
JWT_SECRET=$(openssl rand -base64 64 | tr -d "=+/" | cut -c1-64)
API_KEY_SALT=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)

# === 模型签名 ===
MODEL_SIGNING_PRIVATE_KEY=$(openssl genpkey -algorithm Ed25519 -outform PEM | base64 -w0)
MODEL_SIGNING_PUBLIC_KEY=$(echo "${MODEL_SIGNING_PRIVATE_KEY}" | base64 -d | openssl pkey -pubout -outform PEM | base64 -w0)
EOF
    chmod 600 /opt/bee-monitor/.env
    log_info "已生成 /opt/bee-monitor/.env (含随机密码)"
else
    log_info "/opt/bee-monitor/.env 已存在，跳过生成"
fi

# ========== 9. 安装 Docker ==========
log_info "安装 Docker Engine + Compose Plugin..."
if ! command -v docker &>/dev/null; then
    yum remove -y docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-engine podman runc 2>/dev/null || true
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    yum install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    # 配置 Docker daemon
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  },
  "storage-driver": "overlay2",
  "default-ulimits": {
    "nofile": {
      "Name": "nofile",
      "Hard": 1000000,
      "Soft": 1000000
    }
  },
  "registry-mirrors": ["https://docker.mirrors.ustc.edu.cn"]
}
EOF
    systemctl restart docker
    log_info "Docker 安装完成"
else
    log_info "Docker 已安装，跳过"
fi

# ========== 10. 验证 ==========
log_info "=== 验证安装 ==="
docker --version
docker compose version
mysql -u bee_user -p"${MYSQL_APP_PASSWORD}" -e "SELECT 'MySQL OK';"
systemctl is-active docker firewalld fail2ban chronyd

log_info "=== bootstrap.sh 完成 ==="
log_warn "重要: 请妥善保存 /opt/bee-monitor/.env 中的所有密码！"
log_warn "下一步: 编写 docker-compose.yml 并启动全栈服务"