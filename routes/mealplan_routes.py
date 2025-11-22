from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from collections import deque
import random

from utils.supabase_client import supabase
from services.mealplan_service import optimize_subrecipes  # get_recipe_subrecipes used inside

mealplan_bp = Blueprint("mealplan", __name__)


@mealplan_bp.route("/generate_meal_plan", methods=["POST"])
def generate_meal_plan():
    """
    Body:
    {
      "user_id": "uuid",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "include_weekends": false,
      "meals": {
        "breakfast": "breakfast",
        "lunch": "lunch",
        "dinner": "dinner",
        "snack": "snack"
      }
    }
    """
    data = request.get_json() or {}

    user_id = data.get("user_id")
    start_date_str = data.get("start_date")
    end_date_str = data.get("end_date")
    include_weekends = data.get("include_weekends", False)

    # --- Handle meals mapping safely and flexibly ---

    raw_meals = data.get("meals")

    # Allowed values that correspond to actual recipe fields (could_be_<meal_type>)
    allowed_meal_types = {"breakfast", "lunch", "dinner", "snack"}

    if raw_meals:
        # Keep only entries that:
        # 1. Have a non-empty value
        # 2. That value is one of the allowed meal types
        meals_map = {
            key: value
            for key, value in raw_meals.items()
            if value and isinstance(value, str) and value in allowed_meal_types
        }
    else:
        # Default to all standard meals if no "meals" provided
        meals_map = {
            "breakfast": "breakfast",
            "lunch": "lunch",
            "dinner": "dinner",
            "snack": "snack",
        }

    # ✅ Special case: allow duplicate-type meals like "snack2"
    # For example: "snack2": "snack" will be included and treated as a separate slot
    extra_meals = {
        key: value
        for key, value in (raw_meals or {}).items()
        if value and isinstance(value, str) and value in allowed_meal_types and key not in meals_map
    }
    meals_map.update(extra_meals)

    # 🚨 If the resulting map is empty, return a helpful message
    if not meals_map:
        return jsonify({"error": "At least one valid meal must be selected."}), 400


    # --- Basic validation ---
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "Invalid date format. Use YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    # --- 1) Get all recipes in overlapping weekly menus ---
    recipes_resp = (
        supabase.table("weekly_menu")
        .select("weekly_menu_recipe(recipe(*))")
        .gte("week_end_date", str(start_date))
        .lte("week_start_date", str(end_date))
        .execute()
    )

    recipes = []
    for wm in recipes_resp.data or []:
        for wmr in wm.get("weekly_menu_recipe", []):
            if wmr.get("recipe"):
                recipes.append(wmr["recipe"])

    # Deduplicate by id
    recipes_by_id = {r["id"]: r for r in recipes}
    recipes = list(recipes_by_id.values())

    if not recipes:
        return jsonify({"error": "No recipes found for the selected date range"}), 404

    # --- 2) User preferences ---
    prefs_resp = (
        supabase.table("user_recipe_preferences")
        .select("recipe_id, like, dislike, dont_include")
        .eq("user_id", user_id)
        .execute()
    )
    user_prefs = {p["recipe_id"]: p for p in (prefs_resp.data or [])}

    # --- 3) Score & sort recipes according to preferences ---
    scored_recipes = []
    for r in recipes:
        rid = r["id"]
        pref = user_prefs.get(rid, {})
        # Skip recipes explicitly marked as "don't include"
        if pref.get("dont_include"):
            continue
        score = random.random()
        if pref.get("like"):
            score += 2
        if pref.get("dislike"):
            score -= 5
        scored_recipes.append((score, r))

    for meal_key, meal_type in meals_map.items():
        candidates_for_type = [
            r for _, r in scored_recipes
            if r.get(f"could_be_{meal_type}", False)
        ]
        if not candidates_for_type:
            return jsonify({
                "error": (
                    f"We couldn’t find any available recipes for {meal_type}. "
                    "Some of your preferences may have excluded all options for this meal. "
                    "Please review your selections or try again with different dates."
                )
            }), 400
    if not scored_recipes:
        return jsonify({"error": "No available recipes found after applying user preferences."}), 400

    scored_recipes.sort(key=lambda x: x[0], reverse=True)

    # --- 4) Get latest daily macro target ---
    macro_target_resp = (
        supabase.table("daily_macro_target")
            .select("protein_g, carbs_g, fat_g, kcal_target")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
    )

    if not macro_target_resp.data:
        return jsonify({"error": "No macro target found for this user"}), 400

    target = macro_target_resp.data[0]

    # Extract macros safely
    protein_g = float(target.get("protein_g") or 0.0)
    carbs_g   = float(target.get("carbs_g") or 0.0)
    fat_g     = float(target.get("fat_g") or 0.0)

    # Compute kcal from macros
    computed_kcal = protein_g * 4 + carbs_g * 4 + fat_g * 9

    # Use DB kcal_target only if valid AND consistent
    db_kcal = target.get("kcal_target")

    if isinstance(db_kcal, (int, float)) and db_kcal > 0:
        # optional: accept db_kcal if within 10% difference
        if abs(db_kcal - computed_kcal) / max(computed_kcal, 1) <= 0.10:
            kcal = db_kcal
        else:
            kcal = computed_kcal
    else:
        kcal = computed_kcal

    target_with_kcal = {
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
        "kcal": round(kcal),
    }


    # --- 5) Build meal plan day by day ---
    total_days = (end_date - start_date).days + 1
    days = []
    recent_recipes = deque(maxlen=8)  # To avoid repeating same recipe too often

    for i in range(total_days):
        date = start_date + timedelta(days=i)

        # Skip weekends if required
        if not include_weekends and date.weekday() >= 5:
            continue

        # Choose recipes for each meal slot (breakfast, lunch, etc.)
        recipes_by_meal = {}
        for meal_key, meal_type in meals_map.items():
            # Candidates that match meal_type and are not recently used
            candidates = [
                r for _, r in scored_recipes
                if r.get(f"could_be_{meal_type}", False)
                and r["id"] not in recent_recipes
            ]

            # If we have nothing non-recent, allow repeats
            if not candidates:
                candidates = [
                    r for _, r in scored_recipes
                    if r.get(f"could_be_{meal_type}", False)
                ]

            if not candidates:
                return jsonify({
                    "error": (
                        f"No available recipes found for meal type '{meal_type}'. "
                        f"Please add recipes with could_be_{meal_type}=true "
                        f"or adjust your meals configuration."
                    )
                }), 400

            chosen = random.choice(candidates)
            recipes_by_meal[meal_key] = {
                "recipe_id": chosen["id"],
                "meal_key": meal_key,
                "meal_type": meal_type,
                "recipe_name": chosen.get("name"),
                "photo": chosen.get("photo"),
            }
            recent_recipes.append(chosen["id"])

        # --- Run optimizer for this day ---
        optimized_subs, loss, day_totals = optimize_subrecipes(
            recipes_by_meal, target
        )

        # Group optimized subrecipes per meal_key
        subs_by_meal = {k: [] for k in recipes_by_meal.keys()}
        for sub in optimized_subs:
            meal_name = sub.get("meal_name")  # same as meal_key
            if meal_name in subs_by_meal:
                subs_by_meal[meal_name].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name": sub["name"],
                    "servings": sub["servings"],
                    "macros": sub.get("macros", {}),
                })
        # --- Aggregate macros per recipe (per meal_key) ---
        macros_per_recipe = {}
        for meal_key, sub_list in subs_by_meal.items():
            total_protein = sum(sub["macros"].get("protein", 0) for sub in sub_list)
            total_carbs = sum(sub["macros"].get("carbs", 0) for sub in sub_list)
            total_fat = sum(sub["macros"].get("fat", 0) for sub in sub_list)
            total_kcal = sum(sub["macros"].get("kcal", 0) for sub in sub_list)

            macros_per_recipe[meal_key] = {
                "protein": int(round(total_protein)),
                "carbs": int(round(total_carbs)),
                "fat": int(round(total_fat)),
                "kcal": int(round(total_kcal)),
                    }

        # Build meals list (array) for this day
        meals_list = []
        for meal_key, info in recipes_by_meal.items():
            meals_list.append({
                "meal_key": meal_key,                      # e.g. "breakfast"
                "meal_type": info["meal_type"],            # e.g. "breakfast"
                "recipe_id": info["recipe_id"],
                "recipe_name": info["recipe_name"],
                "photo": info["photo"],
                "macros": macros_per_recipe.get(meal_key, {}),
                "subrecipes": subs_by_meal.get(meal_key, [])
            })

        day_plan = {
            "date": str(date),
            "weekday": date.weekday(),            # 0=Mon, 6=Sun
            "is_weekend": date.weekday() >= 5,
            "macro_error": loss,
            "totals": day_totals,                # includes kcal + tolerance_used
            "meals": meals_list,
        }

        days.append(day_plan)

    return jsonify({
        "user_id": user_id,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "daily_macro_target": target_with_kcal,
        "days": days,
    }), 200


@mealplan_bp.route("/update_meal_plan", methods=["POST"])
def update_meal_plan_endpoint():
    """
    Input:
    {
      "original_plan": {...},  # from /generate_meal_plan
      "change_logs": [ {...}, {...} ]
    }
    Output:
      Updated optimized meal plan JSON
    """
    data = request.get_json()
    original_plan = data.get("original_plan")
    logs = data.get("change_logs", [])

    if not original_plan or not isinstance(logs, list):
        return jsonify({"error": "Missing or invalid input data"}), 400

    from services.mealplan_update_dynamic_service import update_meal_plan
    updated = update_meal_plan(original_plan, logs)

    return jsonify(updated), 200
