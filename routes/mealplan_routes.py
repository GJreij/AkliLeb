from flask import Blueprint, request, jsonify
from datetime import datetime, timedelta
import random

from utils.supabase_client import supabase
from services.mealplan_service import (
    optimize_subrecipes,
    apply_weekly_carryover,
    update_cumulative_deviation,
)
from services.daily_menu_service import (
    get_or_create_week_templates,
    prefetch_flex_stats,
    prefetch_subrecipe_sets,
    prefetch_weekday_popularity,
    prefetch_recipe_macros,
)
from utils.event_logger import log_event

mealplan_bp = Blueprint("mealplan", __name__)


# =============================================================================
# HELPERS
# =============================================================================

def _parse_date(s: str):
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daterange(d1, d2):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def _is_weekend(d) -> bool:
    return d.weekday() >= 5


# =============================================================================
# ROUTES
# =============================================================================

@mealplan_bp.route("/check_meal_plan_conflict", methods=["POST"])
def check_meal_plan_conflict():
    data           = request.get_json() or {}
    user_id        = data.get("user_id")
    start_date_str = data.get("start_date")
    end_date_str   = data.get("end_date")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = _parse_date(start_date_str)
        end_date   = _parse_date(end_date_str)
    except Exception:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    resp = (
        supabase.table("meal_plan")
        .select("id, start_date, end_date, created_at")
        .eq("user_id", user_id)
        .lte("start_date", str(end_date))
        .gte("end_date", str(start_date))
        .execute()
    )
    conflicts = resp.data or []

    has_conflict = len(conflicts) > 0
    log_event(user_id, "meal_plan_conflict_checked", {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "has_conflict": has_conflict,
        "conflict_count": len(conflicts),
    })
    return jsonify({
        "has_conflict": has_conflict,
        "conflicts":    conflicts,
        "selected":     {"start_date": str(start_date), "end_date": str(end_date)},
    }), 200


