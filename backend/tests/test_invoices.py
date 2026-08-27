"""发票缓存与对账。

两条铁律：
1. 上游 invoice 对象里的 customer_email / customer_name / customer_address
   等 PII 绝不落库——sanitize 输出必须是固定的安全字段集合。
2. 对账在原币种内完成，容差 max(推算值 1%, 原币 1.00)。
"""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import database as app_database
from app.services.invoices import (
    classify_latest_invoice,
    refresh_invoices_if_stale_sync,
    sanitize_invoice,
    store_invoices_sync,
)

SAFE_KEYS = {
    "invoice_id", "number", "status", "currency", "amount_due", "amount_paid",
    "period_start", "period_end", "description", "hosted_invoice_url", "created_at",
}


def _raw_invoice(**overrides):
    raw = {
        "id": "in_test001",
        "number": "ABCD-0001",
        "status": "paid",
        "currency": "thb",
        "amount_due": 56100,
        "amount_paid": 56100,
        "customer_email": "someone@example.com",
        "customer_name": "SOME NAME",
        "customer_address": {"country": "TH"},
        "created": 1782222190,
        "period_start": 1782222190,
        "period_end": 1782222190,
        "hosted_invoice_url": "https://invoice.stripe.com/i/abc",
        "invoice_pdf": "https://pay.stripe.com/invoice/abc/pdf",
        "lines": {
            "data": [
                {
                    "description": "2 seat x ChatGPT Business Subscription",
                    "amount": 156000,
                    "period": {"start": 1782222190, "end": 1784814190},
                }
            ]
        },
        "total": 56100,
        "subtotal": 156000,
    }
    raw.update(overrides)
    return raw


class SanitizeInvoiceTest(unittest.TestCase):
    def test_output_is_exactly_the_safe_field_set(self):
        row = sanitize_invoice(_raw_invoice())
        self.assertEqual(set(row.keys()), SAFE_KEYS)
        for value in row.values():
            self.assertNotIn("someone@example.com", str(value))
            self.assertNotIn("SOME NAME", str(value))
        # invoice_pdf 有单独的下载语义，界面只留 hosted_invoice_url。
        self.assertEqual(row["hosted_invoice_url"], "https://invoice.stripe.com/i/abc")

    def test_amounts_convert_minor_units_and_periods_come_from_line_items(self):
        row = sanitize_invoice(_raw_invoice())
        self.assertEqual(row["amount_due"], 561.0)
        self.assertEqual(row["amount_paid"], 561.0)
        self.assertEqual(row["currency"], "THB")
        # 发票级 period_start == created 是常态，真实账期在行项目里。
        self.assertTrue(row["period_end"].startswith("2026-07-23"))
        self.assertNotEqual(row["period_start"], row["period_end"])
        self.assertIn("ChatGPT Business", row["description"])

    def test_zero_decimal_currency_is_not_divided(self):
        row = sanitize_invoice(_raw_invoice(currency="jpy", amount_due=5610, amount_paid=5610))
        self.assertEqual(row["amount_due"], 5610.0)

    def test_rejects_objects_without_id_and_non_https_urls(self):
        self.assertIsNone(sanitize_invoice({"status": "paid"}))
        self.assertIsNone(sanitize_invoice(None))
        row = sanitize_invoice(_raw_invoice(hosted_invoice_url="javascript:alert(1)"))
        self.assertIsNone(row["hosted_invoice_url"])


