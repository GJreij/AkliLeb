from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from utils.supabase_client import supabase

class PartnerService:
    def __init__(self):
        self.sb = supabase

    def get_partner_shares(self, partner_id, this_month=False):
        """
        Returns a summary of partner's payments:
          - shares_acquired: sum(amount) where status='paid'
          - shares_pending: sum(amount) where status='pending'
          - start_date: start of month or first payment ever
          - end_date: latest expected date for pending payments
        """
        now = datetime.utcnow()

        # ---------- 1️⃣ Determine date range ----------
        if this_month:
            month_start = datetime(now.year, now.month, 1)
            next_month = month_start + relativedelta(months=1)
            month_end = next_month - timedelta(days=1)
            date_filter = {
                "gte": month_start.isoformat(),
                "lte": month_end.isoformat()
            }
        else:
            month_start = None
            date_filter = None

        # ---------- 2️⃣ Query payments ----------
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
                "start_date": month_start.date().isoformat() if this_month else None,
                "end_date": None,
                "message": "No payments found for this partner."
            }

        # ---------- 3️⃣ Aggregate sums ----------
        shares_acquired = sum(p["amount"] for p in payments if p.get("status") == "paid")
        shares_pending = sum(p["amount"] for p in payments if p.get("status") == "pending")

        # ---------- 4️⃣ Start / end dates ----------
        start_date = (
            month_start.date().isoformat()
            if this_month
            else min(datetime.fromisoformat(p["created_at"]).date() for p in payments)
        )

        # end_date = latest expected payment date for pending shares
        pending_dates = [
            datetime.fromisoformat(p["created_at"]).date() for p in payments if p.get("status") == "pending"
        ]
        end_date = max(pending_dates).isoformat() if pending_dates else None

        return {
            "partner_id": partner_id,
            "shares_acquired": round(shares_acquired, 2),
            "shares_pending": round(shares_pending, 2),
            "start_date": start_date,
            "end_date": end_date
        }
