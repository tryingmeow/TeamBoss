#!/bin/bash
set -e
set -o pipefail

# TeamBoss 数据恢复脚本
#
# 用法：
#   ./restore.sh <backup_file_path> [--skip-service-check]
#
# 示例：
#   ./restore.sh <项目目录>/backend/data/backups/app-20260723T063420Z.db
#   ./restore.sh <项目目录>/backend/data/backups/sessions-20260723T063420Z.tar.gz
#
# 重要：恢复前必须停止所有会写入该数据目录的后端进程！
#       脚本会检查本机 systemd 服务（默认 auto-team.service，可用环境变量
#       AUTO_TEAM_SERVICE 指定其他名字）；容器恢复需先停止 Compose
#       backend，再指定 --skip-service-check。
#       如服务配置了 AUTO_TEAM_DATA_DIR，恢复时也必须带上相同环境变量。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="${AUTO_TEAM_SERVICE:-auto-team.service}"
DATA_DIR="${AUTO_TEAM_DATA_DIR:-${PROJECT_ROOT}/backend/data}"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

BACKUP_FILE="${1}"
SKIP_SERVICE_CHECK="${2}"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_info() {
    echo -e "[INFO] $1"
}

# 使用说明
if [ -z "$BACKUP_FILE" ]; then
    cat <<EOF
TeamBoss 数据恢复脚本

用法：
  $0 <backup_file_path> [--skip-service-check]

示例：
  $0 "${PROJECT_ROOT}/backend/data/backups/app-20260723T063420Z.db"
  $0 "${PROJECT_ROOT}/backend/data/backups/sessions-20260723T063420Z.tar.gz"

参数：
  backup_file_path         备份文件路径（.db 或 .tar.gz）
  --skip-service-check     跳过服务运行检查（谨慎使用）

环境变量：
  AUTO_TEAM_SERVICE        要检查的 systemd 服务名（默认 auto-team.service）
  AUTO_TEAM_DATA_DIR       数据目录（默认 backend/data）

重要安全提示：
  恢复前必须停止所有会写入该数据目录的后端进程！
  在服务运行时覆盖数据库会导致数据损坏。

停止服务（需要 root）：
  sudo systemctl stop ${SERVICE_NAME}

恢复完成后重启服务：
  sudo systemctl start ${SERVICE_NAME}

EOF
    exit 1
fi

# 验证备份文件存在
if [ ! -f "$BACKUP_FILE" ]; then
    print_error "备份文件不存在: $BACKUP_FILE"
    exit 1
fi

print_info "恢复文件: $BACKUP_FILE"

# 检查服务是否运行
if [ "$SKIP_SERVICE_CHECK" != "--skip-service-check" ]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$SERVICE_NAME"; then
        print_error "${SERVICE_NAME} 正在运行！恢复前必须停止服务。"
        print_info "停止服务命令："
        echo "  sudo systemctl stop ${SERVICE_NAME}"
        exit 1
    fi
    print_success "未发现正在运行的 ${SERVICE_NAME}"
fi

# 根据文件类型进行恢复
if [[ "$BACKUP_FILE" == *.db ]]; then
    # 恢复数据库
    DB_PATH="${DATA_DIR}/app.db"
    BACKUP_COPY="${DATA_DIR}/app.db.restore-backup-$(date +%Y%m%dT%H%M%SZ)"
    RESTORE_TEMP=$(mktemp "${DATA_DIR}/.app.db.restore.XXXXXX")

    print_info "恢复数据库..."
    print_warning "原数据库将备份到: $BACKUP_COPY"

    # 用 SQLite backup API 生成自包含的临时库：备份文件若是 WAL 模式，直接 cp 过来的
    # 文件头仍声明 WAL，打开时会在旁边生成 -wal/-shm。这里统一转成 DELETE 日志模式，
    # 并做完整性检查；临时库旁边不留任何日志文件。
    # 备份文件按 mode=ro 打开：它旁边若本来就有 -wal，那是备份内容的一部分，要读进来。
    # 只读打开 WAL 头的文件（scripts/backup.py 产出的就是）会在旁边新建 -shm 和空 -wal，
    # 关闭后只删本次新建的，原有的不动。不用 immutable=1：它会无视旁边已有的 -wal，
    # 静默丢掉其中的提交。SQLite 按解析掉符号链接后的路径建这些文件，所以按真实路径记。
    if ! python3 - "$BACKUP_FILE" "$RESTORE_TEMP" <<'PY'
import os
import sqlite3
import sys
from urllib.parse import quote