class ClassifyLatestInvoiceTest(unittest.TestCase):
    def _latest(self, **overrides):
        latest = {"status": "paid", "currency": "THB", "amount_due": 561.0, "amount_paid": 561.0}
        latest.update(overrides)
        return latest

    def test_within_tolerance_is_a_match(self):
        # 容差 = max(1% × 561, 1.00) = 5.61
        rec, display, diff = classify_latest_invoice(self._latest(), 556.0, "THB")
        self.assertEqual(rec, "match")
        self.assertEqual(display, 561.0)
        self.assertAlmostEqual(diff, 5.0)

    def test_over_and_under_beyond_tolerance(self):
        rec, _, diff = classify_latest_invoice(self._latest(), 500.0, "THB")
        self.assertEqual(rec, "over")
        self.assertAlmostEqual(diff, 61.0)

        rec, _, diff = classify_latest_invoice(
            self._latest(amount_paid=400.0), 500.0, "THB"
        )
        self.assertEqual(rec, "under")
        self.assertAlmostEqual(diff, -100.0)

    def test_small_amounts_use_the_one_unit_floor(self):
        # 推算 50，1% 才 0.5——低于 1.00 下限，差 0.9 仍算吻合。
        rec, _, _ = classify_latest_invoice(
            self._latest(amount_paid=50.9), 50.0, "THB"
        )
        self.assertEqual(rec, "match")

    def test_open_invoice_is_unpaid_and_shows_amount_due(self):
        rec, display, diff = classify_latest_invoice(
            self._latest(status="open", amount_paid=0.0), 561.0, "THB"
        )
        self.assertEqual(rec, "unpaid")
        self.assertEqual(display, 561.0)
        self.assertIsNone(diff)

    def test_currency_mismatch_or_missing_estimate_cannot_be_compared(self):
        rec, display, _ = classify_latest_invoice(self._latest(), 561.0, "USD")
        self.assertIsNone(rec)
        self.assertEqual(display, 561.0)

        rec, _, _ = classify_latest_invoice(self._latest(), None, "THB")
        self.assertIsNone(rec)


class FakeInvoiceClient:
    calls = 0

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"data": [_raw_invoice()]}

    def get_invoices(self, limit=6):
        FakeInvoiceClient.calls += 1
        return self.payload


class InvoiceStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        db_dir_patch = patch.object(
            app_database, "get_db_dir", return_value=self.tmpdir.name
        )
        db_dir_patch.start()
        self.addCleanup(db_dir_patch.stop)
        asyncio.run(app_database.init_database())

        self.conn = sqlite3.connect(app_database.get_db_path())
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute(
            """INSERT INTO teams (id, name, status, access_token, device_id, created_at, updated_at)
               VALUES ('team-1', 'Team 1', 'active', 'tok', 'dev', '2026-08-01', '2026-08-01')"""
        )
        self.conn.commit()
        FakeInvoiceClient.calls = 0

    def test_store_upserts_by_invoice_id_and_row_has_no_pii(self):
        store_invoices_sync(self.conn, "team-1", [_raw_invoice()], "2026-08-17T00:00:00+00:00")
        store_invoices_sync(
            self.conn, "team-1",
            [_raw_invoice(status="void")],
            "2026-08-17T01:00:00+00:00",
        )
        rows = self.conn.execute("SELECT * FROM invoices WHERE team_id = 'team-1'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "void")
        row_text = str(dict(rows[0]))
        self.assertNotIn("someone@example.com", row_text)
        self.assertNotIn("SOME NAME", row_text)

    def test_refresh_respects_the_24h_gate(self):
        run_call = lambda fn, *a, **kw: fn(*a, **kw)
        client = FakeInvoiceClient()

        err = refresh_invoices_if_stale_sync(
            self.conn, client, "team-1", "2026-08-17T00:00:00+00:00", run_call
        )
        self.assertIsNone(err)
        self.assertEqual(FakeInvoiceClient.calls, 1)

        # 12 小时后再问：缓存仍新鲜，不发请求。
        refresh_invoices_if_stale_sync(
            self.conn, client, "team-1", "2026-08-17T12:00:00+00:00", run_call
        )
        self.assertEqual(FakeInvoiceClient.calls, 1)

        # 过了 24 小时才再拉。
        refresh_invoices_if_stale_sync(
            self.conn, client, "team-1", "2026-08-18T00:00:01+00:00", run_call
        )
        self.assertEqual(FakeInvoiceClient.calls, 2)

    def test_upstream_error_does_not_touch_the_sync_marker(self):
        run_call = lambda fn, *a, **kw: fn(*a, **kw)
        client = FakeInvoiceClient(payload={"error": "boom"})
        err = refresh_invoices_if_stale_sync(
            self.conn, client, "team-1", "2026-08-17T00:00:00+00:00", run_call
        )
        self.assertEqual(err, "boom")
        marker = self.conn.execute(
            "SELECT invoices_synced_at FROM teams WHERE id = 'team-1'"
        ).fetchone()[0]
        self.assertIsNone(marker)


if __name__ == "__main__":
    unittest.main()
