# services/promo_service.py
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from utils.supabase_client import supabase


def _normalize_code(code: str) -> str:
    # Keep it simple; you can also enforce uppercase everywhere if you want.
    return code.strip()


def _get_user_partner_id(user_id: str) -> Optional[str]:
    """
    Returns the most recent partner_id for a client, or None.
    """
    res = (
        supabase.table("partner_client_link")
        .select("partner_id")
        .eq("client_id", user_id)
        .order("start_date", desc=True)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0].get("partner_id")
    return None


def _pick_applicable_promo(
    *,
    user_id: str,
    code: str,
) -> Optional[Dict[str, Any]]:
    """
    Deterministic promo resolution:
      1) user-scoped promo for that user
      2) partner-scoped promo for user's current partner
      3) global promo

    Returns the selected promo row dict or None.
    """
    # Normalize once (caller already normalized; keep defensive)
    code = _normalize_code(code)

    partner_id = _get_user_partner_id(user_id)

    # Priority 1: user-scoped
    user_res = (
        supabase.table("promo_codes")
        .select("*")
        .eq("is_active", True)
        .eq("scope", "user")
        .eq("user_id", user_id)
        .eq("code", code)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if user_res.data:
        return user_res.data[0]

    # Priority 2: partner-scoped
    if partner_id:
        partner_res = (
            supabase.table("promo_codes")
            .select("*")
            .eq("is_active", True)
            .eq("scope", "partner")
            .eq("partner_id", partner_id)
            .eq("code", code)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if partner_res.data:
            return partner_res.data[0]

    # Priority 3: global
    global_res = (
        supabase.table("promo_codes")
        .select("*")
        .eq("is_active", True)
        .eq("scope", "global")
        .eq("code", code)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if global_res.data:
        return global_res.data[0]

    return None


def validate_and_apply_promo_code(
    user_id: str,
    promo_code_str: Optional[str],
    total_price: float,
) -> Dict[str, Any]:
    """
    Validates and applies a promo code.
    - Supports same `code` across many rows by resolving deterministically:
      user -> partner -> global.
    - Keeps your existing usage + date + min order logic.
    """
    # 0) No code provided
    if not promo_code_str or promo_code_str.strip() == "":
        return {
            "status": "no_code",
            "discount_amount": 0,
            "final_price": round(float(total_price or 0), 2),
            "promo_message": "",
        }

    code = _normalize_code(promo_code_str)
    total_price = float(total_price or 0)

    # 1) Resolve which promo row applies (deterministic)
    promo = _pick_applicable_promo(user_id=user_id, code=code)
    if not promo:
        return {
            "status": "invalid",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": "Promo code is invalid.",
        }

    promo_id = promo["id"]

    # 2) Date validity
    today = date.today()

    if promo.get("start_date") and today < date.fromisoformat(promo["start_date"]):
        return {
            "status": "not_started",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": f"This promo code is not active until {promo['start_date']}.",
        }

    if promo.get("end_date") and today > date.fromisoformat(promo["end_date"]):
        return {
            "status": "expired",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": "This promo code has expired.",
        }

    # 3) Global usage limit
    if promo.get("max_global_uses") is not None:
        usage_res = (
            supabase.table("promo_code_usage")
            .select("id", count="exact")
            .eq("promo_code_id", promo_id)
            .execute()
        )
        if (usage_res.count or 0) >= int(promo["max_global_uses"]):
            return {
                "status": "max_global_reached",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": "This promo code has reached its maximum number of uses.",
            }

    # 4) Per-user usage limit
    if promo.get("max_uses_per_user") is not None:
        user_usage = (
            supabase.table("promo_code_usage")
            .select("id", count="exact")
            .eq("promo_code_id", promo_id)
            .eq("user_id", user_id)
            .execute()
        )
        if (user_usage.count or 0) >= int(promo["max_uses_per_user"]):
            return {
                "status": "max_user_reached",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": "You have already used this promo code the maximum number of times.",
            }

    # 5) Minimum order value
    if promo.get("min_order_value") is not None:
        min_order_value = float(promo["min_order_value"])
        if total_price < min_order_value:
            return {
                "status": "order_value_too_low",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": f"Minimum order value for this promo is €{min_order_value}.",
            }

    # 6) Discount calculation
    discount_value = float(promo.get("discount_value") or 0)
    discount = 0.0

    discount_type = promo.get("discount_type")

    if discount_type == "percentage":
        discount = total_price * (discount_value / 100.0)
        msg = f"Promo applied! You saved {discount_value}%."
    elif discount_type == "fixed":
        discount = discount_value
        msg = f"Promo applied! You saved €{discount_value}."
    else:
        # Unknown discount_type; treat as no discount but "applied"
        discount = 0.0
        msg = "Promo applied."

    final_price = max(total_price - discount, 0.0)

    return {
        "status": "valid",
        "discount_amount": round(discount, 2),
        "final_price": round(final_price, 2),
        "promo_code_id": promo_id,
        "promo_message": msg,
    }
