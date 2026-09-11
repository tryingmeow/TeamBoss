#!/usr/bin/env python3
"""
TeamBoss 数据备份脚本

备份 SQLite 数据库和会话目录，支持自动清理旧备份。
使用 SQLite 在线备份机制，确保数据一致性。

用法：
  python backup.py [选项]

选项：
  --db-path PATH           SQLite 数据库路径（默认: AUTO_TEAM_DATA_DIR/app.db）
  --sessions-path PATH     会话目录路径（默认: AUTO_TEAM_DATA_DIR/sessions）
  --backup-dir PATH        备份输出目录（默认: AUTO_TEAM_DATA_DIR/backups）
  --keep-count N           保留最近 N 份备份（默认: 10）
  --dry-run               仅打印操作不执行
  --help                  显示帮助信息

示例：
  python backup.py                              # 使用所有默认值
  python backup.py --keep-count 20              # 保留最近 20 份
  python backup.py --dry-run                    # 演习模式，不执行实际备份
"""

import sys
import sqlite3
import shutil
import os
import argparse
import logging
import tarfile
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """Match the application data-directory configuration."""
    configured_dir = os.getenv("AUTO_TEAM_DATA_DIR")
    if configured_dir:
        return Path(configured_dir)
    return Path(__file__).resolve().parents[1] / "backend" / "data"


def parse_args():
    data_dir = default_data_dir()
    parser = argparse.ArgumentParser(
        description='TeamBoss 数据备份脚本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        '--db-path',
        default=str(data_dir / 'app.db'),
        help='SQLite 数据库路径'
    )
    parser.add_argument(
        '--sessions-path',
        default=str(data_dir / 'sessions'),
        help='会话目录路径'
    )
    parser.add_argument(
        '--backup-dir',
        default=str(data_dir / 'backups'),
        help='备份输出目录'
    )
    parser.add_argument(
        '--keep-count',
        type=int,
        default=10,
        help='保留最近 N 份备份'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='仅打印操作不执行'
    )
    return parser.parse_args()


def backup_database(db_path: str, backup_path: str, dry_run: bool = False) -> bool:
    """
    使用 SQLite 在线备份机制备份数据库

    Args:
        db_path: 源数据库文件路径
        backup_path: 备份文件输出路径
        dry_run: 如果为 True，仅打印不执行

    Returns:
        True 表示成功，False 表示失败
    """
    try:
        if dry_run:
            logger.info(f"[DRY-RUN] 备份数据库: {db_path} -> {backup_path}")
            return True

        logger.info(f"开始备份数据库: {db_path}")

        # 连接源数据库
        src_conn = sqlite3.connect(db_path, timeout=30.0)
        src_conn.execute('PRAGMA query_only = TRUE')  # 只读模式，防止写入

        try:
            # 连接目标备份数据库
            dst_conn = sqlite3.connect(backup_path, timeout=30.0)

            try:
                # 执行在线备份
                with dst_conn:
                    src_conn.backup(dst_conn, pages=0, progress=None)

                # 设置备份文件权限为 600（只有所有者可读写）
                os.chmod(backup_path, 0o600)

                # 验证备份完整性：检查备份是否可以打开
                verify_conn = sqlite3.connect(backup_path, timeout=5.0)
                try:
                    integrity = verify_conn.execute('PRAGMA integrity_check').fetchone()
                finally:
                    verify_conn.close()
                if not integrity or str(integrity[0]).lower() != 'ok':
                    raise RuntimeError(
                        f"备份完整性检查失败: {integrity[0] if integrity else 'no result'}"
                    )

                logger.info(f"数据库备份完成: {backup_path}")
                return True
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

    except Exception as e:
        logger.error(f"数据库备份失败: {e}", exc_info=True)
        _remove_partial_backup(backup_path)
        return False


def _remove_partial_backup(path: str) -> None:
    """Remove a partial/corrupt backup file left behind by a failed run.

    Without this, cleanup_old_backups() later counts the corrupt file as one
    of the retained ``keep_count`` backup groups and deletes a genuinely
    good older backup to make room for it.
    """
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"已清理失败备份的残留文件: {path}")
    except OSError as cleanup_err:
        logger.warning(f"清理失败备份的残留文件时出错: {path} ({cleanup_err})")


