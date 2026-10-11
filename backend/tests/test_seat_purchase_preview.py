"""Official quotes are sanitized, currency-aware, and never purchase seats."""
import _isolation  # noqa: F401
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from app.chatgpt_client import ChatGPTClient
from app.main import app
from app.routes import seat_purchase_preview as route
from app.services import seat_purchase_preview as service


def fixtures(currency="THB", exponent=2):
    subscription = {
        "billing_currency": currency.lower(), "billing_period": "monthly",
        "seat_capacity": [
            {"type": "default", "paid": 2, "available": 0},
            {"type": "prolite", "paid": 0, "available": 0},
        ],
    }
    pricing = {"currency_config": {"symbol_code": currency, "minor_unit_exponent": exponent}}
    def recurring(premium, amount):
        return {
            "price_interval": "month",
            "seat_quantities": [{"seat_type": "default", "quantity": 2}, {"seat_type": "prolite", "quantity": premium}],
            "discount_amount": 99900,
            "amount_due": {"amount": amount, "tax_amount": 0, "amount_excluding_tax": amount},
        }
    quote = {
        "currency": currency.lower(), "amount_due": {"amount": 1089406, "tax_amount": 0},
        "current_recurring": recurring(0, 56100),
        "proposed_recurring": recurring(5, 2006100),
        "change_effective_at": "now", "billing_adjustment": {"type": "proration"},
        "quote_id": None,
    }
    return subscription, pricing, quote


class QuoteServiceTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, data=None, seat_type="prolite", count=5):
        subscription, pricing, quote = deepcopy(data or fixtures())
        client = Mock(spec=ChatGPTClient)
        client.get_subscription.return_value = subscription
        client.get_billing_pricing_config.return_value = pricing
        client.preview_seat_purchase.return_value = quote
        async def run(method, *args):
            return method(*args)
        with patch.object(service, "run_chatgpt_call", side_effect=run):
            result = await service.fetch_seat_purchase_preview(client, seat_type, count)
        return result, client

    async def test_quote_amounts_and_calls(self):
        result, client = await self.fetch()
        self.assertEqual(result["due_now"], {"amount": 10894.06, "tax_amount": 0})
        self.assertEqual(result["current_recurring"], {"period": "monthly", "amount": 561, "discount": 999})
        self.assertEqual(result["proposed_recurring"], {"period": "monthly", "amount": 20061, "discount": 999})
        self.assertEqual(result["baseline_quantities"], {"default": 2, "prolite": 0})
        self.assertEqual(result["proposed_quantities"], {"default": 2, "prolite": 5})
        self.assertEqual([call[0] for call in client.mock_calls], ["get_subscription", "get_billing_pricing_config", "preview_seat_purchase"])
        client.preview_seat_purchase.assert_called_once_with({"default": 2, "prolite": 5})
        self.assertEqual(set(result), {"currency", "minor_unit_exponent", "quoted_at", "seat_type", "additional_seats", "baseline_quantities", "proposed_quantities", "current_recurring", "proposed_recurring", "due_now"})

    async def test_currency_exponent_zero_and_three(self):
        for currency, exponent in [("JPY", 0), ("KWD", 3)]:
            with self.subTest(currency=currency):
                result, _ = await self.fetch(fixtures(currency, exponent))
                self.assertEqual(result["due_now"]["amount"], 1089406 / 10 ** exponent)
                self.assertEqual(result["minor_unit_exponent"], exponent)

    async def test_default_increase_preserves_premium(self):
        data = fixtures()
        data[0]["seat_capacity"][1]["paid"] = 4
        data[2]["current_recurring"]["seat_quantities"][1]["quantity"] = 4
        data[2]["proposed_recurring"]["seat_quantities"] = [{"seat_type": "default", "quantity": 7}, {"seat_type": "prolite", "quantity": 4}]
        result, client = await self.fetch(data, seat_type="default")
        client.preview_seat_purchase.assert_called_once_with({"default": 7, "prolite": 4})
        self.assertEqual(result["baseline_quantities"], {"default": 2, "prolite": 4})

    async def test_capacity_only_needs_paid_and_response_is_allowlisted(self):
        data = fixtures()
        for entry in data[0]["seat_capacity"]:
            entry.pop("available")
        data[2]["private_metadata"] = "must-not-escape"
        data[2]["current_recurring"]["private_metadata"] = "must-not-escape"
        result, _ = await self.fetch(data)
        self.assertNotIn("must-not-escape", str(result))

    async def test_incomplete_malformed_capacity_never_calls_preview(self):
        bad = [None, [], [{"type": "default", "paid": 2, "available": 0}]]
        for field, value in [("paid", True), ("paid", "2"), ("paid", -1), ("paid", None), ("type", "usage_based")]:
            capacity = fixtures()[0]["seat_capacity"]
            capacity[0][field] = value
            bad.append(capacity)
        capacity = fixtures()[0]["seat_capacity"]
        bad.append(capacity + [capacity[0]])
        for capacity in bad:
            client = Mock(spec=ChatGPTClient)
            with self.subTest(capacity=capacity), patch.object(service, "run_chatgpt_call", new=AsyncMock(return_value={"seat_capacity": capacity})):
                with self.assertRaises(service.QuoteUnavailable):
                    await service.fetch_seat_purchase_preview(client, "prolite", 5)
                client.preview_seat_purchase.assert_not_called()

    async def test_malformed_stale_quotes_and_upstream_errors(self):
        for transform in [
            lambda s, p, q: q.update(error="upstream unavailable", status_code=401),
            lambda s, p, q: q.update(currency="USD"),
            lambda s, p, q: q.update(change_effective_at="next_cycle"),
            lambda s, p, q: q["amount_due"].update(amount=True),
            lambda s, p, q: q["amount_due"].pop("tax_amount"),
            lambda s, p, q: q["current_recurring"]["seat_quantities"][0].update(quantity=3),
            lambda s, p, q: q["proposed_recurring"]["seat_quantities"][1].update(quantity=6),
            lambda s, p, q: q["proposed_recurring"].update(price_interval="year"),
            lambda s, p, q: q["proposed_recurring"].pop("discount_amount"),
            lambda s, p, q: p["currency_config"].pop("minor_unit_exponent"),
            lambda s, p, q: p["currency_config"].update(minor_unit_exponent=True),
            lambda s, p, q: p["currency_config"].update(symbol_code="USD"),
        ]:
            data = fixtures()
            transform(*data)
            with self.assertRaises(service.QuoteUnavailable):
                await self.fetch(data)


