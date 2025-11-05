import random
from utils.supabase_client import supabase


def get_recipe_subrecipes(recipe_id):
    """
    Returns a list of subrecipes for a given recipe with their aggregated macros and max_serving.
    Each subrecipe includes: id, label, macros, max_serving.
    """
    resp = (
        supabase.table("recipe_subrecipe")
        .select(
            "subrecipe_id, subrecipe(name, max_serving, "
            "subrec_ingred(quantity, ingredient(protein, carbs, fat, kcal)))"
        )
        .eq("recipe_id", recipe_id)
        .execute()
    )

    subrecipes = []
    for rs in resp.data:
        sub = rs.get("subrecipe", {})
        total = {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0}
        for si in sub.get("subrec_ingred", []):
            i = si.get("ingredient", {})
            q = si.get("quantity") or 1
            total["protein"] += (i.get("protein") or 0) * q
            total["carbs"] += (i.get("carbs") or 0) * q
            total["fat"] += (i.get("fat") or 0) * q
            total["kcal"] += (i.get("kcal") or 0) * q

        subrecipes.append(
            {
                "id": rs["subrecipe_id"],
                "name": sub.get("name"),
                "macros": total,
                "max_serving": sub.get("max_serving") or 3,
            }
        )

    return subrecipes


def optimize_subrecipes(recipes_by_meal, macro_target):
    """
    Optimizes serving sizes with discrete integer steps and meal-distribution rules.
    """

    all_subs = []
    for meal, info in recipes_by_meal.items():
        subs = get_recipe_subrecipes(info["recipe_id"])
        for s in subs:
            all_subs.append(
                {
                    "meal": meal,
                    "subrecipe_id": s["id"],
                    "name": s["name"],
                    "macros": s["macros"],
                    "max_serving": s["max_serving"],
                }
            )

    # --- Targets ---
    P_t, C_t, F_t = (
        macro_target["protein_g"],
        macro_target["carbs_g"],
        macro_target["fat_g"],
    )
    kcal_t = 4 * (P_t + C_t) + 9 * F_t

    # --- Step 1: Discrete search (integer servings) ---
    serving_options = [1, 2, 3, 4, 5]

    best_combo = None
    best_error = float("inf")

    for _ in range(5000):  # you can tune this number
        combo = [
            random.choice(serving_options[: int(s["max_serving"])])
            for s in all_subs
        ]

        total_P = sum(combo[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
        total_C = sum(combo[i] * s["macros"]["carbs"] for i, s in enumerate(all_subs))
        total_F = sum(combo[i] * s["macros"]["fat"] for i, s in enumerate(all_subs))
        total_K = 4 * (total_P + total_C) + 9 * total_F  # noqa: F841 (unused but ok)

        # Basic macro error
        macro_error = (
            (total_P - P_t) ** 2
            + (total_C - C_t) ** 2
            + (total_F - F_t) ** 2
        )

        # --- Step 2: Apply calorie distribution penalties ---
        kcal_by_meal = {}
        for meal in recipes_by_meal.keys():
            kcal_by_meal[meal] = sum(
                combo[i] * s["macros"]["kcal"]
                for i, s in enumerate(all_subs)
                if s["meal"] == meal
            )

        penalties = 0

        # Rule 1: Breakfast ≤ 30% of total kcal
        if "breakfast" in kcal_by_meal:
            if kcal_by_meal["breakfast"] > 0.3 * kcal_t:
                penalties += (kcal_by_meal["breakfast"] - 0.3 * kcal_t) ** 2

        # Rule 2: Snack ≤ 20% of total kcal
        if "snack" in kcal_by_meal:
            if kcal_by_meal["snack"] > 0.2 * kcal_t:
                penalties += (kcal_by_meal["snack"] - 0.2 * kcal_t) ** 2

        # Rule 3: Dinner ≈ Lunch (within ±20%)
        if "dinner" in kcal_by_meal and "lunch" in kcal_by_meal:
            diff = abs(kcal_by_meal["dinner"] - kcal_by_meal["lunch"])
            allowed = 0.2 * max(kcal_by_meal["dinner"], kcal_by_meal["lunch"])
            if diff > allowed:
                penalties += (diff - allowed) ** 2

        total_error = macro_error + 0.01 * penalties  # scale penalty effect

        if total_error < best_error:
            best_error = total_error
            best_combo = combo

    # Build output
    optimized = []
    for i, s in enumerate(all_subs):
        optimized.append(
            {
                "subrecipe_id": s["subrecipe_id"],
                "name": s["name"],
                "meal": s["meal"],
                "servings": int(best_combo[i]),
            }
        )

    return optimized, best_error
