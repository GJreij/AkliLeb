from flask import Blueprint, request, jsonify
from utils.supabase_client import supabase

simple_price_bp = Blueprint("simple_price_simulator", __name__)

# Same logic as your original
def get_kcal_discount(kcal: float) -> float:
    min_kcal = 1200
    max_kcal = 3000
    max_discount = 0.15

    if kcal <= min_kcal:
        return 0.0
    if kcal >= max_kcal:
        return max_discount

    ratio = (kcal - min_kcal) / (max_kcal - min_kcal)
    return ratio * max_discount


def fetch_latest_prices():
    price_resp = (
        supabase.table("macro_price")
        .select("*")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not price_resp.data:
        raise ValueError("No pricing data found in macro_price")

    price_data = price_resp.data[0] or {}
    return {
        "protein_price": float(price_data.get("proteing_g_price", 0) or 0),
        "carbs_price": float(price_data.get("carbs_g_price", 0) or 0),
        "fat_price": float(price_data.get("fat_g_price", 0) or 0),
        "day_packaging_price": float(price_data.get("day_packaging_price", 0) or 0),
        "recipe_packaging_price": float(price_data.get("recipe_packaging_price", 0) or 0),
        "subrecipe_packaging_price": float(price_data.get("subrecipe_packaging_price", 0) or 0),
    }


@simple_price_bp.route("/simple_price_simulator", methods=["POST"])
def simple_price_simulator():
    """
    INPUT:
    {
      "protein_g": 150,
      "carbs_g": 200,
      "fat_g": 60,
      "meals_per_day": 3,
      "avg_subrecipes_per_meal": 1.5,

      "apply_kcal_discount": true
    }

    OUTPUT:
    {
      "avg_day_price": ...,
      "breakdown": {...}
    }
    """
    data = request.get_json() or {}

    # Required inputs
    try:
        protein_g = float(data["protein_g"])
        carbs_g = float(data["carbs_g"])
        fat_g = float(data["fat_g"])
        meals_per_day = int(data["meals_per_day"])
        avg_subrecipes_per_meal = float(data["avg_subrecipes_per_meal"])
    except KeyError as e:
        return jsonify({"error": f"Missing field: {str(e)}"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid input types"}), 400

    if meals_per_day <= 0:
        return jsonify({"error": "meals_per_day must be >= 1"}), 400
    if protein_g < 0 or carbs_g < 0 or fat_g < 0:
        return jsonify({"error": "Macros must be >= 0"}), 400
    if avg_subrecipes_per_meal < 0:
        return jsonify({"error": "avg_subrecipes_per_meal must be >= 0"}), 400

    apply_kcal_discount = bool(data.get("apply_kcal_discount", True))

    # Fetch prices
    try:
        prices = fetch_latest_prices()
    except Exception as e:
        return jsonify({"error": f"Failed to fetch pricing data: {str(e)}"}), 500

    # Estimate kcal from macros (standard: P=4, C=4, F=9)
    estimated_kcal = protein_g * 4 + carbs_g * 4 + fat_g * 9

    # Macro cost (per day)
    base_macro_cost = (
        protein_g * prices["protein_price"]
        + carbs_g * prices["carbs_price"]
        + fat_g * prices["fat_price"]
    )

    discount_pct = get_kcal_discount(estimated_kcal) if apply_kcal_discount else 0.0
    macro_cost_after_discount = base_macro_cost * (1 - discount_pct)

    # Packaging costs
    day_packaging = prices["day_packaging_price"]
    recipes_packaging = meals_per_day * prices["recipe_packaging_price"]
    subrecipes_packaging = meals_per_day * avg_subrecipes_per_meal * prices["subrecipe_packaging_price"]

    avg_day_price = round(
        day_packaging + macro_cost_after_discount + recipes_packaging + subrecipes_packaging,
        2
    )

    return jsonify({
        "inputs": {
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
            "meals_per_day": meals_per_day,
            "avg_subrecipes_per_meal": avg_subrecipes_per_meal,
            "estimated_kcal": round(estimated_kcal, 0),
            "apply_kcal_discount": apply_kcal_discount
        },
        "avg_day_price": avg_day_price,
        "breakdown": {
            "prices_used": {
                "protein_price_per_g": prices["protein_price"],
                "carbs_price_per_g": prices["carbs_price"],
                "fat_price_per_g": prices["fat_price"],
                "day_packaging_price": prices["day_packaging_price"],
                "recipe_packaging_price": prices["recipe_packaging_price"],
                "subrecipe_packaging_price": prices["subrecipe_packaging_price"]
            },
            "base_macro_cost": round(base_macro_cost, 2),
            "kcal_discount_pct": round(discount_pct, 4),
            "macro_cost_after_discount": round(macro_cost_after_discount, 2),
            "day_packaging_cost": round(day_packaging, 2),
            "recipes_packaging_cost": round(recipes_packaging, 2),
            "subrecipes_packaging_cost": round(subrecipes_packaging, 2)
        }
    }), 200
