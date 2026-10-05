import _isolation  # noqa: F401  must precede any app import
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import security
from app.database import init_database


class InitialAdminCredentialsTest(unittest.IsolatedAsyncioTestCase):
    async def _run_first_start(self, password: str, api_key: str):
        async def read_setting(_key: str):
            return None

        with (
            patch.object(security, "_read_setting", side_effect=read_setting),
            patch.object(security, "_write_setting", new=AsyncMock()) as write_setting,
            patch.dict(
                os.environ,
                {
                    security.ADMIN_PASSWORD_ENV: password,
                    security.ADMIN_API_KEY_ENV: api_key,
                },
                clear=False,
            ),
        ):
            await security.ensure_admin_credentials_initialized()
            return write_setting

    async def test_rejects_example_password(self):
        with self.assertRaisesRegex(RuntimeError, "公开占位值"):
            await self._run_first_start(
                "change_me_to_a_strong_password_8_chars_min",
                "atk_a_real_random_value",
            )

    async def test_rejects_example_api_key(self):
        with self.assertRaisesRegex(RuntimeError, "公开占位值"):
            await self._run_first_start(
                "a-real-password",
                "atk_change_me_to_a_random_token",
            )

    async def test_rejects_short_password(self):
        with self.assertRaisesRegex(RuntimeError, "至少 8 位"):
            await self._run_first_start("short", "atk_a_real_random_value")

    async def test_accepts_strong_initial_credentials(self):
        write_setting = await self._run_first_start(
            "a-real-password",
            "atk_a_real_random_value",
        )
        self.assertEqual(write_setting.await_count, 2)
        written = {call.args[0]: call.args[1] for call in write_setting.await_args_list}
        self.assertTrue(written[security.ADMIN_PASSWORD_HASH_SETTING].startswith("pbkdf2_sha256$"))
        self.assertEqual(
            written[security.ADMIN_API_KEY_SETTING],
            "atk_a_real_random_value",
        )


class AdminApiKeyRotationTest(unittest.IsolatedAsyncioTestCase):
    async def test_previous_key_has_ten_minute_recovery_window(self):
        old_key = "atk_old_key_with_enough_entropy"
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": data_dir}, clear=False
        ):
            await init_database()
            await security._write_setting(security.ADMIN_API_KEY_SETTING, old_key)

            new_key = await security.rotate_admin_api_key()

            self.assertNotEqual(new_key, old_key)
            self.assertEqual(await security.get_admin_api_key(), new_key)
            await security.require_admin(x_api_key=old_key)
            await security.require_admin(x_api_key=new_key)

            expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            await security._write_setting(
                security.ADMIN_PREVIOUS_API_KEY_EXPIRES_SETTING,
                expired_at,
            )
            with self.assertRaises(HTTPException) as raised:
                await security.require_admin(x_api_key=old_key)
            self.assertEqual(raised.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
