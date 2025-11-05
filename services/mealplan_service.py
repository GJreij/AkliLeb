import random
from utils.supabase_client import supabase


def get_recipe_subrecipes(recipe_id):
    """
    Returns a list of subrecipes for a given recipe with their per-serving macros and max_serving.
    Uses the precomputed macros stored on subrecipe (one serving).
    """
    resp = (
        supabase.table("recipe_subrecipe")
        .select("subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .eq("recipe_id", recipe_id)
        .execute()
    )

    subrecipes = []
    for rs in resp.data:
        sub = rs.get("subrecipe") or {}
        subrecipes.append({
            "id": sub.get("id"),
            "name": sub.get("name"),
            "max_serving": sub.get("max_serving") or 3,
            "macros": {
                "kcal": sub.get("kcal") or 0.0,
                "protein": sub.get("protein") or 0.0,
                "carbs": sub.get("carbs") or 0.0,
                "fat": sub.get("fat") or 0.0,
            }
        })

    return subrecipes


def optimize_subrecipes(recipes_by_meal, macro_target):
    """
    Optimizes subrecipe servings (integers) to fit macro targets and calorie distribution rules.

    - Servings are integers in [1, max_serving]
    - Rules:
        * Breakfast <= 30% of total kcal
        * Snack    <= 20% of total kcal
        * Dinner and Lunch within ±20% of each other
      (Penalized, not hard-forbidden)
    - Returns: optimized_subs, total_error, day_totals
    """

    # ---- Gather subrecipes with macros per serving ----
    all_subs = []  # each: {meal, subrecipe_id, name, macros, max_serving}
    for meal, info in recipes_by_meal.items():
        subs = get_recipe_subrecipes(info["recipe_id"])
        for s in subs:
            all_subs.append({
                "meal": meal,
                "subrecipe_id": s["id"],
                "name": s["name"],
                "macros": s["macros"],   # per serving
                "max_serving": s["max_serving"]
            })

    if not all_subs:
        return [], 0.0, {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0}

    # ---- Targets ----
    P_t = macro_target.get("protein_g") or 0.0
    C_t = macro_target.get("carbs_g") or 0.0
    F_t = macro_target.get("fat_g") or 0.0
    kcal_t = 4 * (P_t + C_t) + 9 * F_t

    # ---- Random discrete search ----
    best_combo = None
    best_error = float("inf")

    iterations = 4000  # can tune

    for _ in range(iterations):
        combo = []
        for s in all_subs:
            max_sv = int(s["max_serving"] or 1)
            if max_sv < 1:
                max_sv = 1
            # integer servings between 1 and max_serving
            combo.append(random.randint(1, max_sv))

        # Totals
        total_P = sum(combo[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
        total_C = sum(combo[i] * s["macros"]["carbs"] for i, s in enumerate(all_subs))
        total_F = sum(combo[i] * s["macros"]["fat"] for i, s in enumerate(all_subs))
        total_K = sum(combo[i] * s["macros"]["kcal"] for i, s in enumerate(all_subs))

        # Macro error
        macro_error = (total_P - P_t) ** 2 + (total_C - C_t) ** 2 + (total_F - F_t) ** 2

        # ---- Calorie distribution penalties ----
        penalties = 0.0
        if kcal_t > 0:
            # kcal per meal
            kcal_by_meal = {}
            for meal in recipes_by_meal.keys():
                kcal_by_meal[meal] = sum(
                    combo[i] * s["macros"]["kcal"]
                    for i, s in enumerate(all_subs) if s["meal"] == meal
                )

            # Rule 1: Breakfast <= 30% of total kcal
            if "breakfast" in kcal_by_meal:
                limit_b = 0.3 * kcal_t
                if kcal_by_meal["breakfast"] > limit_b:
                    penalties += (kcal_by_meal["breakfast"] - limit_b) ** 2

            # Rule 2: Snack <= 20% of total kcal
            if "snack" in kcal_by_meal:
                limit_s = 0.2 * kcal_t
                if kcal_by_meal["snack"] > limit_s:
                    penalties += (kcal_by_meal["snack"] - limit_s) ** 2

            # Rule 3: Dinner ≈ Lunch (±20%)
            if "dinner" in kcal_by_meal and "lunch" in kcal_by_meal:
                d_k = kcal_by_meal["dinner"]
                l_k = kcal_by_meal["lunch"]
                bigger = max(d_k, l_k)
                allowed_diff = 0.2 * bigger  # 20%
                diff = abs(d_k - l_k)
                if diff > allowed_diff:
                    penalties += (diff - allowed_diff) ** 2

        total_error = macro_error + 0.01 * penalties

        if total_error < best_error:
            best_error = total_error
            best_combo = combo

    # ---- Build result for best combo ----
    if best_combo is None:
        # fallback
        best_combo = [1] * len(all_subs)

    # Daily totals for the chosen combo
    day_totals = {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0}
    optimized = []

    for i, s in enumerate(all_subs):
        servings = int(best_combo[i])
        macros = s["macros"]

        day_totals["protein"] += macros["protein"] * servings
        day_totals["carbs"]   += macros["carbs"] * servings
        day_totals["fat"]     += macros["fat"] * servings
        day_totals["kcal"]    += macros["kcal"] * servings

        optimized.append({
            "subrecipe_id": s["subrecipe_id"],
            "name": s["name"],
            "meal": s["meal"],
            "servings": servings
        })

    return optimized, float(best_error), day_totals
