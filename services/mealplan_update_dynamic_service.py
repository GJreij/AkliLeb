from datetime import datetime
from typing import List, Dict, Any
from collections import defaultdict
import copy
import random

from utils.supabase_client import supabase
from services.mealplan_service import optimize_subrecipes


# ------------------------------------------------------------------
# STEP 1. Consolidate all user changes
# ------------------------------------------------------------------
def consolidate_changes(change_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    for log in change_logs:
        if isinstance(log.get("created_at"), str):
            log["created_at"] = datetime.fromisoformat(log["created_at"].replace("Z", ""))

    change_logs.sort(key=lambda x: x["created_at"])
    grouped = defaultdict(list)
    for entry in change_logs:
        grouped[entry["date"]].append(entry)

    final_state = {}
    for date, entries in grouped.items():
        deleted_day_entry = next(
            (e for e in reversed(entries) if e.get("Delete") and not e.get("meal_key")),
            None
        )
        if deleted_day_entry:
            final_state[date] = {"deleted_day": True}
            continue

        meals_by_key = defaultdict(list)
        for e in entries:
            if e.get("meal_key"):
                meals_by_key[e["meal_key"]].append(e)

        day_actions = {}
        for meal_key, logs in meals_by_key.items():
            last_log = sorted(logs, key=lambda x: x["created_at"])[-1]
            if last_log.get("Delete"):
                day_actions[meal_key] = {
                    "action": "delete",
                    "old_recipe_id": last_log.get("old_recipe_id"),
                    "include_macros_in_rest": last_log.get("include_macros_in_rest", True),
                }
            elif last_log.get("new_recipe_id") and not last_log.get("Delete"):
                day_actions[meal_key] = {
                    "action": "replace",
                    "old_recipe_id": last_log.get("old_recipe_id"),
                    "new_recipe_id": last_log.get("new_recipe_id"),
                    "include_macros_in_rest": last_log.get("include_macros_in_rest", True),
                }

        if day_actions:
            final_state[date] = day_actions

    return final_state


# ------------------------------------------------------------------
# STEP 2. Fetch recipe + subrecipes/macros from Supabase
# ------------------------------------------------------------------
def fetch_recipe_details(recipe_id: int) -> Dict[str, Any]:
    resp = (
        supabase.table("recipe")
        .select("id, name, photo, could_be_breakfast, could_be_lunch, could_be_dinner, could_be_snack, recipe_subrecipe(subrecipe(id, name, kcal, protein, carbs, fat, max_serving))")
        .eq("id", recipe_id)
        .single()
        .execute()
    )

    recipe = resp.data
    if not recipe:
        return {}

    subrecipes = []
    total_macros = {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0}
    for rs in recipe.get("recipe_subrecipe", []):
        s = rs.get("subrecipe", {})
        macros = {
            "protein": s.get("protein") or 0,
            "carbs": s.get("carbs") or 0,
            "fat": s.get("fat") or 0,
            "kcal": s.get("kcal") or 0,
        }
        subrecipes.append({
            "subrecipe_id": s.get("id"),
            "name": s.get("name"),
            "servings": 1,
            "macros": macros
        })
        for k in total_macros:
            total_macros[k] += macros[k]

    # Guess meal type (based on boolean flags)
    meal_types = []
    for t in ["breakfast", "lunch", "dinner", "snack"]:
        if recipe.get(f"could_be_{t}"):
            meal_types.append(t)

    return {
        "recipe_id": recipe["id"],
        "recipe_name": recipe.get("name"),
        "photo": recipe.get("photo"),
        "meal_types": meal_types,
        "subrecipes": subrecipes,
        "macros": {k: round(v) for k, v in total_macros.items()},
    }


# ------------------------------------------------------------------
# STEP 3. Apply user changes + re-optimize macros dynamically
# ------------------------------------------------------------------
def apply_changes_and_optimize(original_plan: Dict[str, Any], changes: Dict[str, Any]) -> Dict[str, Any]:
    updated_plan = copy.deepcopy(original_plan)
    daily_target = updated_plan.get("daily_macro_target", {})
    new_days = []

    for day in updated_plan.get("days", []):
        date = day["date"]
        day_change = changes.get(date)

        # 1. Skip deleted days
        if day_change and day_change.get("deleted_day"):
            continue

        # 2. Build meal set
        updated_meals = []
        for meal in day["meals"]:
            meal_key = meal["meal_key"]
            change = (day_change or {}).get(meal_key)

            # --- keep as is ---
            if not change:
                updated_meals.append(meal)
                continue

            # --- replaced recipe ---
            if change["action"] == "replace":
                new_recipe_id = change["new_recipe_id"]
                new_recipe = fetch_recipe_details(new_recipe_id)
                if not new_recipe:
                    updated_meals.append(meal)
                    continue

                meal.update({
                    "recipe_id": new_recipe["recipe_id"],
                    "recipe_name": new_recipe["recipe_name"],
                    "photo": new_recipe["photo"],
                    "subrecipes": new_recipe["subrecipes"],
                    "macros": new_recipe["macros"],
                })
                updated_meals.append(meal)
                continue

            # --- delete recipe ---
            if change["action"] == "delete":
                include_macros = change.get("include_macros_in_rest", True)
                if include_macros:
                    # we’ll re-optimize later (so we just drop meal visually)
                    continue
                else:
                    # user eats outside → drop completely
                    continue

        # 3. Build recipes_by_meal for optimizer (only non-deleted meals)
        recipes_by_meal = {
            m["meal_key"]: {
                "recipe_id": m["recipe_id"],
                "meal_type": m["meal_type"],
                "recipe_name": m["recipe_name"],
                "photo": m["photo"]
            }
            for m in updated_meals
        }

        # If everything was deleted, skip optimization
        if not recipes_by_meal:
            continue

        # 4. Re-optimize macros for this day using your existing optimizer
        optimized_subs, loss, day_totals = optimize_subrecipes(recipes_by_meal, daily_target)

        # --- Group subrecipes per meal for display ---
        subs_by_meal = {k: [] for k in recipes_by_meal.keys()}
        for sub in optimized_subs:
            meal_name = sub.get("meal_name")
            if meal_name in subs_by_meal:
                subs_by_meal[meal_name].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name": sub["name"],
                    "servings": sub["servings"],
                    "macros": sub["macros"],
                })

        # --- Aggregate macros per meal ---
        for meal in updated_meals:
            sub_list = subs_by_meal.get(meal["meal_key"], [])
            if sub_list:
                total_protein = sum(s["macros"]["protein"] for s in sub_list)
                total_carbs = sum(s["macros"]["carbs"] for s in sub_list)
                total_fat = sum(s["macros"]["fat"] for s in sub_list)
                total_kcal = sum(s["macros"]["kcal"] for s in sub_list)
                meal["macros"] = {
                    "protein": round(total_protein),
                    "carbs": round(total_carbs),
                    "fat": round(total_fat),
                    "kcal": round(total_kcal),
                }
                meal["subrecipes"] = sub_list

        # 5. Construct updated day
        updated_day = {
            "date": date,
            "weekday": day["weekday"],
            "is_weekend": day["is_weekend"],
            "macro_error": loss,
            "totals": day_totals,
            "meals": updated_meals,
        }

        new_days.append(updated_day)

    updated_plan["days"] = new_days
    return updated_plan


# ------------------------------------------------------------------
# STEP 4. Entry point — main function
# ------------------------------------------------------------------
def update_meal_plan(original_plan: Dict[str, Any], raw_change_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
    consolidated = consolidate_changes(raw_change_logs)
    updated = apply_changes_and_optimize(original_plan, consolidated)
    return updated
