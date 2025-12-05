from flask import Blueprint, request, jsonify
import statistics
from utils.supabase_client import supabase
from services.promo_service import validate_and_apply_promo_code
checkout_bp = Blueprint("checkout", __name__)

def get_kcal_discount(kcal):
    min_kcal = 1200
    max_kcal = 3000
    max_discount = 0.15

    if kcal <= min_kcal:
        return 0.0
    if kcal >= max_kcal:
        return max_discount
    
    ratio = (kcal - min_kcal) / (max_kcal - min_kcal)
    return ratio * max_discount



@checkout_bp.route("/checkout_summary", methods=["POST"])
def checkout_summary():
    """
    Input:
    {
      "user_id": "uuid",
      "final_plan": { ... }   # from /generate_meal_plan or /update_meal_plan
    }
    Output:
    {
      "user_id": "...",
      "total_meals": int,
      "macro_summary": { ... },
      "price_breakdown": { ... }
    }
    """
    data = request.get_json()
    user_id = data.get("user_id")
    plan = data.get("final_plan")
    promo_code = data.get("promo_code")
    if not user_id or not plan:
        return jsonify({"error": "Missing user_id or final_plan"}), 400

    days = plan.get("days", [])
    if not days:
        return jsonify({"error": "Plan is empty"}), 400

    # ------------------------------------------------------------------
    # STEP 1 — Fetch the most recent macro price from Supabase
    # ------------------------------------------------------------------
    try:
        price_resp = (
            supabase.table("macro_price")
            .select("*")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not price_resp.data:
            raise ValueError("No pricing data found")
        price_data = price_resp.data[0]
    except Exception as e:
        return jsonify({"error": f"Failed to fetch pricing data: {str(e)}"}), 500

    protein_price = price_data.get("proteing_g_price", 0) or 0
    carbs_price = price_data.get("carbs_g_price", 0) or 0
    fat_price = price_data.get("fat_g_price", 0) or 0
    day_packaging_price = price_data.get("day_packaging_price", 0) or 0
    recipe_packaging_price = price_data.get("recipe_packaging_price", 0) or 0
    subrecipe_packaging_price = price_data.get("subrecipe_packaging_price", 0) or 0

    # ------------------------------------------------------------------
    # STEP 2 — Aggregate macros & compute price dynamically
    # ------------------------------------------------------------------
    kcal_values, protein_values, carbs_values, fat_values = [], [], [], []
    total_meals = 0
    total_price = 0
    daily_price_details = []  # keep track of per-day cost breakdown

    for day in days:
        totals = day.get("totals", {})
        day_price = day_packaging_price

        # Add macros for global averages
        if totals:
            kcal_values.append(totals.get("kcal", 0))
            protein_values.append(totals.get("protein", 0))
            carbs_values.append(totals.get("carbs", 0))
            fat_values.append(totals.get("fat", 0))

        for meal in day.get("meals", []):
            total_meals += 1
            p = meal["macros"].get("protein", 0)
            c = meal["macros"].get("carbs", 0)
            f = meal["macros"].get("fat", 0)

            # Compute base macro cost
            base_macro_cost = p * protein_price + c * carbs_price + f * fat_price

            # Apply kcal-based surcharge percentage
            discount_pct = get_kcal_discount(totals.get("kcal", 0))
            macro_cost = base_macro_cost * (1 - discount_pct)


            recipe_cost = recipe_packaging_price
            sub_pack_cost = len(meal.get("subrecipes", [])) * subrecipe_packaging_price

            meal_price = macro_cost + recipe_cost + sub_pack_cost
            day_price += meal_price

        total_price += day_price
        daily_price_details.append({
            "date": day["date"],
            "total_price": round(day_price, 2),
            "meals": len(day.get("meals", []))
        })
    promo_result = validate_and_apply_promo_code(
    user_id=user_id,
    promo_code_str=promo_code,
    total_price=total_price
    )
    # Compute discount ratio (how much to scale each day's price)
    if promo_result["status"] == "valid" and total_price > 0:
        discount_ratio = promo_result["final_price"] / total_price
    else:
        discount_ratio = 1.0

    # Build a discounted version of daily_price_details
    discounted_daily_price_details = []
    for day in daily_price_details:
        original_day_price = day["total_price"]
        discounted_day_price = round(original_day_price * discount_ratio, 2)

        discounted_daily_price_details.append({
            **day,
            "original_total_price": original_day_price,   # optional, for transparency
            "total_price": discounted_day_price           # <-- the one used by payment
        })
    # ------------------------------------------------------------------
    # STEP 3 — Calculate average macros
    # ------------------------------------------------------------------
    avg_kcal = round(statistics.mean(kcal_values), 1) if kcal_values else 0
    avg_protein = round(statistics.mean(protein_values), 1) if protein_values else 0
    avg_carbs = round(statistics.mean(carbs_values), 1) if carbs_values else 0
    avg_fat = round(statistics.mean(fat_values), 1) if fat_values else 0

    # ------------------------------------------------------------------
    # STEP 4 — Build response JSON
    # ------------------------------------------------------------------
    summary = {
        "user_id": user_id,
        "total_meals": total_meals,
        "macro_summary": {
            "avg_kcal": avg_kcal,
            "avg_protein": avg_protein,
            "avg_carbs": avg_carbs,
            "avg_fat": avg_fat,
        },
        "price_breakdown": {
            "protein_price_per_g": protein_price,
            "carbs_price_per_g": carbs_price,
            "fat_price_per_g": fat_price,
            "day_packaging_price": day_packaging_price,
            "recipe_packaging_price": recipe_packaging_price,
            "subrecipe_packaging_price": subrecipe_packaging_price,
            "total_price_before_discount": round(total_price, 2),
            "discount_amount": promo_result["discount_amount"],
            "final_price": promo_result["final_price"],
            "promo_code_status": promo_result["status"],
            "promo_code_used" : promo_code,
            "promo_message": promo_result["promo_message"],
            "promo_code_id": promo_result.get("promo_code_id"),

            "daily_breakdown": discounted_daily_price_details
        }
    }

    return jsonify(summary), 200
