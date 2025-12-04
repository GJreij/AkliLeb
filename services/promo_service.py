from datetime import date
from utils.supabase_client import supabase


def validate_and_apply_promo_code(user_id, promo_code_str, total_price):
    # CASE 0 — No promo code
    if not promo_code_str or promo_code_str.strip() == "":
        return {
            "status": "no_code",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": ""
        }

    promo_code_str = promo_code_str.strip()

    # 1. Fetch promo code
    res = supabase.table("promo_codes") \
        .select("*") \
        .eq("code", promo_code_str) \
        .eq("is_active", True) \
        .execute()

    if not res.data:
        return {
            "status": "invalid",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": "Promo code is invalid."
        }

    promo = res.data[0]
    promo_id = promo["id"]

    # 2. Date validity
    today = date.today()

    if promo.get("start_date") and today < date.fromisoformat(promo["start_date"]):
        return {
            "status": "not_started",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": f"This promo code is not active until {promo['start_date']}."
        }

    if promo.get("end_date") and today > date.fromisoformat(promo["end_date"]):
        return {
            "status": "expired",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": "This promo code has expired."
        }

    # 3. Global usage limit
    if promo.get("max_global_uses"):
        usage_res = supabase.table("promo_code_usage") \
            .select("id", count="exact") \
            .eq("promo_code_id", promo_id) \
            .execute()

        if usage_res.count >= promo["max_global_uses"]:
            return {
                "status": "max_global_reached",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": "This promo code has reached its maximum number of uses."
            }

    # 4. Per-user usage limit
    if promo.get("max_uses_per_user"):
        user_usage = supabase.table("promo_code_usage") \
            .select("id", count="exact") \
            .eq("promo_code_id", promo_id) \
            .eq("user_id", user_id) \
            .execute()

        if user_usage.count >= promo["max_uses_per_user"]:
            return {
                "status": "max_user_reached",
                "discount_amount": 0,
                "final_price": round(total_price, 2),
                "promo_message": "You have already used this promo code the maximum number of times."
            }

    # 5. Minimum order value check
    if promo.get("min_order_value") and total_price < float(promo["min_order_value"]):
        return {
            "status": "order_value_too_low",
            "discount_amount": 0,
            "final_price": round(total_price, 2),
            "promo_message": f"Minimum order value for this promo is ${promo['min_order_value']}."
        }

    # 6. Discount calculation
    discount_value = float(promo.get("discount_value") or 0)
    discount = 0

    if promo["discount_type"] == "percentage":
        discount = total_price * (discount_value / 100)
        msg = f"Promo applied! You saved {discount_value}%."
    elif promo["discount_type"] == "fixed":
        discount = discount_value
        msg = f"Promo applied! You saved €{discount_value}."
    else:
        msg = "Promo applied."

    final_price = max(total_price - discount, 0)

    return {
        "status": "valid",
        "discount_amount": round(discount, 2),
        "final_price": round(final_price, 2),
        "promo_code_id": promo_id,
        "promo_message": msg
    }
