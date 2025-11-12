from datetime import datetime, timedelta
from utils.supabase_client import supabase

class PartnerService:
    def __init__(self):
        self.sb = supabase

    def get_partner_shares(self, partner_id, this_month=False):
        """
        Returns a summary of a partner's earnings (7% share).

        Steps:
          - Fetch all payments linked to this partner.
          - Join each payment to its meal_plan_day to get the 'date'.
          - If this_month=True, only include current month dates.
          - Calculate:
              * shares_acquired (7% of 'paid')
              * shares_pending (7% of 'pending')
              * start_date (earliest meal_plan_day.date)
              * end_date (latest meal_plan_day.date for pending)
        """
        now = datetime.utcnow()
        month_start = datetime(now.year, now.month, 1)
        next_month = datetime(now.year, now.month + 1, 1) if now.month < 12 else datetime(now.year + 1, 1, 1)
        month_end = next_month - timedelta(days=1)

        # --- Step 1: Fetch partner payments
        query = (
            self.sb.table("payment")
            .select("id, amount, status, meal_plan_day_id, created_at")
            .eq("partner_at_order", partner_id)
        )
        if this_month:
            query = query.gte("created_at", month_start.isoformat()).lte("created_at", month_end.isoformat())

        res = query.execute()
        payments = res.data or []

        if not payments:
            return {
                "partner_id": partner_id,
                "shares_acquired": 0.0,
                "shares_pending": 0.0,
                "start_date": month_start.date().isoformat() if this_month else None,
                "end_date": None,
                "message": "No payments found for this partner."
            }

        # --- Step 2: Collect all meal_plan_day_id
        day_ids = [p["meal_plan_day_id"] for p in payments if p.get("meal_plan_day_id")]

        # --- Step 3: Fetch meal_plan_day dates
        days_res = (
            self.sb.table("meal_plan_day")
            .select("id, date")
            .in_("id", day_ids)
            .execute()
        )
        day_map = {d["id"]: d["date"] for d in (days_res.data or [])}

        # --- Step 4: Compute sums and dates
        shares_acquired = 0.0
        shares_pending = 0.0
        paid_dates = []
        pending_dates = []

        for p in payments:
            day_date = day_map.get(p.get("meal_plan_day_id"))
            if not day_date:
                continue

            # restrict to this month if requested
            if this_month:
                d = datetime.fromisoformat(day_date)
                if d < month_start or d > month_end:
                    continue

            share = (p.get("amount") or 0) * 0.07  # 7% share
            if p.get("status") == "paid":
                shares_acquired += share
                paid_dates.append(day_date)
            elif p.get("status") == "pending":
                shares_pending += share
                pending_dates.append(day_date)

        # --- Step 5: Dates
        all_dates = paid_dates + pending_dates
        start_date = min(all_dates) if all_dates else None
        # end_date: last date among pending (if any), else last paid
        end_date = max(pending_dates) if pending_dates else (max(all_dates) if all_dates else None)

        return {
            "partner_id": partner_id,
            "shares_acquired": round(shares_acquired, 2),
            "shares_pending": round(shares_pending, 2),
            "start_date": start_date,
            "end_date": end_date
        }
