from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from collections import deque
import random

from utils.supabase_client import supabase
from services.mealplan_service import optimize_subrecipes

mealplan_bp = Blueprint("mealplan", __name__)


@mealplan_bp.route("/generate_meal_plan", methods=["POST"])
def generate_meal_plan():
    data = request.get_json() or {}

    # -------------------------------------------------------------
    # 1. Parse input
    # -------------------------------------------------------------
    user_id = data.get("user_id")
    start_date_str = data.get("start_date")
    end_date_str = data.get("end_date")
    include_weekends = data.get("include_weekends", False)
    raw_meals = data.get("meals")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except:
        return jsonify({"error": "Invalid date format"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    # -------------------------------------------------------------
    # 2. Build meals_map (includes support for snack2, snack3…)
    # -------------------------------------------------------------
    allowed_meal_types = {"breakfast", "lunch", "dinner", "snack"}

    if raw_meals:
        meals_map = {
            k: v for k, v in raw_meals.items()
            if v in allowed_meal_types
        }
    else:
        meals_map = {
            "breakfast": "breakfast",
            "lunch": "lunch",
            "dinner": "dinner",
            "snack": "snack",
        }

    # -------------------------------------------------------------
    # 3. Fetch weekly-menu recipes
    # -------------------------------------------------------------
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
            recipe = wmr.get("recipe")
            if recipe:
                recipes.append(recipe)

    recipes_by_id = {r["id"]: r for r in recipes}
    recipes = list(recipes_by_id.values())

    if not recipes:
        return jsonify({"error": "No recipes found"}), 404

    # -------------------------------------------------------------
    # 4. Fetch user preferences
    # -------------------------------------------------------------
    prefs_resp = (
        supabase.table("user_recipe_preferences")
        .select("recipe_id, like, dislike, dont_include")
        .eq("user_id", user_id)
        .execute()
    )
    user_prefs = {p["recipe_id"]: p for p in (prefs_resp.data or [])}

    # -------------------------------------------------------------
    # 5. Score recipes
    # -------------------------------------------------------------
    scored_recipes = []
    for r in recipes:
        rid = r["id"]
        pref = user_prefs.get(rid, {})

        if pref.get("dont_include"):
            continue

        score = random.random()
        if pref.get("like"): score += 2
        if pref.get("dislike"): score -= 5

        scored_recipes.append((score, r))

    if not scored_recipes:
        return jsonify({"error": "All recipes were excluded"}), 400

    scored_recipes.sort(key=lambda x: x[0], reverse=True)

    # -------------------------------------------------------------
    # 6. Fetch macro target
    # -------------------------------------------------------------
    macro_target_resp = (
        supabase.table("daily_macro_target")
        .select("protein_g, carbs_g, fat_g, kcal_target")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not macro_target_resp.data:
        return jsonify({"error": "No macro target"}), 400

    target = macro_target_resp.data[0]

    protein_g = float(target.get("protein_g") or 0)
    carbs_g = float(target.get("carbs_g") or 0)
    fat_g = float(target.get("fat_g") or 0)

    computed_kcal = protein_g * 4 + carbs_g * 4 + fat_g * 9
    db_kcal = target.get("kcal_target")

    if isinstance(db_kcal, (int, float)) and db_kcal > 0:
        if abs(db_kcal - computed_kcal) / max(computed_kcal, 1) <= 0.1:
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

    # -------------------------------------------------------------
    # 7. FAIL-SAFE MEAL SELECTION ENGINE
    # -------------------------------------------------------------

    # Independent rotation per meal slot
    recent_recipes = {
        meal_key: deque(maxlen=4)
        for meal_key in meals_map.keys()
    }

    def get_candidates(meal_type, history):
        """
        Multi-layer fail-safe selection.
        Always returns a non-empty list.
        """

        # L1 — strict filtering
        preferred = [
            r for _, r in scored_recipes
            if r.get(f"could_be_{meal_type}", False)
            and r["id"] not in history
            and not user_prefs.get(r["id"], {}).get("dont_include", False)
        ]
        if preferred:
            return preferred

        # L2 — allow repeated + disliked
        relaxed = [
            r for _, r in scored_recipes
            if r.get(f"could_be_{meal_type}", False)
        ]
        if relaxed:
            return relaxed

        # L3 — ignore meal_type constraint completely
        any_recipe = [r for _, r in scored_recipes]
        if any_recipe:
            return any_recipe

        # L4 — emergency fallback: return top-scored recipe only
        return [scored_recipes[0][1]]

    # -------------------------------------------------------------
    # 8. Build plan day-by-day
    # -------------------------------------------------------------
    total_days = (end_date - start_date).days + 1
    days = []

    for i in range(total_days):
        date = start_date + timedelta(days=i)

        if not include_weekends and date.weekday() >= 5:
            continue

        recipes_by_meal = {}

        # Select recipes with full fail-safe protection
        for meal_key, meal_type in meals_map.items():
            history = recent_recipes[meal_key]

            candidates = get_candidates(meal_type, history)
            chosen = random.choice(candidates)

            recipes_by_meal[meal_key] = {
                "recipe_id": chosen["id"],
                "meal_key": meal_key,
                "meal_type": meal_type,
                "recipe_name": chosen.get("name"),
                "photo": chosen.get("photo"),
            }

            history.append(chosen["id"])

        # ---------------------------------------------------------
        # 9. Optimize macros
        # ---------------------------------------------------------
        optimized_subs, loss, day_totals = optimize_subrecipes(
            recipes_by_meal,
            target_with_kcal
        )

        # Group subrecipes
        subs_by_meal = {k: [] for k in recipes_by_meal}
        for sub in optimized_subs:
            mk = sub["meal_name"]
            if mk in subs_by_meal:
                subs_by_meal[mk].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name": sub["name"],
                    "servings": sub["servings"],
                    "macros": sub.get("macros", {})
                })

        # Compute macros per recipe
        macros_per_recipe = {}
        for meal_key, sub_list in subs_by_meal.items():
            total_p = sum(s["macros"].get("protein", 0) for s in sub_list)
            total_c = sum(s["macros"].get("carbs", 0) for s in sub_list)
            total_f = sum(s["macros"].get("fat", 0) for s in sub_list)
            total_k = sum(s["macros"].get("kcal", 0) for s in sub_list)

            macros_per_recipe[meal_key] = {
                "protein": int(round(total_p)),
                "carbs": int(round(total_c)),
                "fat": int(round(total_f)),
                "kcal": int(round(total_k)),
            }

        # Build meals list
        meals_list = []
        for meal_key, info in recipes_by_meal.items():
            meals_list.append({
                "meal_key": meal_key,
                "meal_type": info["meal_type"],
                "recipe_id": info["recipe_id"],
                "recipe_name": info["recipe_name"],
                "photo": info["photo"],
                "macros": macros_per_recipe.get(meal_key, {}),
                "subrecipes": subs_by_meal.get(meal_key, [])
            })

        days.append({
            "date": str(date),
            "weekday": date.weekday(),
            "is_weekend": date.weekday() >= 5,
            "macro_error": loss,
            "totals": day_totals,
            "meals": meals_list,
        })

    return jsonify({
        "user_id": user_id,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "daily_macro_target": target_with_kcal,
        "days": days,
    }), 200
