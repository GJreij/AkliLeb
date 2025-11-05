from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from collections import deque
import random

from utils.supabase_client import supabase
from services.mealplan_service import get_recipe_subrecipes, optimize_subrecipes

mealplan_bp = Blueprint("mealplan", __name__)


@mealplan_bp.route("/generate_meal_plan", methods=["POST"])
def generate_meal_plan():
    data = request.get_json()
    user_id = data.get("user_id")
    start_date = datetime.strptime(data.get("start_date"), "%Y-%m-%d").date()
    end_date = datetime.strptime(data.get("end_date"), "%Y-%m-%d").date()

    # 1️⃣ Get all recipes within overlapping weekly menus
    recipes_resp = (
        supabase.table("weekly_menu")
        .select("weekly_menu_recipe(recipe(*))")
        .gte("week_end_date", str(start_date))
        .lte("week_start_date", str(end_date))
        .execute()
    )

    recipes = []
    for wm in recipes_resp.data:
        for wmr in wm.get("weekly_menu_recipe", []):
            recipes.append(wmr["recipe"])

    # Remove duplicates by id
    recipes_dict = {r["id"]: r for r in recipes}
    recipes = list(recipes_dict.values())

    # 2️⃣ User preferences
    prefs_resp = (
        supabase.table("user_recipe_preferences")
        .select("recipe_id, like, dislike")
        .eq("user_id", user_id)
        .execute()
    )
    user_prefs = {p["recipe_id"]: p for p in prefs_resp.data}

    # 3️⃣ Score & shuffle recipes
    scored_recipes = []
    for r in recipes:
        rid = r["id"]
        pref = user_prefs.get(rid, {})
        score = random.random()
        if pref.get("like"):
            score += 2
        if pref.get("dislike"):
            score -= 3
        if r.get("always_available"):
            score += 1
        scored_recipes.append((score, r))
    scored_recipes.sort(reverse=True, key=lambda x: x[0])

    # 4️⃣ Prepare macro target
    macro_target_resp = (
        supabase.table("daily_macro_target")
        .select("protein_g, carbs_g, fat_g")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not macro_target_resp.data:
        return jsonify({"error": "No macro target found for this user"}), 400
    target = macro_target_resp.data[0]

    # 5️⃣ Build meal plan day by day
    total_days = (end_date - start_date).days + 1
    meals = ["breakfast", "lunch", "dinner", "snack"]
    plan = []
    recent_recipes = deque(maxlen=8)

    for i in range(total_days):
        date = start_date + timedelta(days=i)
        day_plan = {"date": str(date), "meals": {}}

        for meal in meals:
            candidates = [
                r
                for s, r in scored_recipes
                if r.get(f"could_be_{meal}", False)
                and r["id"] not in recent_recipes
            ]
            if not candidates:
                candidates = [r for s, r in scored_recipes]
            chosen = random.choice(candidates)
            day_plan["meals"][meal] = {
                "recipe_id": chosen["id"],
                "name": chosen.get("name"),
                "photo": chosen.get("photo"),
            }
            recent_recipes.append(chosen["id"])

        # 🔁 Optimize servings for macro balance
        optimized_subs, loss = optimize_subrecipes(day_plan["meals"], target)

        # --- Compute total macros for the day ---
        day_totals = {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0}

        for s in optimized_subs:
            # Fetch the subrecipe macros again (to aggregate totals)
            sub_data = next(
                (
                    sub
                    for sub in get_recipe_subrecipes(
                        day_plan["meals"][s["meal"]]["recipe_id"]
                    )
                    if sub["id"] == s["subrecipe_id"]
                ),
                None,
            )
            if sub_data:
                macros = sub_data["macros"]
                servings = s["servings"]
                day_totals["protein"] += macros["protein"] * servings
                day_totals["carbs"] += macros["carbs"] * servings
                day_totals["fat"] += macros["fat"] * servings
                day_totals["kcal"] += macros["kcal"] * servings

        day_plan["macro_error"] = loss
        day_plan["optimized_subrecipes"] = optimized_subs
        day_plan["totals"] = day_totals

        plan.append(day_plan)

    return (
        jsonify(
            {
                "user_id": user_id,
                "start_date": str(start_date),
                "end_date": str(end_date),
                "daily_macro_target": target,
                "days": plan,
            }
        ),
        200,
    )
