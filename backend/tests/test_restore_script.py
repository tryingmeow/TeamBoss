"""scripts/restore.sh 必须拒绝会话备份里的链接条目，并且拒绝时不动现有会话。"""

import _isolation  # noqa: F401  must precede any app import
import io
import os
import shutil
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


if __name__ == "__main__":
    unittest.main()
