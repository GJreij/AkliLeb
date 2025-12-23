from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from collections import deque, defaultdict
import random

from utils.supabase_client import supabase
from services.mealplan_service import optimize_subrecipes


mealplan_bp = Blueprint("mealplan", __name__)


def _parse_date_yyyy_mm_dd(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daterange(d1, d2):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


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
        start_date = _parse_date_yyyy_mm_dd(start_date_str)
        end_date = _parse_date_yyyy_mm_dd(end_date_str)
    except Exception:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    # -------------------------------------------------------------
    # CHECK: The date range is not only weekends
    # -------------------------------------------------------------
    has_weekday = any(d.weekday() < 5 for d in _daterange(start_date, end_date))
    if not has_weekday:
        return jsonify({"error": "Selected date range contains only weekend days"}), 400

    # -------------------------------------------------------------
    # 2. Build meals_map
    # -------------------------------------------------------------
    allowed_meal_types = {"breakfast", "lunch", "dinner", "snack"}

    if raw_meals:
        meals_map = {k: v for k, v in raw_meals.items() if v in allowed_meal_types}
        if not meals_map:
            return jsonify({"error": "Invalid meals map"}), 400
    else:
        meals_map = {
            "breakfast": "breakfast",
            "lunch": "lunch",
            "dinner": "dinner",
            "snack": "snack",
        }

    # -------------------------------------------------------------
    # 3. Fetch weekly menus that OVERLAP [start_date, end_date]
    #
    # Correct overlap condition:
    #   week_start_date <= end_date  AND  week_end_date >= start_date
    # -------------------------------------------------------------
    weekly_menus_resp = (
        supabase.table("weekly_menu")
        .select("""
            id,
            week_start_date,
            week_end_date,
            weekly_menu_recipe(
                recipe(*)
            )
        """)
        .lte("week_start_date", str(end_date))
        .gte("week_end_date", str(start_date))
        .execute()
    )

    weekly_menus = weekly_menus_resp.data or []
    if not weekly_menus:
        return jsonify({"error": "No weekly menus found for this date range"}), 404

    # -------------------------------------------------------------
    # 3b. Build DATE -> ALLOWED RECIPES mapping
    #
    # This is the HARD GUARANTEE:
    # recipes can only be chosen for dates in the weekly menu that contains them.
    # -------------------------------------------------------------
    allowed_recipe_ids_by_date = defaultdict(set)   # date -> set(recipe_id)
    recipes_by_id = {}                             # recipe_id -> recipe object

    for wm in weekly_menus:
        try:
            ws = _parse_date_yyyy_mm_dd(wm["week_start_date"])
            we = _parse_date_yyyy_mm_dd(wm["week_end_date"])
        except Exception:
            continue

        wmr_list = wm.get("weekly_menu_recipe", []) or []
        recipe_ids_in_this_week = set()

        for wmr in wmr_list:
            recipe = (wmr or {}).get("recipe")
            if not recipe or not recipe.get("id"):
                continue
            rid = recipe["id"]
            recipe_ids_in_this_week.add(rid)
            recipes_by_id[rid] = recipe

        if not recipe_ids_in_this_week:
            continue

        # mark these recipes as allowed ONLY on dates within this weekly menu
        for d in _daterange(ws, we):
            # intersection not required; extra dates outside request are fine
            allowed_recipe_ids_by_date[d].update(recipe_ids_in_this_week)

    # Only keep recipes we actually have objects for
    all_recipes = list(recipes_by_id.values())
    if not all_recipes:
        return jsonify({"error": "No recipes found inside weekly menus"}), 404

    # Safety: ensure every requested day has some allowed recipes (for weekdays if weekends excluded)
    for d in _daterange(start_date, end_date):
        if not include_weekends and d.weekday() >= 5:
            continue
        if not allowed_recipe_ids_by_date.get(d):
            return jsonify({
                "error": "No recipes available for at least one selected day",
                "missing_date": str(d),
            }), 404

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
    # 5. Score recipes (global scoring pool)
    # -------------------------------------------------------------
    scored_recipes = []
    for r in all_recipes:
        rid = r["id"]
        pref = user_prefs.get(rid, {})

        if pref.get("dont_include"):
            continue

        score = random.random()
        if pref.get("like"):
            score += 2
        if pref.get("dislike"):
            score -= 5

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

    # -------------------------------------------------------------
    # 7. Anti-repeat system
    # -------------------------------------------------------------
    recent_recipes_per_meal = {meal_key: deque(maxlen=4) for meal_key in meals_map.keys()}
    recent_recipes_global = deque(maxlen=6)

    def get_candidates(meal_type, meal_history, allowed_ids_for_day):
        """
        Date-scoped candidate selection:
        - ALWAYS restrict to allowed_ids_for_day (hard rule).
        - Preserve your L1..L4 relaxations but never violate allowed_ids_for_day.
        """

        allowed_ids_for_day = set(allowed_ids_for_day or [])

        # L1 strict
        preferred = [
            r for _, r in scored_recipes
            if r["id"] in allowed_ids_for_day
            and r.get(f"could_be_{meal_type}", False)
            and r["id"] not in meal_history
            and r["id"] not in recent_recipes_global
            and not user_prefs.get(r["id"], {}).get("dont_include", False)
        ]
        if preferred:
            return preferred

        # L2 relaxed (still no global repeat)
        relaxed_without_global = [
            r for _, r in scored_recipes
            if r["id"] in allowed_ids_for_day
            and r.get(f"could_be_{meal_type}", False)
            and r["id"] not in recent_recipes_global
        ]
        if relaxed_without_global:
            return relaxed_without_global

        # L3 allow global repetition (still within meal type)
        relaxed_mealtype = [
            r for _, r in scored_recipes
            if r["id"] in allowed_ids_for_day
            and r.get(f"could_be_{meal_type}", False)
        ]
        if relaxed_mealtype:
            return relaxed_mealtype

        # L4 final resort: any allowed recipe for that day (meal_type ignored)
        any_allowed = [
            r for _, r in scored_recipes
            if r["id"] in allowed_ids_for_day
        ]
        return any_allowed

    # -------------------------------------------------------------
    # 8. Generate each day
    # -------------------------------------------------------------
    days = []
    total_days = (end_date - start_date).days + 1

    for i in range(total_days):
        date = start_date + timedelta(days=i)

        if not include_weekends and date.weekday() >= 5:
            continue

        allowed_ids_today = allowed_recipe_ids_by_date.get(date, set())
        if not allowed_ids_today:
            return jsonify({"error": "No recipes available for this day", "date": str(date)}), 404

        recipes_by_meal = {}

        for meal_key, meal_type in meals_map.items():
            meal_history = recent_recipes_per_meal[meal_key]

            candidates = get_candidates(meal_type, meal_history, allowed_ids_today)
            if not candidates:
                return jsonify({
                    "error": "No candidate recipes found for this day/meal type (weekly_menu constraint enforced)",
                    "date": str(date),
                    "meal_key": meal_key,
                    "meal_type": meal_type,
                }), 404

            chosen = random.choice(candidates)

            recipes_by_meal[meal_key] = {
                "recipe_id": chosen["id"],
                "meal_key": meal_key,
                "meal_type": meal_type,
                "recipe_name": chosen.get("name"),
                "photo": chosen.get("photo"),
            }

            meal_history.append(chosen["id"])
            recent_recipes_global.append(chosen["id"])

        # ---------------------------------------------------------
        # 9. Macro optimization
        # ---------------------------------------------------------
        optimized_subs, loss, day_totals = optimize_subrecipes(recipes_by_meal, target_with_kcal)

        subs_by_meal = {k: [] for k in recipes_by_meal}
        for sub in optimized_subs:
            mk = sub["meal_name"]
            if mk in subs_by_meal:
                subs_by_meal[mk].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name": sub["name"],
                    "servings": sub["servings"],
                    "macros": sub["macros"],
                })

        macros_per_recipe = {}
        for meal_key, sub_list in subs_by_meal.items():
            macros_per_recipe[meal_key] = {
                "protein": int(sum(s["macros"]["protein"] for s in sub_list)),
                "carbs": int(sum(s["macros"]["carbs"] for s in sub_list)),
                "fat": int(sum(s["macros"]["fat"] for s in sub_list)),
                "kcal": int(sum(s["macros"]["kcal"] for s in sub_list)),
            }

        meals_list = []
        for meal_key, info in recipes_by_meal.items():
            meals_list.append({
                "meal_key": meal_key,
                "meal_type": info["meal_type"],
                "recipe_id": info["recipe_id"],
                "recipe_name": info["recipe_name"],
                "photo": info["photo"],
                "macros": macros_per_recipe.get(meal_key, {}),
                "subrecipes": subs_by_meal.get(meal_key, []),
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
    data = request.get_json() or {}
    original_plan = data.get("original_plan")
    logs = data.get("change_logs", [])

    if not original_plan or not isinstance(logs, list):
        return jsonify({"error": "Missing or invalid input data"}), 400

    from services.mealplan_update_dynamic_service import update_meal_plan
    updated = update_meal_plan(original_plan, logs)

    return jsonify(updated), 200
