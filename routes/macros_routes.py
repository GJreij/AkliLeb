from flask import Blueprint, request, jsonify
import statistics

from config.constants import DIET_MACROS, KCAL_PER_G, MACRO_RANGES
from utils.supabase_client import supabase

macros_bp = Blueprint("macros", __name__)

# -------------------------------
# Defaults for estimation
# -------------------------------
DEFAULT_MEALS_PER_DAY = 3
DEFAULT_AVG_SUBRECIPES_PER_MEAL = 3
DEFAULT_APPLY_KCAL_DISCOUNT = True

import math

def _band(amount: float, pct: float = 0.06, min_width: float = 2.0) -> dict:
    """
    Convert an exact amount into a friendly integer range.
    Example: 23.12 -> {"low": 22, "high": 25}
    """
    half_width = max(min_width / 2, amount * pct)
    low = math.floor(amount - half_width)
    high = math.ceil(amount + half_width)
    if high <= low:
        high = low + 1
    return {"low": low, "high": high}


def estimate_ui_pricing_for_3m1s(
    *,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    total_kcal: float,
    avg_subrecipes_per_meal: float = DEFAULT_AVG_SUBRECIPES_PER_MEAL,
    snack_kcal_share: float = 0.20,     # 15–25% typical; default 20%
    snack_subrecipes: float = 1.0,      # snack simpler than meals
    apply_kcal_discount: bool = DEFAULT_APPLY_KCAL_DISCOUNT,
) -> dict:
    """
    UI-oriented pricing for: 3 meals + 1 snack.

    Uses estimate_day_price() for the true day cost (with 4 containers/day),
    then provides safe UI ranges.
    """
    MEALS = 3
    SNACKS = 1
    containers = MEALS + SNACKS  # 4

    # Get true estimate using your pricing logic
    day_est = estimate_day_price(
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        total_kcal=total_kcal,
        meals_per_day=containers,  # packaging aligned with 4 containers/day
        avg_subrecipes_per_meal=avg_subrecipes_per_meal,
        apply_kcal_discount=apply_kcal_discount,
    )

    exact_day = float(day_est["estimated_day_price"])

    # Average per container (this is your "(≈ $6–7 per meal on average)")
    per_container_exact = exact_day / containers

    # Friendly UI ranges
    day_range = _band(exact_day, pct=0.06, min_width=3.0)
    per_meal_avg_range = _band(per_container_exact, pct=0.08, min_width=1.0)

    # Weekly (7 days)
    exact_week = exact_day * 7
    week_range = _band(exact_week, pct=0.06, min_width=10.0)

    return {
        "scenario": {"meals": MEALS, "snacks": SNACKS, "containers": containers},
        "ranges": {
            "day": day_range,                 # {"low": 22, "high": 25}
            "week": week_range,               # {"low": 155, "high": 175}
            "per_meal_avg": per_meal_avg_range
        },
        "exact": {
            "day": round(exact_day, 2),
            "week": round(exact_week, 2),
            "avg_per_container": round(per_container_exact, 2),
        },
        "ui_copy": {
            "headline": "For a day of 3 meals and 1 snack:",
            "day": f"~ ${day_range['low']}–{day_range['high']} / day",
            "per_meal": f"(≈ ${per_meal_avg_range['low']}–{per_meal_avg_range['high']} per meal on average)",
            "week": f"~ ${week_range['low']}–{week_range['high']} / week",
            "note": "Meals vary in size and macros. Pricing is based on total daily nutrition, not individual dishes.",
        },
        "assumptions": {
            "avg_subrecipes_per_meal": avg_subrecipes_per_meal,
            "snack_kcal_share": max(0.0, min(0.5, float(snack_kcal_share))),
            "snack_subrecipes": snack_subrecipes,
            "apply_kcal_discount": apply_kcal_discount,
        },
        "day_estimate_debug": day_est,  # keep or remove depending on what you want exposed
    }

# -------------------------------
# Pricing helpers
# -------------------------------
def get_kcal_discount(kcal: float) -> float:
    """
    Discount grows linearly from 0% at 1200kcal to 15% at 3000kcal.
    """
    min_kcal = 1200
    max_kcal = 3000
    max_discount = 0.15

    if kcal is None:
        return 0.0

    if kcal <= min_kcal:
        return 0.0
    if kcal >= max_kcal:
        return max_discount

    ratio = (kcal - min_kcal) / (max_kcal - min_kcal)
    return ratio * max_discount


