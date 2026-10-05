"""scripts/restore.sh 必须拒绝会话备份里的链接条目，并且拒绝时不动现有会话。"""

import _isolation  # noqa: F401  must precede any app import
import io
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

RESTORE = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"


@unittest.skipUnless(shutil.which("bash") and shutil.which("tar") and shutil.which("python3"), "needs bash, tar, python3")
class RestoreScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="teamboss-restore-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Always an explicit scratch data dir: the script defaults to backend/data.
        self.data_dir = self.tmp / "data"
        (self.data_dir / "sessions").mkdir(parents=True)
        (self.data_dir / "sessions" / "existing.json").write_text("{}", encoding="utf-8")

    def _archive(self, build) -> Path:
        path = self.tmp / "sessions-test.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            build(tar)
        return path

    @staticmethod
    def _add_dir(tar, name):
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        info.mode = 0o700
        tar.addfile(info)

    @staticmethod
    def _add_file(tar, name, data=b"{}"):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))

    def _restore(self, archive: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, AUTO_TEAM_DATA_DIR=str(self.data_dir))
        return subprocess.run(
            ["bash", str(RESTORE), str(archive), "--skip-service-check"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _assert_rejected_and_untouched(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已拒绝恢复", result.stdout)
        self.assertEqual(sorted(p.name for p in (self.data_dir / "sessions").iterdir()), ["existing.json"])
        self.assertEqual([p.name for p in self.data_dir.iterdir() if p.name.startswith(".sessions.restore")], [])

    def test_symlink_entry_is_rejected(self):
        def build(tar):
            self._add_dir(tar, "sessions")
            link = tarfile.TarInfo("sessions/team.json")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/hostname"
            tar.addfile(link)

        self._assert_rejected_and_untouched(self._restore(self._archive(build)))

    def test_hardlink_entry_is_rejected(self):
        def build(tar):
            self._add_dir(tar, "sessions")
            self._add_file(tar, "sessions/a.json")
            link = tarfile.TarInfo("sessions/b.json")
            link.type = tarfile.LNKTYPE
            link.linkname = "sessions/a.json"
            tar.addfile(link)

        self._assert_rejected_and_untouched(self._restore(self._archive(build)))

    def test_parent_traversal_is_still_rejected(self):
        def build(tar):
            self._add_dir(tar, "sessions")
            self._add_file(tar, "sessions/../escape.json")

        self._assert_rejected_and_untouched(self._restore(self._archive(build)))

    def test_plain_sessions_archive_restores(self):
        def build(tar):
            self._add_dir(tar, "sessions")
            self._add_file(tar, "sessions/team.json", b'{"ok": true}')

        result = self._restore(self._archive(build))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.data_dir / "sessions" / "team.json"
        self.assertEqual(restored.read_text(encoding="utf-8"), '{"ok": true}')
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)


    def _make_db(self, path: Path, rows, wal=False):
        conn = sqlite3.connect(path)
        if wal:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
        conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
        conn.commit()
        return conn

    def test_db_restore_drops_stale_wal_and_keeps_consistent_safety_copy(self):
        live = self.data_dir / "app.db"
        # 旧库：WAL 模式，行 old-wal 只存在于未检查点的 -wal 里；连接保持打开，
        # 这样 -wal 文件留在磁盘上（模拟服务异常退出后的现场）。
        conn = self._make_db(live, ["old-base"], wal=True)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("INSERT INTO t VALUES ('old-wal')")
        conn.commit()
        self.addCleanup(conn.close)
        self.assertGreater(Path(str(live) + "-wal").stat().st_size, 0)

        backup = self.tmp / "app-backup.db"
        self._make_db(backup, ["restored-1", "restored-2"]).close()

        result = self._restore(backup)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        check = sqlite3.connect(live)
        try:
            self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(sorted(r[0] for r in check.execute("SELECT v FROM t")), ["restored-1", "restored-2"])
        finally:
            check.close()
        self.assertEqual(live.stat().st_mode & 0o777, 0o600)

        # 打开恢复后的库后，旁边不应有来自旧库的 -wal 内容。
        stale = Path(str(live) + "-wal")
        self.assertFalse(stale.exists() and stale.stat().st_size > 0)

        copies = [p for p in self.data_dir.iterdir() if p.name.startswith("app.db.restore-backup-") and not p.name.endswith(("-wal", "-shm"))]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)
        safety = sqlite3.connect(copies[0])
        try:
            self.assertEqual(safety.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(sorted(r[0] for r in safety.execute("SELECT v FROM t")), ["old-base", "old-wal"])
        finally:
            safety.close()

    def test_db_restore_rejects_corrupt_backup_and_leaves_live_db(self):
        live = self.data_dir / "app.db"
        self._make_db(live, ["keep"]).close()
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"not a sqlite database" * 100)
        result = self._restore(bad)
        self.assertNotEqual(result.returncode, 0)
        check = sqlite3.connect(live)
        try:
            self.assertEqual([r[0] for r in check.execute("SELECT v FROM t")], ["keep"])
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
