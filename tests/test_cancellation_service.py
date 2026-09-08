# tests/test_cancellation_service.py
"""
Regression tests for CancellationService's affiliate-commission voiding.

Bug: approving a cancellation (wallet/refund/no-refund) marked the
meal_plan_day/deliveries rows cancelled but never touched the linked
`payment.commission_amount` — so a cancelled order kept counting toward
the affiliate's earned/owed balance in
AffiliateService.get_affiliate_commission_summary forever.

No real Supabase connection is used — `CancellationService.sb` is replaced
with an in-memory fake that returns pre-canned responses per table and
records every call, so these run offline via:

    python -m unittest tests.test_cancellation_service -v

(run from the mealplanner-flask/ directory)
"""

import os
import sys
import unittest
from unittest.mock import patch

# Make `services`/`utils` importable when run from anywhere, and satisfy
# utils/supabase_client.py's required env vars at import time (never hit
# over the network here — self.sb is swapped out below in every test).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

from services.cancellation_service import CancellationService  # noqa: E402


class FakeResponse:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Minimal stand-in for a supabase-py PostgREST query builder.

    Every chained method call (select/eq/in_/update/order/limit/...) is a
    no-op that returns self; `not_` is a chainable property rather than a
    call. `.execute()` returns the canned response configured for this
    table, regardless of which filters were chained before it — the fake
    doesn't apply filtering itself, so tests set the response to whatever
    the *real* filtered result would be.
    """

    def __init__(self, response, log, table_name):
        self._response = response
        self._log = log
        self._table_name = table_name

    def __getattr__(self, name):
        if name == "not_":
            return self
        def method(*args, **kwargs):
            self._log.append((self._table_name, name, args, kwargs))
            return self
        return method

    def execute(self):
        return self._response


class FakeSupabase:
    def __init__(self, table_responses):
        self._table_responses = table_responses
        self.call_log = []

    def table(self, name):
        response = self._table_responses.get(name, FakeResponse(None))
        return FakeQuery(response, self.call_log, name)

    def rpc(self, name, params=None):
        self.call_log.append(("rpc", name, params, {}))
        return FakeQuery(FakeResponse(None), self.call_log, f"rpc:{name}")

    def update_calls(self, table_name):
        return [c for c in self.call_log if c[0] == table_name and c[1] == "update"]


class VoidCommissionForCancelledDaysTests(unittest.TestCase):
    """Unit tests for the new _void_commission_for_cancelled_days helper."""

    def _service(self, payment_rows):
        svc = CancellationService()
        svc.sb = FakeSupabase({"payment": FakeResponse(payment_rows)})
        return svc

    def test_no_day_ids_returns_none_and_touches_nothing(self):
        svc = self._service(payment_rows=[])
        result = svc._void_commission_for_cancelled_days([])
        self.assertIsNone(result)
        self.assertEqual(svc.sb.call_log, [])

    def test_no_commission_rows_returns_none_and_does_not_update(self):
        svc = self._service(payment_rows=[])
        result = svc._void_commission_for_cancelled_days([10, 11])
        self.assertIsNone(result)
        self.assertEqual(svc.sb.update_calls("payment"), [])

    def test_voids_commission_and_reports_totals(self):
        svc = self._service(payment_rows=[
            {"id": 501, "commission_amount": 12.5},
            {"id": 502, "commission_amount": 7.5},
        ])
        result = svc._void_commission_for_cancelled_days([10, 11])

        self.assertEqual(result, {"voided_count": 2, "voided_amount": 20.0})

        updates = svc.sb.update_calls("payment")
        self.assertEqual(len(updates), 1)
        _, _, args, _ = updates[0]
        self.assertEqual(args[0], {"commission_amount": 0})

        # The subsequent .in_("id", [...]) call is what actually scopes the
        # update to just the voided rows — assert it targets exactly those
        # payment ids, not e.g. every payment row for the cancelled days.
        in_calls = [c for c in svc.sb.call_log if c[0] == "payment" and c[1] == "in_"]
        self.assertIn((501, 502), [tuple(sorted(c[2][1])) for c in in_calls])

    def test_rounds_voided_total_to_cents(self):
        svc = self._service(payment_rows=[
            {"id": 1, "commission_amount": 3.333},
            {"id": 2, "commission_amount": 3.334},
        ])
        result = svc._void_commission_for_cancelled_days([10])
        self.assertEqual(result["voided_amount"], 6.67)

    def test_null_commission_amount_treated_as_zero_in_total(self):
        # Defensive: a row selected without a filter mismatch shouldn't crash
        # if commission_amount somehow comes back null.
        svc = self._service(payment_rows=[{"id": 1, "commission_amount": None}])
        result = svc._void_commission_for_cancelled_days([10])
        self.assertEqual(result, {"voided_count": 1, "voided_amount": 0.0})


class DecideWiresCommissionVoidingTests(unittest.TestCase):
    """Confirms decide() actually calls the voiding helper for approved
    decisions (with the right day_ids) and skips it entirely on rejection —
    this is the wiring that was missing before the fix."""

    def _build_service(self, requested_day_ids, pending_days, deliveries):
        svc = CancellationService()
        svc.sb = FakeSupabase({
            "cancellation_request": FakeResponse({
                "id": 1,
                "meal_plan_id": 555,
                "user_id": "user-1",
                "status": "pending",
                "meal_plan_day_ids": requested_day_ids,
            }),
            "meal_plan_day": FakeResponse(pending_days),
            "deliveries": FakeResponse(deliveries),
        })
        return svc

    def test_approved_decision_calls_void_commission_with_cancelled_day_ids(self):
        svc = self._build_service(
            requested_day_ids=[10, 11],
            pending_days=[
                {"id": 10, "delivery_id": 100, "status": "cancellation_pending"},
                {"id": 11, "delivery_id": 101, "status": "cancellation_pending"},
            ],
            deliveries=[],  # no delivery_slot release needed for this check
        )
        with patch.object(svc, "_void_commission_for_cancelled_days", return_value={"voided_count": 2, "voided_amount": 20.0}) as mock_void, \
             patch.object(svc, "_apply_volume_discount_correction", return_value=None):
            result, status = svc.decide(1, "approved_no_refund", decided_by="admin-1")

        self.assertEqual(status, 200)
        mock_void.assert_called_once_with([10, 11])
        self.assertEqual(result["commission_voided"], {"voided_count": 2, "voided_amount": 20.0})

    def test_rejected_decision_never_calls_void_commission(self):
        svc = self._build_service(
            requested_day_ids=[10],
            pending_days=[{"id": 10, "delivery_id": 100, "status": "cancellation_pending"}],
            deliveries=[],
        )
        with patch.object(svc, "_void_commission_for_cancelled_days") as mock_void:
            result, status = svc.decide(1, "rejected", decided_by="admin-1")

        self.assertEqual(status, 200)
        mock_void.assert_not_called()
        self.assertNotIn("commission_voided", result)


if __name__ == "__main__":
    unittest.main()