def fetch_latest_prices() -> dict:
    """
    Fetch latest pricing from Supabase macro_price table.
    """
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
        "protein_price_per_g": float(price_data.get("proteing_g_price", 0) or 0),
        "carbs_price_per_g": float(price_data.get("carbs_g_price", 0) or 0),
        "fat_price_per_g": float(price_data.get("fat_g_price", 0) or 0),
        "day_packaging_price": float(price_data.get("day_packaging_price", 0) or 0),
        "recipe_packaging_price": float(price_data.get("recipe_packaging_price", 0) or 0),
        "subrecipe_packaging_price": float(price_data.get("subrecipe_packaging_price", 0) or 0),
    }


def estimate_day_price(
    *,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    total_kcal: float,
    meals_per_day: int = DEFAULT_MEALS_PER_DAY,
    avg_subrecipes_per_meal: float = DEFAULT_AVG_SUBRECIPES_PER_MEAL,
    apply_kcal_discount: bool = DEFAULT_APPLY_KCAL_DISCOUNT,
) -> dict:
    """
    Returns a detailed price estimate (per day) using the same logic as checkout:
    - macro cost based on grams
    - kcal-based discount (optional)
    - day packaging
    - recipe packaging per meal
    - subrecipe packaging based on avg count
    """
    prices = fetch_latest_prices()

    base_macro_cost = (
        protein_g * prices["protein_price_per_g"]
        + carbs_g * prices["carbs_price_per_g"]
        + fat_g * prices["fat_price_per_g"]
    )

    discount_pct = get_kcal_discount(total_kcal) if apply_kcal_discount else 0.0
    macro_cost_after_discount = base_macro_cost * (1 - discount_pct)

    day_packaging = prices["day_packaging_price"]
    recipes_packaging = meals_per_day * prices["recipe_packaging_price"]
    subrecipes_packaging = meals_per_day * avg_subrecipes_per_meal * prices["subrecipe_packaging_price"]

    estimated_day_price = round(
        day_packaging + macro_cost_after_discount + recipes_packaging + subrecipes_packaging,
        2
    )

    return {
        "estimated_day_price": estimated_day_price,
        "assumptions": {
            "meals_per_day": meals_per_day,
            "avg_subrecipes_per_meal": avg_subrecipes_per_meal,
            "apply_kcal_discount": apply_kcal_discount,
        },
        "breakdown": {
            "base_macro_cost": round(base_macro_cost, 2),
            "kcal_discount_pct": round(discount_pct, 4),
            "macro_cost_after_discount": round(macro_cost_after_discount, 2),
            "day_packaging_cost": round(day_packaging, 2),
            "recipes_packaging_cost": round(recipes_packaging, 2),
            "subrecipes_packaging_cost": round(subrecipes_packaging, 2),
        },
        "prices_used": prices,
    }


