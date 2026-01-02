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

def _is_weekend(d):
    return d.weekday() >= 5


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

    # Optional: if you support multiple kitchens
    kitchen_id = data.get("kitchen_id")  # can be None

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
    # 1b. Fetch kitchen closures in [start_date, end_date]
    # -------------------------------------------------------------
    closures_q = (
        supabase.table("kitchen_closure")
        .select("closure_date")
        .gte("closure_date", str(start_date))
        .lte("closure_date", str(end_date))
    )

    # If you use multiple kitchens, filter by kitchen_id.
    # If your table stores NULL kitchen_id for "global closures", you can
    # either keep them separate or implement OR logic later.
    if kitchen_id is not None:
        closures_q = closures_q.eq("kitchen_id", kitchen_id)

    closures_resp = closures_q.execute()
    closed_dates = set()
    for row in (closures_resp.data or []):
        try:
            closed_dates.add(_parse_date_yyyy_mm_dd(row["closure_date"]))
        except Exception:
            continue

    # -------------------------------------------------------------
    # 1c. Build requested dates -> available dates
    # -------------------------------------------------------------
    requested_dates = list(_daterange(start_date, end_date))

    # First apply weekend rule (if needed)
    if include_weekends:
        candidate_dates = requested_dates[:]
    else:
        candidate_dates = [d for d in requested_dates if not _is_weekend(d)]

    # Then remove closures
    available_dates = [d for d in candidate_dates if d not in closed_dates]
    excluded_dates = sorted(list(set(candidate_dates) - set(available_dates)))

    # If nothing left => all selected dates are closed (or were weekends)
    if not available_dates:
        return jsonify({
            "error": "kitchen_closed",
            "message": "The kitchen is closed for all selected dates. Please choose different dates.",
            "start_date": str(start_date),
            "end_date": str(end_date),
            "excluded_dates": [str(d) for d in excluded_dates]  # helpful for UI
        }), 400

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
    # (keep original overlap logic)
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
    # 3b. Build DATE -> ALLOWED RECIPES mapping (same as your code)
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

        for d in _daterange(ws, we):
            allowed_recipe_ids_by_date[d].update(recipe_ids_in_this_week)

    all_recipes = list(recipes_by_id.values())
    if not all_recipes:
        return jsonify({"error": "No recipes found inside weekly menus"}), 404

    # -------------------------------------------------------------
    # Safety: ensure every AVAILABLE day has recipes
    # (IMPORTANT: iterate available_dates, not full range)
    # -------------------------------------------------------------
    for d in available_dates:
        if not allowed_recipe_ids_by_date.get(d):
            return jsonify({
                "error": "No recipes available for at least one selected day",
                "missing_date": str(d),
            }), 404

    # -------------------------------------------------------------
    # 4. Fetch user preferences (same as your code)
    # -------------------------------------------------------------
    prefs_resp = (
        supabase.table("user_recipe_preferences")
        .select("recipe_id, like, dislike, dont_include")
        .eq("user_id", user_id)
        .execute()
    )
    user_prefs = {p["recipe_id"]: p for p in (prefs_resp.data or [])}

    # -------------------------------------------------------------
    # 5. Score recipes (same as your code)
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
    # 6. Fetch macro target (same as your code)
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
    kcal_target = float(target.get("kcal_target") or (4 * (protein_g + carbs_g) + 9 * fat_g))

    target_with_kcal = {
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
        "kcal": kcal_target,
    }

    # -------------------------------------------------------------
    # 7. Generate plan ONLY for available_dates
    # -------------------------------------------------------------
    days = []
    recent_recipes_global = deque(maxlen=10)
    meal_history = deque(maxlen=20)

    def _filter_recipes_for_meal_type(scored, meal_type, recent_global, meal_hist, allowed_ids_today):
        # candidates must:
        # - match meal_type flag
        # - not be recently used (soft)
        # - be allowed by weekly_menu for that date (hard)
        candidates = []
        for _, r in scored:
            rid = r["id"]
            if rid not in allowed_ids_today:
                continue

            # recipe booleans like could_be_breakfast, could_be_lunch...
            if not r.get(f"could_be_{meal_type}", False):
                continue

            # soft filtering on repeats
            if rid in recent_global or rid in meal_hist:
                continue

            candidates.append(r)

        # if too strict, relax repeat constraint
        if not candidates:
            for _, r in scored:
                rid = r["id"]
                if rid not in allowed_ids_today:
                    continue
                if not r.get(f"could_be_{meal_type}", False):
                    continue
                candidates.append(r)

        return candidates

    for date in available_dates:
        allowed_ids_today = allowed_recipe_ids_by_date.get(date, set())

        recipes_by_meal = {}

        # pick one recipe per meal_key
        for meal_key, meal_type in meals_map.items():
            candidates = _filter_recipes_for_meal_type(
                scored_recipes, meal_type, recent_recipes_global, meal_history, allowed_ids_today
            )
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
            "is_weekend": _is_weekend(date),
            "macro_error": loss,
            "totals": day_totals,
            "meals": meals_list,
        })

    return jsonify({
        "user_id": user_id,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "daily_macro_target": target_with_kcal,
        "excluded_dates": [str(d) for d in excluded_dates],
        "days": days,
    }), 200
