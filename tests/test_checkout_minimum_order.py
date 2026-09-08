# tests/test_checkout_minimum_order.py
"""
Tests for the per-day minimum-order-price floor in checkout_summary.py.

Business rule (per the actual request that shaped this): the floor applies
PER DAY, not to the order total — a single very cheap day (e.g. one small
snack) still ties up its own delivery slot, so it must not get a free pass
just because other days in the same order are full price.

_apply_minimum_order_fee is a pure function — unit-tested directly. The
route itself is exercised through Flask's test client with supabase/promo/
volume-discount/delivery-fee dependencies mocked out (no live DB), to lock
in that a tiny day gets bumped independently of a normal day in the same
order, and that the aggregate total still reconciles with the per-day sum
(this second part matters because order_service.py bills off the per-day
total_price_with_delivery values, not the aggregate field).

Run from the mealplanner-flask/ directory:

    python -m unittest tests.test_checkout_minimum_order -v
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

import flask  # noqa: E402
from routes.checkout_summary import checkout_bp, _apply_minimum_order_fee  # noqa: E402


class ApplyMinimumOrderFeeTests(unittest.TestCase):
    def test_below_minimum_tops_up_to_the_floor(self):
        price, fee = _apply_minimum_order_fee(3.0, 5.0)
        self.assertEqual(price, 5.0)
        self.assertEqual(fee, 2.0)

    def test_at_minimum_is_untouched(self):
        price, fee = _apply_minimum_order_fee(5.0, 5.0)
        self.assertEqual(price, 5.0)
        self.assertEqual(fee, 0.0)

    def test_above_minimum_is_untouched(self):
        price, fee = _apply_minimum_order_fee(42.5, 5.0)
        self.assertEqual(price, 42.5)
        self.assertEqual(fee, 0.0)

    def test_zero_threshold_disables_the_floor(self):
        # macro_price.minimum_order_price is nullable — a 0/None threshold
        # (e.g. before it's ever configured) must never distort a real price.
        price, fee = _apply_minimum_order_fee(3.0, 0)
        self.assertEqual(price, 3.0)
        self.assertEqual(fee, 0.0)

    def test_none_threshold_disables_the_floor(self):
        price, fee = _apply_minimum_order_fee(3.0, None)
        self.assertEqual(price, 3.0)
        self.assertEqual(fee, 0.0)

    def test_fee_rounds_to_cents(self):
        price, fee = _apply_minimum_order_fee(3.333, 5.0)
        self.assertEqual(price, 5.0)
        self.assertEqual(fee, 1.67)


# ─── Route-level: per-day independence + aggregate reconciliation ─────────────

class _FakeQuery:
    """Chainable no-op query builder; only .execute() matters, returning
    canned data regardless of which filters were chained before it."""
    def __init__(self, data):
        self._data = data

    def __getattr__(self, name):
        def method(*args, **kwargs):
            return self
        return method

    def execute(self):
        return SimpleNamespace(data=self._data)


PRICE_ROW = {
    "proteing_g_price": 0.05,
    "carbs_g_price": 0.02,
    "fat_g_price": 0.03,
    "day_packaging_price": 0.5,
    "recipe_packaging_price": 0.3,
    "subrecipe_packaging_price": 0.1,
    "delivery_price": 2.0,
    "minimum_order_price": 5.0,
}

NO_PROMO_RESULT = {
    "status": "no_code",
    "discount_amount": 0,
    "final_price": 0.0,
    "promo_message": "",
    "affiliate_id": None,
    "commission_rate": None,
    "waives_delivery": False,
}


def _make_test_client():
    app = flask.Flask(__name__)
    app.register_blueprint(checkout_bp)
    return app.test_client()


def _table_router(name):
    return _FakeQuery({"macro_price": [PRICE_ROW], "wallet_transactions": []}.get(name, []))


class CheckoutSummaryPerDayMinimumTests(unittest.TestCase):
    def setUp(self):
        self.client = _make_test_client()

    @patch("routes.checkout_summary.log_event")
    @patch("routes.checkout_summary.validate_and_apply_promo_code", return_value=NO_PROMO_RESULT)
    @patch("routes.checkout_summary.apply_volume_discount", return_value={"discount_amount": 0.0, "rule": None})
    @patch("routes.checkout_summary.resolve_delivery_fee_per_day", return_value=(2.0, False))
    @patch("routes.checkout_summary.supabase")
    def test_tiny_day_is_floored_independently_of_a_normal_day(
        self, mock_supabase, mock_resolve_delivery, mock_volume, mock_promo, mock_log_event
    ):
        mock_supabase.table.side_effect = _table_router

        plan = {
            "start_date": "2026-09-20",
            "end_date": "2026-09-21",
            "days": [
                {
                    # Normal day — real meal, well above the $5 floor on its own.
                    "date": "2026-09-20",
                    "totals": {"kcal": 2320, "protein": 200, "carbs": 200, "fat": 80},
                    "meals": [{
                        "recipe_id": 1, "meal_type": "lunch",
                        "macros": {"protein": 200, "carbs": 200, "fat": 80, "kcal": 2320},
                        "subrecipes": [],
                    }],
                },
                {
                    # Tiny snack day — the actual scenario from the bug report.
                    "date": "2026-09-21",
                    "totals": {"kcal": 62, "protein": 1, "carbs": 10, "fat": 2},
                    "meals": [{
                        "recipe_id": 2, "meal_type": "snack",
                        "macros": {"protein": 1, "carbs": 10, "fat": 2, "kcal": 62},
                        "subrecipes": [],
                    }],
                },
            ],
        }

        resp = self.client.post("/checkout_summary", json={
            "user_id": "test-user", "final_plan": plan,
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        bd = body["price_breakdown"]
        daily = bd["daily_breakdown"]

        normal_day, tiny_day = daily[0], daily[1]

        # The normal day is untouched by the floor.
        self.assertEqual(normal_day["minimum_order_fee"], 0.0)

        # The tiny day gets bumped up to the $5 floor (before its own delivery
        # fee stacks on top) — the whole point of this being per-day, not
        # gated by the fact that the other day in this same order is fine.
        self.assertGreater(tiny_day["minimum_order_fee"], 0.0)
        self.assertGreaterEqual(
            tiny_day["total_price"] + tiny_day["minimum_order_fee"], 5.0 - 0.005
        )

        self.assertEqual(bd["minimum_order"]["days_affected"], 1)
        self.assertAlmostEqual(bd["minimum_order"]["fee_applied"], tiny_day["minimum_order_fee"], places=2)

        # The aggregate total must reconcile with the per-day sum — this is
        # what order_service._create_payment_record actually bills off, so a
        # mismatch here would mean the checkout screen and the real charge
        # disagree.
        expected_total = round(sum(d["total_price_with_delivery"] for d in daily), 2)
        self.assertEqual(bd["final_price"], expected_total)

    @patch("routes.checkout_summary.log_event")
    @patch("routes.checkout_summary.validate_and_apply_promo_code", return_value=NO_PROMO_RESULT)
    @patch("routes.checkout_summary.apply_volume_discount", return_value={"discount_amount": 0.0, "rule": None})
    @patch("routes.checkout_summary.resolve_delivery_fee_per_day", return_value=(2.0, False))
    @patch("routes.checkout_summary.supabase")
    def test_all_normal_days_never_trigger_the_floor(
        self, mock_supabase, mock_resolve_delivery, mock_volume, mock_promo, mock_log_event
    ):
        mock_supabase.table.side_effect = _table_router

        plan = {
            "start_date": "2026-09-20", "end_date": "2026-09-20",
            "days": [{
                "date": "2026-09-20",
                "totals": {"kcal": 2320, "protein": 200, "carbs": 200, "fat": 80},
                "meals": [{
                    "recipe_id": 1, "meal_type": "lunch",
                    "macros": {"protein": 200, "carbs": 200, "fat": 80, "kcal": 2320},
                    "subrecipes": [],
                }],
            }],
        }

        resp = self.client.post("/checkout_summary", json={
            "user_id": "test-user", "final_plan": plan,
        })
        body = resp.get_json()
        bd = body["price_breakdown"]

        self.assertFalse(bd["minimum_order"]["is_applied"])
        self.assertEqual(bd["minimum_order"]["days_affected"], 0)
        self.assertEqual(bd["minimum_order"]["fee_applied"], 0.0)


if __name__ == "__main__":
    unittest.main()