# -------------------------------
# Input parsing helpers
# -------------------------------
def parse_float(value, field_name, *, allow_zero=False) -> float:
    """
    Safely parse float and return clear error messages.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a number. "
            "Use a dot (.) for decimals, not a comma (,)."
        )

    if allow_zero:
        if v < 0:
            raise ValueError(f"{field_name} must be >= 0.")
    else:
        if v <= 0:
            raise ValueError(f"{field_name} must be greater than 0.")

    return v


def parse_int(value, field_name, *, default=None, min_value=1) -> int:
    if value is None:
        if default is None:
            raise ValueError(f"Missing field: {field_name}")
        return default

    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer.")

    if v < min_value:
        raise ValueError(f"{field_name} must be >= {min_value}.")
    return v


def parse_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return bool(value)


# -------------------------------
# Routes
# -------------------------------
@macros_bp.route("/macros", methods=["GET"])
def get_macros():
    """
    GET /macros?kcal=2200&diet=balanced
    Optional query params for price estimate:
      - meals_per_day (int, default 3)
      - avg_subrecipes_per_meal (float, default 0)
      - apply_kcal_discount (bool, default true)
    """
    kcal = request.args.get("kcal", type=float)
    diet_type = request.args.get("diet", "").lower().strip()

    if not kcal or kcal <= 0:
        return jsonify({"error": "Please provide a positive kcal value"}), 400
    if diet_type not in DIET_MACROS:
        return jsonify({"error": f"Diet type must be one of {list(DIET_MACROS.keys())}"}), 400

    # Optional pricing knobs
    try:
        meals_per_day = parse_int(request.args.get("meals_per_day"), "meals_per_day", default=DEFAULT_MEALS_PER_DAY)
        avg_subrecipes_per_meal = parse_float(
            request.args.get("avg_subrecipes_per_meal", DEFAULT_AVG_SUBRECIPES_PER_MEAL),
            "avg_subrecipes_per_meal",
            allow_zero=True
        )
        apply_kcal_discount = parse_bool(request.args.get("apply_kcal_discount"), default=DEFAULT_APPLY_KCAL_DISCOUNT)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    macros_pct = DIET_MACROS[diet_type]

    macros_grams = {
        macro: round((kcal * pct) / KCAL_PER_G[macro], 1)
        for macro, pct in macros_pct.items()
    }

    # Price estimate
    try:
        price_estimate = estimate_day_price(
            protein_g=float(macros_grams.get("protein", 0) or 0),
            carbs_g=float(macros_grams.get("carbs", 0) or 0),
            fat_g=float(macros_grams.get("fat", 0) or 0),
            total_kcal=float(kcal),
            meals_per_day=meals_per_day,
            avg_subrecipes_per_meal=avg_subrecipes_per_meal,
            apply_kcal_discount=apply_kcal_discount,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to estimate price: {str(e)}"}), 500

    return jsonify({
        "diet_type": diet_type,
        "kcal": float(kcal),
        "macros_percentage": {m: int(pct * 100) for m, pct in macros_pct.items()},
        "macros_grams": macros_grams,
        "price_estimate": price_estimate,
    }), 200

@macros_bp.route("/macros/ui-price", methods=["GET"])
def get_ui_price():
    """
    GET /macros/ui-price?kcal=2200&diet=balanced

    Optional query params:
      - avg_subrecipes_per_meal (float, default DEFAULT_AVG_SUBRECIPES_PER_MEAL)
      - snack_kcal_share (float, default 0.20)   # portion of daily kcal assigned to snack conceptually
      - snack_subrecipes (float, default 1.0)
      - apply_kcal_discount (bool, default true)

    Returns UI-friendly pricing strings + ranges for:
      - week
      - day
      - per-meal average (for 3 meals + 1 snack)
    """
    kcal = request.args.get("kcal", type=float)
    diet_type = request.args.get("diet", "").lower().strip()

    if not kcal or kcal <= 0:
        return jsonify({"error": "Please provide a positive kcal value"}), 400
    if diet_type not in DIET_MACROS:
        return jsonify({"error": f"Diet type must be one of {list(DIET_MACROS.keys())}"}), 400

    # Optional knobs
    try:
        avg_subrecipes_per_meal = parse_float(
            request.args.get("avg_subrecipes_per_meal", DEFAULT_AVG_SUBRECIPES_PER_MEAL),
            "avg_subrecipes_per_meal",
            allow_zero=True,
        )
        snack_kcal_share = parse_float(
            request.args.get("snack_kcal_share", 0.20),
            "snack_kcal_share",
            allow_zero=True,
        )
        snack_subrecipes = parse_float(
            request.args.get("snack_subrecipes", 1.0),
            "snack_subrecipes",
            allow_zero=True,
        )
        apply_kcal_discount = parse_bool(
            request.args.get("apply_kcal_discount"),
            default=DEFAULT_APPLY_KCAL_DISCOUNT
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Compute macros grams from kcal + diet (same logic as /macros)
    macros_pct = DIET_MACROS[diet_type]
    macros_grams = {
        macro: round((kcal * pct) / KCAL_PER_G[macro], 1)
        for macro, pct in macros_pct.items()
    }

    # UI pricing
    try:
        ui_price = estimate_ui_pricing_for_3m1s(
            protein_g=float(macros_grams.get("protein", 0) or 0),
            carbs_g=float(macros_grams.get("carbs", 0) or 0),
            fat_g=float(macros_grams.get("fat", 0) or 0),
            total_kcal=float(kcal),
            avg_subrecipes_per_meal=avg_subrecipes_per_meal,
            snack_kcal_share=snack_kcal_share,
            snack_subrecipes=snack_subrecipes,
            apply_kcal_discount=apply_kcal_discount,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to estimate UI price: {str(e)}"}), 500

    # Keep response focused for the UI
    return jsonify({
        "diet_type": diet_type,
        "kcal": float(kcal),
        "macros_percentage": {m: int(pct * 100) for m, pct in macros_pct.items()},
        "macros_grams": macros_grams,
        "ui_price": ui_price,
    }), 200

@macros_bp.route("/macros/from-grams", methods=["POST"])
def macros_from_grams():
    """
    POST /macros/from-grams
    Body:
    {
      "protein": 150,
      "carbs": 200,
      "fat": 60,

      "meals_per_day": 3,              (optional)
      "avg_subrecipes_per_meal": 1.5,  (optional)
      "apply_kcal_discount": true      (optional)
    }
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    # Required macros
    try:
        protein_g = parse_float(data.get("protein"), "Protein (g)")
        carbs_g = parse_float(data.get("carbs"), "Carbohydrates (g)")
        fat_g = parse_float(data.get("fat"), "Fat (g)")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # Optional pricing knobs
    try:
        meals_per_day = parse_int(data.get("meals_per_day"), "meals_per_day", default=DEFAULT_MEALS_PER_DAY)
        avg_subrecipes_per_meal = parse_float(
            data.get("avg_subrecipes_per_meal", DEFAULT_AVG_SUBRECIPES_PER_MEAL),
            "avg_subrecipes_per_meal",
            allow_zero=True
        )
        apply_kcal_discount = parse_bool(data.get("apply_kcal_discount"), default=DEFAULT_APPLY_KCAL_DISCOUNT)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # kcal calculation
    kcal_protein = protein_g * KCAL_PER_G["protein"]
    kcal_carbs = carbs_g * KCAL_PER_G["carbs"]
    kcal_fat = fat_g * KCAL_PER_G["fat"]
    total_kcal = kcal_protein + kcal_carbs + kcal_fat

    if total_kcal <= 0:
        return jsonify({"error": "Total calories must be greater than 0"}), 400

    # percentages
    pct_protein = kcal_protein / total_kcal
    pct_carbs = kcal_carbs / total_kcal
    pct_fat = kcal_fat / total_kcal

    # sanity checks
    errors = []
    for macro, pct in {"protein": pct_protein, "carbs": pct_carbs, "fat": pct_fat}.items():
        min_pct, max_pct = MACRO_RANGES[macro]
        if not (min_pct <= pct <= max_pct):
            errors.append(
                f"{macro.capitalize()} percentage ({int(pct*100)}%) "
                f"is outside the recommended range "
                f"({int(min_pct*100)}–{int(max_pct*100)}%)."
            )

    if errors:
        return jsonify({"error": "Macro distribution is unrealistic.", "details": errors}), 400

    # price estimate
    try:
        price_estimate = estimate_day_price(
            protein_g=protein_g,
            carbs_g=carbs_g,
            fat_g=fat_g,
            total_kcal=total_kcal,
            meals_per_day=meals_per_day,
            avg_subrecipes_per_meal=avg_subrecipes_per_meal,
            apply_kcal_discount=apply_kcal_discount,
        )
    except Exception as e:
        return jsonify({"error": f"Failed to estimate price: {str(e)}"}), 500

    return jsonify({
        "total_kcal": round(total_kcal),
        "macros_grams": {
            "protein": protein_g,
            "carbs": carbs_g,
            "fat": fat_g,
        },
        "macros_percentage": {
            "protein": round(pct_protein * 100, 1),
            "carbs": round(pct_carbs * 100, 1),
            "fat": round(pct_fat * 100, 1),
        },
        "kcal_breakdown": {
            "protein": round(kcal_protein),
            "carbs": round(kcal_carbs),
            "fat": round(kcal_fat),
        },
        "price_estimate": price_estimate,
    }), 200
