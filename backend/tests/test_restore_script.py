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

    def _crash_image(self, path: Path, base_rows, wal_rows, sidecars=("-wal",)) -> int:
        """在 path 留下一份"写进程崩溃"的现场：wal_rows 只存在于 -wal，之后没有任何连接打开。

        不能直接在 path 上留一个打开的连接：本进程持有的共享锁会让被测脚本的连接在关闭时
        无法检查点并删除 -wal，正好把要测的问题掩盖掉。所以在别处写，再把文件原样复制过来。
        另建一张表 u，它的页只在主文件里；返回该页在主文件中的字节偏移。
        """
        writer = self.tmp / f"writer-{path.name}" / "w.db"
        writer.parent.mkdir()
        conn = self._make_db(writer, base_rows, wal=True)
        try:
            conn.execute("CREATE TABLE u (v TEXT)")
            conn.execute("INSERT INTO u VALUES ('main-file-only')")
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in wal_rows])
            conn.commit()
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            root = conn.execute("SELECT rootpage FROM sqlite_master WHERE name = 'u'").fetchone()[0]
            for ext in ("", *sidecars):
                shutil.copyfile(f"{writer}{ext}", f"{path}{ext}")
        finally:
            conn.close()
        self.assertGreater(Path(f"{path}-wal").stat().st_size, 0)
        return (root - 1) * page_size

    def _rows(self, path: Path):
        conn = sqlite3.connect(path)
        try:
            return sorted(r[0] for r in conn.execute("SELECT v FROM t"))
        finally:
            conn.close()

    def _safety_copy(self) -> Path:
        copies = [p for p in self.data_dir.iterdir() if p.name.startswith("app.db.restore-backup-") and not p.name.endswith(("-wal", "-shm"))]
        self.assertEqual(len(copies), 1)
        return copies[0]

    def _assert_damaged_live_db_is_copied_raw_with_its_wal(self, offset, garbage: bytes):
        live = self.data_dir / "app.db"
        u_page = self._crash_image(live, ["old-base"], ["old-wal-only"])
        if offset is None:
            offset = u_page
        header = bytearray(live.read_bytes())
        original = bytes(header[offset:offset + len(garbage)])
        header[offset:offset + len(garbage)] = garbage
        live.write_bytes(bytes(header))
        before_db = live.read_bytes()
        before_wal = Path(f"{live}-wal").read_bytes()

        backup = self.tmp / "app-backup.db"
        self._make_db(backup, ["restored"]).close()
        result = self._restore(backup)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("原样复制", result.stdout)
        self.assertEqual(self._rows(live), ["restored"])

        # 退化路径的副本必须是原现场：主文件没被脚本自己的读取尝试检查点改写，-wal 一并保存。
        copy = self._safety_copy()
        copy_wal = Path(f"{copy}-wal")
        self.assertEqual(copy.read_bytes(), before_db)
        self.assertEqual(copy_wal.read_bytes(), before_wal)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(copy_wal.stat().st_mode & 0o777, 0o600)

        # 修好被破坏的字节后，只在 WAL 里的提交能从副本里读回来。
        repaired = self.tmp / "repaired.db"
        data = bytearray(before_db)
        data[offset:offset + len(garbage)] = original
        repaired.write_bytes(bytes(data))
        shutil.copyfile(copy_wal, f"{repaired}-wal")
        self.assertEqual(self._rows(repaired), ["old-base", "old-wal-only"])

    def test_unreadable_live_db_is_copied_raw_with_its_wal(self):
        # 文件头魔数被破坏：SQLite 报 "file is not a database"，备份接口直接失败。
        self._assert_damaged_live_db_is_copied_raw_with_its_wal(0, b"not sqlite at all")

    def test_malformed_live_db_is_copied_raw_with_its_wal(self):
        # 只在主文件里的 b-tree 页被写坏：备份接口照样"成功"（它只搬页不解析），
        # 快照是坏的，得靠完整性检查拦下来走原样复制。
        self._assert_damaged_live_db_is_copied_raw_with_its_wal(None, b"\xff" * 16)

    def test_db_restore_leaves_no_new_sidecars_beside_backup_file(self):
        self._make_db(self.data_dir / "app.db", ["old"]).close()
        # scripts/backup.py 用备份接口从 WAL 库导出，文件头仍声明 WAL；只读打开这种文件
        # 会在旁边建出 -shm 和空 -wal。
        source = self._make_db(self.tmp / "backup-source.db", ["restored"], wal=True)
        backup = self.tmp / "app-backup.db"
        try:
            dst = sqlite3.connect(backup)
            try:
                source.backup(dst)
            finally:
                dst.close()
        finally:
            source.close()
        self.assertEqual(backup.read_bytes()[18:20], b"\x02\x02")

        result = self._restore(backup)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self._rows(self.data_dir / "app.db"), ["restored"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(backup.name)), [backup.name])

    def test_db_restore_keeps_preexisting_sidecars_beside_backup_file(self):
        self._make_db(self.data_dir / "app.db", ["old"]).close()
        for sidecars in (("-wal",), ("-wal", "-shm")):
            with self.subTest(sidecars=sidecars):
                backup = self.tmp / f"app-backup{len(sidecars)}.db"
                self._crash_image(backup, ["restored-base"], ["restored-wal-only"], sidecars)
                before_wal = Path(f"{backup}-wal").read_bytes()

                result = self._restore(backup)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # 备份旁原有的 -wal 是备份内容的一部分：要被读进恢复结果，且原样留着。
                self.assertEqual(self._rows(self.data_dir / "app.db"), ["restored-base", "restored-wal-only"])
                names = sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(backup.name))
                self.assertEqual(names, sorted(backup.name + ext for ext in ("", *sidecars)))
                self.assertEqual(Path(f"{backup}-wal").read_bytes(), before_wal)

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