def backup_sessions(sessions_path: str, backup_path: str, dry_run: bool = False) -> bool:
    """
    备份会话目录

    Args:
        sessions_path: 源会话目录路径
        backup_path: 备份文件输出路径（.tar.gz）
        dry_run: 如果为 True，仅打印不执行

    Returns:
        True 表示成功，False 表示失败
    """
    try:
        if not os.path.isdir(sessions_path):
            logger.warning(f"会话目录不存在，跳过: {sessions_path}")
            return True

        if dry_run:
            logger.info(f"[DRY-RUN] 备份会话目录: {sessions_path} -> {backup_path}")
            return True

        logger.info(f"开始备份会话目录: {sessions_path}")

        # 使用 tar.gz 压缩会话目录，保持权限信息
        with tarfile.open(backup_path, 'w:gz') as tar:
            tar.add(sessions_path, arcname='sessions', recursive=True)

        # 设置备份文件权限为 600
        os.chmod(backup_path, 0o600)

        logger.info(f"会话备份完成: {backup_path}")
        return True

    except Exception as e:
        logger.error(f"会话备份失败: {e}", exc_info=True)
        _remove_partial_backup(backup_path)
        return False


def cleanup_old_backups(
    backup_dir: str,
    keep_count: int = 10,
    dry_run: bool = False
) -> None:
    """
    清理旧备份，只保留最近 N 份备份组（db + tar.gz 为一组）

    Args:
        backup_dir: 备份目录
        keep_count: 保留的备份数量
        dry_run: 如果为 True，仅打印不执行
    """
    try:
        backup_path = Path(backup_dir)
        if not backup_path.exists():
            return

        # 按时间戳分组备份文件
        # 时间戳模式：app-20260723T064529Z.db 或 sessions-20260723T064529Z.tar.gz
        timestamp_pattern = re.compile(r'(app|sessions)-([\dT]+Z)\.')
        timestamp_to_files = {}

        for f in backup_path.glob('*'):
            if not f.is_file():
                continue

            match = timestamp_pattern.search(f.name)
            if not match:
                # 手工拷贝的旧备份不归本脚本管理，跳过即可，不必每次告警
                logger.debug(f"跳过非本脚本生成的文件: {f.name}")
                continue

            timestamp = match.group(2)  # 提取 20260723T064529Z
            if timestamp not in timestamp_to_files:
                timestamp_to_files[timestamp] = []
            timestamp_to_files[timestamp].append(f)

        # 按时间戳排序（最新的在前）
        sorted_timestamps = sorted(
            timestamp_to_files.keys(),
            reverse=True
        )

        if len(sorted_timestamps) <= keep_count:
            logger.info(f"备份组数量 {len(sorted_timestamps)} <= 保留数量 {keep_count}，无需清理")
            return

        # 删除超出数量的旧备份组
        deleted_count = 0
        for old_timestamp in sorted_timestamps[keep_count:]:
            for old_backup in timestamp_to_files[old_timestamp]:
                if dry_run:
                    logger.info(f"[DRY-RUN] 删除旧备份: {old_backup}")
                else:
                    old_backup.unlink()
                    logger.info(f"已删除旧备份: {old_backup}")
                deleted_count += 1

        kept_groups = len(sorted_timestamps[:keep_count])
        deleted_groups = len(sorted_timestamps[keep_count:])
        logger.info(f"备份清理完成：保留 {kept_groups} 组备份，删除 {deleted_groups} 组旧备份（{deleted_count} 个文件）")

    except Exception as e:
        logger.error(f"清理旧备份失败: {e}", exc_info=True)


def main():
    args = parse_args()

    # 验证源文件和目录
    if not os.path.exists(args.db_path):
        logger.error(f"数据库文件不存在: {args.db_path}")
        sys.exit(1)

    # 确保备份目录存在
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)

    # 生成时间戳（ISO 8601 格式）
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

    # 备份数据库
    db_backup_path = os.path.join(args.backup_dir, f'app-{timestamp}.db')
    db_ok = backup_database(args.db_path, db_backup_path, args.dry_run)

    # 备份会话目录
    sessions_backup_path = os.path.join(args.backup_dir, f'sessions-{timestamp}.tar.gz')
    sessions_ok = backup_sessions(args.sessions_path, sessions_backup_path, args.dry_run)

    if not (db_ok and sessions_ok):
        logger.error("备份过程中出现失败")
        sys.exit(1)

    # 清理旧备份
    cleanup_old_backups(args.backup_dir, args.keep_count, args.dry_run)

    logger.info(f"备份完成 (保留最近 {args.keep_count} 份)")


if __name__ == '__main__':
    main()
