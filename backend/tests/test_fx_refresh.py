import _isolation  # noqa: F401  must precede any app import
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.services import fx
from app.services.fx import FxRefreshError, get_fx_config, refresh_fx_rates, refresh_fx_rates_safely, save_fx_rates


def _ok_response(payload):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = payload
    return resp


class FxRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        p = patch.object(app_database, "get_db_dir", return_value=self.tmpdir.name)
        p.start()
        self.addCleanup(p.stop)
        asyncio.run(app_database.init_database())

    def test_success_saves_only_supported_currencies(self):
        payload = {"result": "success", "rates": {"USD": 1, "GBP": 0.5, "ZZZ": 9.0}}
        with patch.object(fx.requests, "get", return_value=_ok_response(payload)) as get:
            count = asyncio.run(refresh_fx_rates())
        self.assertEqual(count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], fx.FX_API_TIMEOUT_SECONDS)
        config = asyncio.run(get_fx_config())
        self.assertEqual(config["rates"]["GBP"], 0.5)
        self.assertNotIn("ZZZ", config["rates"])
        self.assertIsNotNone(config["fx_updated_at"])

    def test_failure_keeps_old_rates_and_does_not_raise_in_safe_wrapper(self):
        asyncio.run(save_fx_rates({"USD": 1.0, "GBP": 0.11}))
        with patch.object(fx.requests, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(FxRefreshError):
                asyncio.run(refresh_fx_rates())
            with self.assertLogs(fx.logger, "WARNING"):
                self.assertFalse(asyncio.run(refresh_fx_rates_safely()))
        self.assertEqual(asyncio.run(get_fx_config())["rates"]["GBP"], 0.11)

    def test_unsuccessful_api_result_keeps_old_rates(self):
        asyncio.run(save_fx_rates({"USD": 1.0, "GBP": 0.11}))
        with patch.object(fx.requests, "get", return_value=_ok_response({"result": "error"})):
            self.assertFalse(asyncio.run(refresh_fx_rates_safely()))
        self.assertEqual(asyncio.run(get_fx_config())["rates"]["GBP"], 0.11)

    def test_only_if_stale_skips_fresh_rates(self):
        asyncio.run(save_fx_rates({"USD": 1.0, "GBP": 0.11}))
        with patch.object(fx.requests, "get") as get:
            self.assertFalse(asyncio.run(refresh_fx_rates_safely(only_if_stale=True)))
        get.assert_not_called()

    def test_staleness_check(self):
        now = datetime.now(timezone.utc)
        self.assertTrue(fx._fx_is_stale(None))
        self.assertTrue(fx._fx_is_stale((now - timedelta(hours=25)).isoformat()))
        self.assertFalse(fx._fx_is_stale((now - timedelta(hours=1)).isoformat()))


if __name__ == "__main__":
    unittest.main()
