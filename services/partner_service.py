from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta
from utils.supabase_client import supabase

class PartnerService:
    def __init__(self):
        self.sb = supabase

    def get_partner_shares(self, partner_id, this_month=False):
        """
        Returns the partner's acquired (paid) and pending (prévu) shares.

        Params:
          - partner_id (str)
          - this_month (bool): if True, restricts to current month; else all time
        """

        # ---------- 1️⃣ Date boundaries ----------
        now = datetime.utcnow()
        if this_month:
            month_start = datetime(now.year, now.month, 1)
            # last day of month: first day of next month minus one day
            next_month = month_start + relativedelta(months=1)
            month_end = next_month - timedelta(days=1)
            date_filter = {
                "gte": month_start.isoformat(),
                "lte": month_end.isoformat()
            }
        else:
            month_start = None
            date_filter = None

        # ---------- 2️⃣ Fetch all payments for this partner ----------
        query = self.sb.table("payment").select("*").eq("partner_at_order", partner_id)

        if date_filter:
            query = query.gte("created_at", date_filter["gte"]).lte("created_at", date_filter["lte"])

        res = query.execute()
        payments = res.data or []

        if not payments:
            return {
                "partner_id": partner_id,
                "shares_acquired": 0.0,
                "shares_pending": 0.0,
                "next_payout_date": None,
                "start_date": month_start.date().isoformat() if this_month else None,
                "message": "No payments found for this partner."
            }

        # ---------- 3️⃣ Aggregate ----------
        shares_acquired = sum(p["amount"] for p in payments if p.get("status") == "paid")
        shares_pending = sum(p["amount"] for p in payments if p.get("status") == "pending")

        # ---------- 4️⃣ Dates ----------
        start_date = (
            month_start.date().isoformat()
            if this_month
            else min(p["created_at"] for p in payments)
        )

        next_payout_date = (
            (month_end.date().isoformat() if this_month else None)
        )

        return {
            "partner_id": partner_id,
            "shares_acquired": round(shares_acquired, 2),
            "shares_pending": round(shares_pending, 2),
            "next_payout_date": next_payout_date,
            "start_date": start_date
        }
