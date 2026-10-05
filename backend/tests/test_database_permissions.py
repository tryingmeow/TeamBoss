import _isolation  # noqa: F401  must precede any app import
import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database


class DatabasePermissionsTest(unittest.TestCase):
    def test_first_start_enforces_private_permissions_even_with_open_umask(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(database, "get_db_dir", return_value=tmpdir):
                previous_umask = os.umask(0o022)
                try:
                    asyncio.run(database.init_database())
                finally:
                    os.umask(previous_umask)

                db_mode = stat.S_IMODE(os.stat(database.get_db_path()).st_mode)
                sessions_mode = stat.S_IMODE(os.stat(database.get_sessions_dir()).st_mode)
                data_mode = stat.S_IMODE(os.stat(tmpdir).st_mode)

        self.assertEqual(db_mode, 0o600)
        self.assertEqual(sessions_mode, 0o700)
        self.assertEqual(data_mode, 0o700)


if __name__ == "__main__":
    unittest.main()
