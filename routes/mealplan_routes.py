from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
from collections import deque, defaultdict
import random

from utils.supabase_client import supabase
from services.mealplan_service import optimize_subrecipes

mealplan_bp = Blueprint("mealplan", __name__)

def _parse_date_yyyy_mm_dd(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


@mealplan_bp.route("/check_meal_plan_conflict", methods=["POST"])
def check_meal_plan_conflict():
    data = request.get_json() or {}
    user_id = data.get("user_id")
    start_date_str = data.get("start_date")
    end_date_str = data.get("end_date")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = _parse_date_yyyy_mm_dd(start_date_str)
        end_date = _parse_date_yyyy_mm_dd(end_date_str)
    except Exception:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    # Find any existing meal_plan that overlaps the selected range
    # overlap: existing.start_date <= end_date AND existing.end_date >= start_date
    resp = (
        supabase.table("meal_plan")
        .select("id, start_date, end_date, created_at")
        .eq("user_id", user_id)
        .lte("start_date", str(end_date))
        .gte("end_date", str(start_date))
        .execute()
    )

    conflicts = resp.data or []

    return jsonify({
        "has_conflict": len(conflicts) > 0,
        "conflicts": conflicts,  # list of overlapping plans (ids + ranges)
        "selected": {"start_date": str(start_date), "end_date": str(end_date)}
    }), 200
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
        return jsonify({"error": "No diet set, we're working on it!"}), 400

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

    def _filter_recipes_for_meal_type(scored, meal_type, recent_global, meal_hist, allowed_ids_today, used_today):
        candidates = []
        for _, r in scored:
            rid = r["id"]
            if rid not in allowed_ids_today:
                continue
            if rid in used_today:                 # <- HARD block same-day duplicates
                continue
            if not r.get(f"could_be_{meal_type}", False):
                continue
            if rid in recent_global or rid in meal_hist:
                continue
            candidates.append(r)

        # relax repeat constraint, BUT still keep same-day uniqueness
        if not candidates:
            for _, r in scored:
                rid = r["id"]
                if rid not in allowed_ids_today:
                    continue
                if rid in used_today:             # <- still enforced here
                    continue
                if not r.get(f"could_be_{meal_type}", False):
                    continue
                candidates.append(r)

        return candidates


    for date in available_dates:
        allowed_ids_today = allowed_recipe_ids_by_date.get(date, set())

        recipes_by_meal = {}
        used_today = set()  # HARD rule: no recipe reused within the same day

        # ---------------------------------------------------------
        # 1. Pick recipes per meal (unique within day)
        # ---------------------------------------------------------
        for meal_key, meal_type in meals_map.items():
            # strict pass: avoid recent + avoid same-day
            candidates = []
            for _, r in scored_recipes:
                rid = r["id"]

                if rid not in allowed_ids_today:
                    continue
                if rid in used_today:
                    continue
                if not r.get(f"could_be_{meal_type}", False):
                    continue
                if rid in recent_recipes_global or rid in meal_history:
                    continue

                candidates.append(r)

            # relaxed pass: allow repeats across days but NOT within day
            if not candidates:
                for _, r in scored_recipes:
                    rid = r["id"]

                    if rid not in allowed_ids_today:
                        continue
                    if rid in used_today:
                        continue
                    if not r.get(f"could_be_{meal_type}", False):
                        continue

                    candidates.append(r)

            if not candidates:
                return jsonify({
                    "error": "Not enough unique recipes for this day",
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

            used_today.add(chosen["id"])
            meal_history.append(chosen["id"])
            recent_recipes_global.append(chosen["id"])

        # ---------------------------------------------------------
        # 2. Optimize subrecipes to hit macro target
        # ---------------------------------------------------------
        optimized_subs, loss, day_totals = optimize_subrecipes(
            recipes_by_meal,
            target_with_kcal
        )

        # ---------------------------------------------------------
        # 3. Group subrecipes by meal
        # ---------------------------------------------------------
        subs_by_meal = {k: [] for k in recipes_by_meal}

        for sub in optimized_subs:
            meal_name = sub["meal_name"]
            if meal_name in subs_by_meal:
                subs_by_meal[meal_name].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name": sub["name"],
                    "servings": sub["servings"],
                    "macros": sub["macros"],
                })

        # ---------------------------------------------------------
        # 4. Compute macros per meal
        # ---------------------------------------------------------
        macros_per_recipe = {}

        for meal_key, subs in subs_by_meal.items():
            macros_per_recipe[meal_key] = {
                "protein": int(sum(s["macros"]["protein"] for s in subs)),
                "carbs": int(sum(s["macros"]["carbs"] for s in subs)),
                "fat": int(sum(s["macros"]["fat"] for s in subs)),
                "kcal": int(sum(s["macros"]["kcal"] for s in subs)),
            }

        # ---------------------------------------------------------
        # 5. Assemble meals list
        # ---------------------------------------------------------
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

        # ---------------------------------------------------------
        # 6. Push day into response
        # ---------------------------------------------------------
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
