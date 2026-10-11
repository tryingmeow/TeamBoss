"""Admin credentials: first-start checks, API key rotation, and password change."""

import _isolation  # noqa: F401  must precede any app import
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import security
from app.database import init_database
from app.routes import admin as admin_routes


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
    """轮换后只有新 Key 有效：旧 Key 同一刻失效，没有宽限期。"""

    async def test_rotation_revokes_the_old_key_immediately(self):
        old_key = "atk_old_key_with_enough_entropy"
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": data_dir}, clear=False
        ):
            await init_database()
            await security._write_setting(security.ADMIN_API_KEY_SETTING, old_key)

            new_key = await security.rotate_admin_api_key()

            self.assertNotEqual(new_key, old_key)
            self.assertEqual(await security.get_admin_api_key(), new_key)
            await security.require_admin(x_api_key=new_key)
            with self.assertRaises(HTTPException) as raised:
                await security.require_admin(x_api_key=old_key)
            self.assertEqual(raised.exception.status_code, 401)

    async def test_startup_drops_any_leftover_grace_key(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": data_dir}, clear=False
        ):
            await init_database()
            await security._write_setting("admin_api_key_previous", "atk_leftover_grace_key")

            await init_database()

            self.assertIsNone(await security._read_setting("admin_api_key_previous"))


class AdminPasswordChangeRotatesKeyTest(unittest.IsolatedAsyncioTestCase):
    """改密码同时换 Key：拿到过旧 Key 的人改完密码后进不了后台。"""

    async def test_password_change_revokes_old_key_immediately(self):
        from app.routes import admin as admin_routes

        old_key = "atk_old_key_with_enough_entropy"
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": data_dir}, clear=False
        ), patch.object(admin_routes, "log_operation", new=AsyncMock()) as log:
            await init_database()
            await security._write_setting(security.ADMIN_PASSWORD_HASH_SETTING, security._password_hash("correct-password"))
            await security._write_setting(security.ADMIN_API_KEY_SETTING, old_key)

            result = await admin_routes.update_admin_password(
                admin_routes.ChangeAdminPasswordRequest(
                    current_password="correct-password", new_password="brand-new-password"
                )
            )

            new_key = result["api_key"]
            self.assertEqual(result["status"], "ok")
            self.assertTrue(new_key.startswith("atk_"))
            self.assertNotEqual(new_key, old_key)
            self.assertEqual(result["api_key_prefix"], admin_routes._api_key_prefix(new_key))
            self.assertEqual(await security.get_admin_api_key(), new_key)
            await security.require_admin(x_api_key=new_key)
            with self.assertRaises(HTTPException) as raised:
                await security.require_admin(x_api_key=old_key)
            self.assertEqual(raised.exception.status_code, 401)
            self.assertTrue(await security.verify_admin_password("brand-new-password"))
            self.assertFalse(await security.verify_admin_password("correct-password"))
            # 审计日志只记前缀，不记完整 Key。
            detail = log.await_args.args[3]
            self.assertIn("api_key_rotated", detail)
            self.assertNotIn(new_key, detail)

    async def test_wrong_current_password_keeps_key(self):
        old_key = "atk_old_key_with_enough_entropy"
        with tempfile.TemporaryDirectory() as data_dir, patch.dict(
            os.environ, {"AUTO_TEAM_DATA_DIR": data_dir}, clear=False
        ):
            await init_database()
            await security._write_setting(security.ADMIN_PASSWORD_HASH_SETTING, security._password_hash("correct-password"))
            await security._write_setting(security.ADMIN_API_KEY_SETTING, old_key)

            with self.assertRaises(HTTPException) as raised:
                await security.change_admin_password("wrong-password", "brand-new-password")
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(await security.get_admin_api_key(), old_key)
            await security.require_admin(x_api_key=old_key)


class ChangePasswordWrongCurrentTest(unittest.IsolatedAsyncioTestCase):
    """错误的当前密码是表单校验失败，不能用 401：前端会把带管理员 key 的 401 当成 key 失效并登出。"""

    def setUp(self):
        self.store = {security.ADMIN_PASSWORD_HASH_SETTING: security._password_hash("correct-password")}

        async def read(key):
            return self.store.get(key)

        async def write(key, value):
            self.store[key] = value

        for target, new in (("_read_setting", read), ("_write_setting", write)):
            p = patch.object(security, target, side_effect=new)
            p.start()
            self.addCleanup(p.stop)
        lp = patch.object(admin_routes, "log_operation", new=AsyncMock())
        self.log = lp.start()
        self.addCleanup(lp.stop)

    def _req(self, current, new="brand-new-password"):
        return admin_routes.ChangeAdminPasswordRequest(current_password=current, new_password=new)

    async def test_wrong_current_password_is_400_and_password_unchanged(self):
        before = self.store[security.ADMIN_PASSWORD_HASH_SETTING]
        with self.assertRaises(HTTPException) as ctx:
            await admin_routes.update_admin_password(self._req("wrong-password"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "当前密码不正确")
        self.assertEqual(self.store[security.ADMIN_PASSWORD_HASH_SETTING], before)
        self.assertTrue(await security.verify_admin_password("correct-password"))


if __name__ == "__main__":
    unittest.main()