@mealplan_bp.route("/generate_meal_plan", methods=["POST"])
def generate_meal_plan():
    data = request.get_json() or {}

    # ------------------------------------------------------------------
    # 1. Parse + validate
    # ------------------------------------------------------------------
    user_id          = data.get("user_id")
    start_date_str   = data.get("start_date")
    end_date_str     = data.get("end_date")
    include_weekends = data.get("include_weekends", False)
    raw_meals        = data.get("meals")
    kcal_override    = data.get("kcal_override")   # optional: client-computed reduced target
    kitchen_id       = data.get("kitchen_id")

    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        start_date = _parse_date(start_date_str)
        end_date   = _parse_date(end_date_str)
    except Exception:
        return jsonify({"error": "Invalid date format. Expected YYYY-MM-DD"}), 400

    if end_date < start_date:
        return jsonify({"error": "end_date must be >= start_date"}), 400

    # ------------------------------------------------------------------
    # 2. Kitchen closures
    # ------------------------------------------------------------------
    closures_q = (
        supabase.table("kitchen_closure")
        .select("closure_date")
        .gte("closure_date", str(start_date))
        .lte("closure_date", str(end_date))
    )
    if kitchen_id is not None:
        closures_q = closures_q.eq("kitchen_id", kitchen_id)

    closed_dates: set = set()
    for row in (closures_q.execute().data or []):
        try:
            closed_dates.add(_parse_date(row["closure_date"]))
        except Exception:
            continue

    # ------------------------------------------------------------------
    # 3. Available dates
    # ------------------------------------------------------------------
    requested_dates = list(_daterange(start_date, end_date))
    candidate_dates = requested_dates if include_weekends else [
        d for d in requested_dates if not _is_weekend(d)
    ]
    available_dates = [d for d in candidate_dates if d not in closed_dates]
    excluded_dates  = sorted(set(candidate_dates) - set(available_dates))

    if not available_dates:
        return jsonify({
            "error":          "kitchen_closed",
            "message":        "The kitchen is closed for all selected dates. Please choose different dates.",
            "start_date":     str(start_date),
            "end_date":       str(end_date),
            "excluded_dates": [str(d) for d in excluded_dates],
        }), 400

    # ------------------------------------------------------------------
    # 4. Meals map
    # ------------------------------------------------------------------
    allowed_meal_types = {"breakfast", "lunch", "dinner", "snack"}

    if raw_meals:
        meals_map = {k: v for k, v in raw_meals.items() if v in allowed_meal_types}
        if not meals_map:
            return jsonify({"error": "Invalid meals map"}), 400
    else:
        meals_map = {
            "breakfast": "breakfast",
            "lunch":     "lunch",
            "snack":     "snack",
            "dinner":    "dinner",
        }

    # ------------------------------------------------------------------
    # 5. Weekly menus - recipe pool
    # ------------------------------------------------------------------
    weekly_menus = (
        supabase.table("weekly_menu")
        .select("id, week_start_date, week_end_date, weekly_menu_recipe(recipe(*))")
        .lte("week_start_date", str(end_date))
        .gte("week_end_date",   str(start_date))
        .execute()
        .data or []
    )
    if not weekly_menus:
        return jsonify({"error": "No weekly menus found for this date range"}), 404

    allowed_recipe_ids_by_date: dict = {}
    recipes_by_id: dict = {}

    for wm in weekly_menus:
        try:
            ws = _parse_date(wm["week_start_date"])
            we = _parse_date(wm["week_end_date"])
        except Exception:
            continue
        for wmr in (wm.get("weekly_menu_recipe") or []):
            recipe = (wmr or {}).get("recipe")
            if not recipe or not recipe.get("id"):
                continue
            rid = recipe["id"]
            recipes_by_id[rid] = recipe
            for d in _daterange(ws, we):
                allowed_recipe_ids_by_date.setdefault(d, set()).add(rid)

    all_recipes = list(recipes_by_id.values())
    if not all_recipes:
        return jsonify({"error": "No recipes found inside weekly menus"}), 404

    for d in available_dates:
        if not allowed_recipe_ids_by_date.get(d):
            return jsonify({
                "error":        "No recipes available for at least one selected day",
                "missing_date": str(d),
            }), 404

    # ------------------------------------------------------------------
    # 6. User preferences
    # ------------------------------------------------------------------
    prefs_resp = (
        supabase.table("user_recipe_preferences")
        .select("recipe_id, like, dislike, dont_include")
        .eq("user_id", user_id)
        .execute()
    )
    user_prefs = {p["recipe_id"]: p for p in (prefs_resp.data or [])}

    if all(user_prefs.get(r["id"], {}).get("dont_include") for r in all_recipes):
        return jsonify({"error": "All recipes were excluded by user preferences"}), 400

    # ------------------------------------------------------------------
    # 7. Macro target
    # ------------------------------------------------------------------
    macro_resp = (
        supabase.table("daily_macro_target")
        .select("protein_g, carbs_g, fat_g, kcal_target")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not macro_resp.data:
        return jsonify({"error": "No diet set, we're working on it!"}), 400

    t         = macro_resp.data[0]
    protein_g = float(t.get("protein_g")   or 0)
    carbs_g   = float(t.get("carbs_g")     or 0)
    fat_g     = float(t.get("fat_g")       or 0)
    kcal_db   = float(t.get("kcal_target") or (4 * (protein_g + carbs_g) + 9 * fat_g))
    # kcal_override lets the client reduce the daily target when the user is
    # "eating out" for excluded meal types (those calories are not Akli's).
    kcal_t = float(kcal_override) if kcal_override and float(kcal_override) > 0 else kcal_db
    # Scale macros proportionally so they stay relatable to the new kcal target
    # instead of solving toward stale grams sized for the original kcal target.
    macro_scale = (kcal_t / kcal_db) if kcal_db > 0 else 1.0
    target_with_kcal = {
        "protein_g": protein_g * macro_scale,
        "carbs_g":   carbs_g * macro_scale,
        "fat_g":     fat_g * macro_scale,
        "kcal":      kcal_t,
    }

    # ------------------------------------------------------------------
    # 8. BATCH PREFETCH - all auxiliary DB lookups done here, once.
    #    See services/daily_menu_service.py for why these specific
    #    functions replace the old inline ones: weekday-popularity was
    #    silently broken (queried columns that don't exist on
    #    meal_plan_day) and category-overlap was a permanent no-op
    #    (recipe_category isn't a real table) - both confirmed against
    #    the live schema and fixed there.
    # ------------------------------------------------------------------
    all_recipe_ids = [r["id"] for r in all_recipes]

    flex_stats     = prefetch_flex_stats(all_recipe_ids)
    subrecipe_sets = prefetch_subrecipe_sets(all_recipe_ids)
    popularity     = prefetch_weekday_popularity(all_recipe_ids)
    recipe_macros  = prefetch_recipe_macros(all_recipe_ids)

    # ------------------------------------------------------------------
    # 9. Resolve the week's shared daily_menu templates in ONE holistic
    #    pass (replaces the old greedy day-by-day walk that collapsed
    #    late-week candidate pools - see scripts/solver_study/ for the
    #    validated study this ships from). Already-templated dates get
    #    live-vote-converged against real confirmed orders; un-templated
    #    dates get generated and persisted; every date gets this
    #    specific client's personal dislike/exclusion swaps applied on
    #    top.
    # ------------------------------------------------------------------
    rng = random.Random()
    templates_by_date = get_or_create_week_templates(
        dates=available_dates,
        meals_map=meals_map,
        allowed_recipe_ids_by_date=allowed_recipe_ids_by_date,
        recipes_by_id=recipes_by_id,
        user_prefs=user_prefs,
        macro_target=target_with_kcal,
        flex_stats=flex_stats,
        popularity=popularity,
        recipe_macros=recipe_macros,
        subrecipe_sets=subrecipe_sets,
        rng=rng,
    )

    # ------------------------------------------------------------------
    # 10. Macro-optimize each day + apply weekly carry-over (unchanged)
    # ------------------------------------------------------------------
    days: list = []

    # Weekly carry-over: tracks accrued (actual - target) per macro across
    # the days generated so far in this call, so day N+1's target can be
    # nudged to compensate for day N's misses (see mealplan_service.py).
    cumulative_deviation: dict = {"protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "kcal": 0.0}

    for day_index, date in enumerate(available_dates):
        recipes_by_meal = templates_by_date.get(date)

        if not recipes_by_meal:
            return jsonify({
                "error": "Not enough unique recipes for this day",
                "date":  str(date),
            }), 404

        # Run macro optimizer - first day of the week uses the plain target;
        # subsequent days get a carryover-adjusted target that nudges for
        # whatever the week has under/over-shot so far (capped at +/-25%
        # of that day's original target).
        if day_index == 0:
            day_target = target_with_kcal
        else:
            day_target = apply_weekly_carryover(target_with_kcal, cumulative_deviation)

        optimized_subs, loss, day_totals = optimize_subrecipes(
            recipes_by_meal, day_target
        )

        cumulative_deviation = update_cumulative_deviation(
            cumulative_deviation, day_target, day_totals
        )

        if day_totals.get("tolerance_used") == "SAFE_FALLBACK":
            log_event(user_id, "mealplan_lp_fallback", {
                "date": str(date),
                "recipe_ids": [info["recipe_id"] for info in recipes_by_meal.values()],
            })

        # Group optimized subrecipes back by meal slot
        subs_by_meal: dict = {k: [] for k in recipes_by_meal}
        for sub in optimized_subs:
            mk = sub["meal_name"]
            if mk in subs_by_meal:
                subs_by_meal[mk].append({
                    "subrecipe_id": sub["subrecipe_id"],
                    "name":         sub["name"],
                    "servings":     sub["servings"],
                    "macros":       sub["macros"],
                })

        # Compute per-meal macro totals
        macros_per_meal: dict = {
            mk: {
                "protein": int(sum(s["macros"]["protein"] for s in subs)),
                "carbs":   int(sum(s["macros"]["carbs"]   for s in subs)),
                "fat":     int(sum(s["macros"]["fat"]     for s in subs)),
                "kcal":    int(sum(s["macros"]["kcal"]    for s in subs)),
            }
            for mk, subs in subs_by_meal.items()
        }

        meals_list = [
            {
                "meal_key":    meal_key,
                "meal_type":   info["meal_type"],
                "recipe_id":   info["recipe_id"],
                "recipe_name": info["recipe_name"],
                "photo":       info["photo"],
                "macros":      macros_per_meal.get(meal_key, {}),
                "subrecipes":  subs_by_meal.get(meal_key, []),
            }
            for meal_key, info in recipes_by_meal.items()
        ]

        days.append({
            "date":        str(date),
            "weekday":     date.weekday(),
            "is_weekend":  _is_weekend(date),
            "macro_error": loss,
            "totals":      day_totals,
            "meals":       meals_list,
            # Persisted so a later /update_meal_plan re-optimization (see
            # mealplan_update_dynamic_service.py) carries the same
            # carryover-adjusted target forward instead of reverting to the
            # flat global target.
            "adjusted_target": day_target,
        })

    log_event(user_id, "meal_plan_generated", {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "num_days": len(days),
        "meals_per_day": len(meals_map),
        "excluded_dates_count": len(excluded_dates),
    })
    return jsonify({
        "user_id":            user_id,
        "start_date":         str(start_date),
        "end_date":           str(end_date),
        "daily_macro_target": target_with_kcal,
        "excluded_dates":     [str(d) for d in excluded_dates],
        "days":               days,
    }), 200


@mealplan_bp.route("/update_meal_plan", methods=["POST"])
def update_meal_plan_endpoint():
    """
    Input:  { "original_plan": {...}, "change_logs": [...] }
    Output: Updated optimized meal plan (same shape as /generate_meal_plan).
    """
    data          = request.get_json() or {}
    original_plan = data.get("original_plan")
    logs          = data.get("change_logs", [])

    if not original_plan or not isinstance(logs, list):
        log_event(None, "api_error", {"route": "/update_meal_plan", "status_code": 400, "reason": "missing_or_invalid_input"})
        return jsonify({"error": "Missing or invalid input data"}), 400

    from services.mealplan_update_dynamic_service import update_meal_plan
    updated = update_meal_plan(original_plan, logs)

    user_id = original_plan.get("user_id")
    log_event(user_id, "recipe_swap_triggered", {
        "change_count": len(logs),
        "start_date": original_plan.get("start_date"),
        "end_date": original_plan.get("end_date"),
    })
    return jsonify(updated), 200
