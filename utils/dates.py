# utils/dates.py

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BEIRUT_TZ = ZoneInfo("Asia/Beirut")


def beirut_iso_date(days_from_now: int) -> str:
    """
    Mirrors `beirutISODate` in FrontEnd/akli-web/src/app/order/new/page.tsx.
    Must stay in sync with that function — both enforce the same 48h
    lead-time rule (order creation there, cancellation cutoff here) and a
    divergence would let one side accept a date the other rejects.
    """
    beirut_now = datetime.now(BEIRUT_TZ) + timedelta(days=days_from_now)
    return beirut_now.strftime("%Y-%m-%d")