class QuoteClientTests(unittest.TestCase):
    def test_only_posts_preview_with_fresh_identifiers(self):
        client = ChatGPTClient("test-token", "test-team", "test-device")
        client.session = Mock()
        client.session.post.return_value.status_code = 200
        client.session.post.return_value.json.return_value = fixtures()[2]
        for _ in range(2):
            self.assertEqual(client.preview_seat_purchase({"default": 2, "prolite": 5}), fixtures()[2])
        calls = client.session.post.call_args_list
        for call in calls:
            self.assertEqual(call.args, ("https://chatgpt.com/backend-api/subscriptions/update/preview",))
            self.assertFalse(call.kwargs["allow_redirects"])
            body = call.kwargs["json"]
            self.assertEqual(body["account_id"], "test-team")
            self.assertEqual(body["updated_seat_quantities"], [{"seat_type": "default", "quantity": 2}, {"seat_type": "prolite", "quantity": 5}])
            UUID(body["flow_id"])
            UUID(body["mutation_attempt_id"])
        self.assertNotEqual(calls[0].kwargs["json"]["flow_id"], calls[1].kwargs["json"]["flow_id"])
        self.assertEqual([call[0] for call in client.session.mock_calls if call[0] == "post"], ["post", "post"])

    def test_error_does_not_contain_raw_text(self):
        client = ChatGPTClient("test-token", "test-team", "test-device")
        client.session = Mock()
        client.session.post.side_effect = RuntimeError("private upstream diagnostic")
        self.assertEqual(client.preview_seat_purchase({"default": 2, "prolite": 5}), {"error": "seat_purchase_quote_unavailable"})

    def test_redirects_and_auth_errors_keep_only_status(self):
        for status in [302, 401, 403, 500]:
            client = ChatGPTClient("test-token", "test-team", "test-device")
            client.session = Mock()
            response = client.session.post.return_value
            response.status_code = status
            response.reason = "private upstream diagnostic"
            response.url = "https://chatgpt.com/private"
            self.assertEqual(client.preview_seat_purchase({"default": 2, "prolite": 5}), {"error": "seat_purchase_quote_unavailable", "status_code": status})
            response.json.assert_not_called()


class QuoteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, body, authenticated=True):
        headers = {"X-API-Key": "test-admin-key"} if authenticated else {}
        with patch("app.security.get_admin_api_key", new=AsyncMock(return_value="test-admin-key")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/api/teams/test-team/seat-purchase-preview", json=body, headers=headers)

    async def test_admin_auth_required_before_any_fetch(self):
        with patch.object(route, "get_team_client", new=AsyncMock()) as get_client:
            response = await self.request({"seat_type": "prolite", "additional_seats": 5}, False)
        self.assertEqual(response.status_code, 401)
        get_client.assert_not_awaited()

    async def test_request_validation(self):
        for seat_type, count in [("usage_based", 1), ("default", 0), ("default", 101), ("default", True), ("prolite", "5"), ("prolite", 1.5)]:
            with patch.object(route, "get_team_client", new=AsyncMock()) as get_client:
                response = await self.request({"seat_type": seat_type, "additional_seats": count})
            self.assertEqual(response.status_code, 422)
            get_client.assert_not_awaited()

    async def test_quote_failure_is_generic_and_not_admin_401(self):
        with patch.object(route, "get_team_client", new=AsyncMock()), patch.object(route, "fetch_seat_purchase_preview", new=AsyncMock(side_effect=service.QuoteUnavailable("private upstream diagnostic"))):
            response = await self.request({"seat_type": "prolite", "additional_seats": 5})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "seat_purchase_quote_unavailable")
        self.assertNotIn("private", response.text)

    async def test_success_route_returns_normalized_quote_without_mutation(self):
        subscription, pricing, quote = fixtures()
        upstream = Mock(spec=ChatGPTClient)
        upstream.get_subscription.return_value = subscription
        upstream.get_billing_pricing_config.return_value = pricing
        upstream.preview_seat_purchase.return_value = quote
        async def run(method, *args):
            return method(*args)
        with patch.object(route, "get_team_client", new=AsyncMock(return_value=upstream)), patch.object(service, "run_chatgpt_call", side_effect=run):
            response = await self.request({"seat_type": "prolite", "additional_seats": 5})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["due_now"]["amount"], 10894.06)
        self.assertEqual([call[0] for call in upstream.mock_calls], ["get_subscription", "get_billing_pricing_config", "preview_seat_purchase"])


if __name__ == "__main__":
    unittest.main()