source = os.path.realpath(sys.argv[1])
created = [source + ext for ext in ("-wal", "-shm") if not os.path.lexists(source + ext)]
result = None
try:
    src = sqlite3.connect(f"file:{quote(source)}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(sys.argv[2])
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            result = dst.execute("PRAGMA integrity_check").fetchone()
        finally:
            dst.close()
    finally:
        src.close()
finally:
    for path in created:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
if not result or str(result[0]).lower() != "ok":
    raise SystemExit(1)
PY
    then
        rm -f "$RESTORE_TEMP" "${RESTORE_TEMP}-wal" "${RESTORE_TEMP}-shm" "${RESTORE_TEMP}-journal"
        print_error "恢复的数据库无法读取或完整性检查失败，现有数据库未改动。"
        exit 1
    fi
    rm -f "${RESTORE_TEMP}-wal" "${RESTORE_TEMP}-shm" "${RESTORE_TEMP}-journal"
    chmod 600 "$RESTORE_TEMP"

    # 备份现有数据库。服务已停但 app.db 旁可能还有未检查点的 -wal，单独 cp app.db
    # 会丢掉这部分提交；所以用 backup API 读出包含 WAL 的一致快照，并做完整性检查。
    # 读不出来或快照不完整时，退化为把 app.db 与 -wal/-shm 原样一并保存，不阻断恢复。
    # 现有库必须按 mode=ro 打开：读写连接作为最后一个连接关闭时，会把 WAL 检查点进
    # （可能已损坏的）app.db 再删掉 -wal，退化路径拿到的就不再是原样现场。只读连接
    # 不写主文件也不删 -wal；它新建的 -shm/空 -wal 在下面替换前会和旧的一起清掉。
    if [ -f "$DB_PATH" ]; then
        if python3 - "$DB_PATH" "$BACKUP_COPY" <<'PY'
import sqlite3
import sys
from urllib.parse import quote

src = sqlite3.connect(f"file:{quote(sys.argv[1])}?mode=ro", uri=True)
try:
    dst = sqlite3.connect(sys.argv[2])
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        result = dst.execute("PRAGMA integrity_check").fetchone()
    finally:
        dst.close()
finally:
    src.close()
if not result or str(result[0]).lower() != "ok":
    raise SystemExit(1)
PY
        then
            rm -f "${BACKUP_COPY}-wal" "${BACKUP_COPY}-shm" "${BACKUP_COPY}-journal"
        else
            print_warning "现有数据库无法完整读取或完整性检查未通过，改为原样复制 app.db 及其 -wal/-shm"
            # 失败的快照可能在副本旁留下日志文件；不清掉的话会被重放到原样副本上。
            rm -f "$BACKUP_COPY" "${BACKUP_COPY}-wal" "${BACKUP_COPY}-shm" "${BACKUP_COPY}-journal"
            cp "$DB_PATH" "$BACKUP_COPY"
            for ext in -wal -shm; do
                if [ -f "${DB_PATH}${ext}" ]; then
                    cp "${DB_PATH}${ext}" "${BACKUP_COPY}${ext}"
                    chmod 600 "${BACKUP_COPY}${ext}"
                fi
            done
        fi
        chmod 600 "$BACKUP_COPY"
        print_success "原数据库已备份: $BACKUP_COPY"
    fi

    # 旧库遗留的 -wal/-shm 绝不能留在恢复后的库旁边：SQLite 会把旧 WAL 重放到新库上，
    # 造成数据库损坏。安全副本已完成，在替换前一刻清掉，再同一文件系统内原子替换。
    rm -f "${DB_PATH}-wal" "${DB_PATH}-shm"
    mv "$RESTORE_TEMP" "$DB_PATH"
    chmod 600 "$DB_PATH"

    # 最终路径上再检查一次；失败时原库在 $BACKUP_COPY。
    if ! python3 - "$DB_PATH" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
try:
    result = conn.execute("PRAGMA integrity_check").fetchone()
finally:
    conn.close()
if not result or str(result[0]).lower() != "ok":
    raise SystemExit(1)
PY
    then
        print_error "恢复后的数据库完整性检查失败！原数据库备份在: $BACKUP_COPY"
        exit 1
    fi

    print_success "数据库恢复成功"

elif [[ "$BACKUP_FILE" == *.tar.gz ]]; then
    # 恢复会话目录
    print_info "恢复会话目录..."

    SESSIONS_PATH="${DATA_DIR}/sessions"
    SESSIONS_BACKUP="${DATA_DIR}/sessions.restore-backup-$(date +%Y%m%dT%H%M%SZ)"
    EXTRACT_DIR=$(mktemp -d "${DATA_DIR}/.sessions.restore.XXXXXX")

    # 只接受普通文件和目录。符号链接 / 硬链接条目会让恢复出来的会话文件指向数据目录
    # 以外的任意文件，后端之后读写会话时就会读到或改写那些文件；设备、FIFO 同理拒绝。
    if ! python3 - "$BACKUP_FILE" <<'PY'
import pathlib
import sys
import tarfile

try:
    with tarfile.open(sys.argv[1], "r:gz") as archive:
        for member in archive:
            path = pathlib.PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise SystemExit(1)
            if not (member.isfile() or member.isdir()):
                raise SystemExit(1)
except (tarfile.TarError, OSError, EOFError):
    raise SystemExit(1)
PY
    then
        rm -rf "$EXTRACT_DIR"
        print_error "会话备份包含不安全的条目（绝对路径、..、符号链接、硬链接或特殊文件），已拒绝恢复。"
        exit 1
    fi

    if ! tar -xzf "$BACKUP_FILE" -C "$EXTRACT_DIR"; then
        rm -rf "$EXTRACT_DIR"
        print_error "会话备份解压失败，现有会话未改动。"
        exit 1
    fi
    if [ ! -d "$EXTRACT_DIR/sessions" ]; then
        rm -rf "$EXTRACT_DIR"
        print_error "会话备份缺少 sessions 目录，现有会话未改动。"
        exit 1
    fi

    chmod 700 "$EXTRACT_DIR/sessions"
    find "$EXTRACT_DIR/sessions" -type f -exec chmod 600 {} \;
    if [ -d "$SESSIONS_PATH" ]; then
        mv "$SESSIONS_PATH" "$SESSIONS_BACKUP"
        print_success "原会话目录已备份: $SESSIONS_BACKUP"
    fi
    mv "$EXTRACT_DIR/sessions" "$SESSIONS_PATH"
    rmdir "$EXTRACT_DIR"

    print_success "会话目录恢复成功"

else
    print_error "不支持的备份文件格式，需要 .db 或 .tar.gz 文件"
    exit 1
fi

cat <<EOF

${GREEN}恢复完成${NC}

后续步骤：
  1. 检查恢复后的数据是否正确
  2. 重新启动此前停止的后端服务
  3. 请求 /api/health 验证服务状态
  4. 检查后端日志

EOF
