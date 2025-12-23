from typing import Dict, Any, List, Tuple
from collections import defaultdict

from pulp import (
    LpProblem, LpMinimize, LpVariable, lpSum, LpInteger, value,
    PULP_CBC_CMD, LpStatus
)
from utils.supabase_client import supabase


def get_recipe_subrecipes(recipe_id: int) -> List[Dict[str, Any]]:
    resp = (
        supabase.table("recipe_subrecipe")
        .select("subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .eq("recipe_id", recipe_id)
        .execute()
    )

    subrecipes = []
    for rs in resp.data or []:
        sub = rs.get("subrecipe") or {}
        subrecipes.append({
            "id": sub.get("id"),
            "name": sub.get("name"),
            "max_serving": sub.get("max_serving") or 3,
            "macros": {
                "kcal": float(sub.get("kcal") or 0.0),
                "protein": float(sub.get("protein") or 0.0),
                "carbs": float(sub.get("carbs") or 0.0),
                "fat": float(sub.get("fat") or 0.0),
            }
        })

    return subrecipes


def optimize_subrecipes(
    recipes_by_meal: Dict[str, Dict[str, Any]],
    macro_target: Dict[str, float],
) -> Tuple[List[Dict[str, Any]], float, Dict[str, Any]]:
    """
    recipes_by_meal:
      {
        "breakfast": { "recipe_id": 1, "meal_type": "breakfast", ... },
        "lunch":     { "recipe_id": 2, "meal_type": "lunch", ... }
      }

    macro_target:
      { "protein_g": 150, "carbs_g": 200, "fat_g": 60, "kcal": 2000? }

    Returns:
      optimized_subs, total_error, totals
    """

    # -------------------------------------------------------------
    # Config
    # -------------------------------------------------------------
    KCAL_TOLERANCES = [0.10, 0.15, 0.20, 0.40]

    BREAKFAST_MAX_PCT = 0.40
    SNACK_MAX_PCT = 0.30
    DINNER_LUNCH_DIFF_PCT = 0.30
    NO_DINNER_YES_LUNCH_PCT = 0.70
    NO_LUNCH_YES_DINNER_PCT = 0.70

    SERVING_MIN = 1
    DEFAULT_MAX_SERVING = 3

    WEIGHT_PROTEIN = 1.0
    WEIGHT_CARBS = 1.0
    WEIGHT_FAT = 1.0

    # -------------------------------------------------------------
    # Collect all subrecipes
    # -------------------------------------------------------------
    all_subs = []
    for meal_key, info in recipes_by_meal.items():
        subs = get_recipe_subrecipes(info["recipe_id"])
        for s in subs:
            all_subs.append({
                "meal": meal_key,
                "subrecipe_id": s["id"],
                "name": s["name"],
                "macros": s["macros"],
                "max_serving": int(s.get("max_serving") or DEFAULT_MAX_SERVING),
            })

    if not all_subs:
        return [], 0.0, {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0, "tolerance_used": None}

    # Targets
    P_t = float(macro_target.get("protein_g") or 0.0)
    C_t = float(macro_target.get("carbs_g") or 0.0)
    F_t = float(macro_target.get("fat_g") or 0.0)

    # Use provided kcal if present, else compute
    kcal_t = float(macro_target.get("kcal") or (4 * (P_t + C_t) + 9 * F_t))

    def safe_fallback():
        """
        Guaranteed to be in-scope and non-crashing.
        """
        servings_safe = {i: 1 for i in range(len(all_subs))}

        def totals(servs):
            P = sum(servs[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
            C = sum(servs[i] * s["macros"]["carbs"] for i, s in enumerate(all_subs))
            F = sum(servs[i] * s["macros"]["fat"] for i, s in enumerate(all_subs))
            K = sum(servs[i] * s["macros"]["kcal"] for i, s in enumerate(all_subs))
            return P, C, F, K

        P, C, F, K = totals(servings_safe)

        target_P = P_t
        target_K_low = 0.8 * kcal_t
        target_K_high = 1.2 * kcal_t

        # Increase protein until target hit (or kcal too high)
        while P < target_P and K < target_K_high:
            best = max(
                range(len(all_subs)),
                key=lambda i: (all_subs[i]["macros"]["protein"] / max(all_subs[i]["macros"]["kcal"], 1))
            )
            if servings_safe[best] < all_subs[best]["max_serving"]:
                servings_safe[best] += 1
                P, C, F, K = totals(servings_safe)
            else:
                break

        # Increase kcal to reach 80% kcal floor
        while K < target_K_low:
            best = max(range(len(all_subs)), key=lambda i: all_subs[i]["macros"]["kcal"])
            if servings_safe[best] < all_subs[best]["max_serving"]:
                servings_safe[best] += 1
                P, C, F, K = totals(servings_safe)
            else:
                break

        optimized_safe = []
        for i, s in enumerate(all_subs):
            servings_val = int(servings_safe[i])
            meal_key = s["meal"]
            meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")

            macros = {
                "protein": servings_val * s["macros"]["protein"],
                "carbs": servings_val * s["macros"]["carbs"],
                "fat": servings_val * s["macros"]["fat"],
                "kcal": servings_val * s["macros"]["kcal"],
            }

            optimized_safe.append({
                "subrecipe_id": s["subrecipe_id"],
                "name": s["name"],
                "meal_name": meal_key,
                "meal_type": meal_type,
                "servings": servings_val,
                "macros": macros,
            })

        return optimized_safe, None, {
            "protein": int(P),
            "carbs": int(C),
            "fat": int(F),
            "kcal": int(K),
            "tolerance_used": "SAFE_FALLBACK",
        }

    # Try several kcal tolerances until feasible
    for tol in KCAL_TOLERANCES:
        prob = LpProblem(f"MealPlanOptimization_{int(tol * 100)}", LpMinimize)

        servings = {
            i: LpVariable(
                f"x_{i}",
                lowBound=SERVING_MIN,
                upBound=s["max_serving"],
                cat=LpInteger,
            )
            for i, s in enumerate(all_subs)
        }

        total_P = lpSum(servings[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
        total_C = lpSum(servings[i] * s["macros"]["carbs"] for i, s in enumerate(all_subs))
        total_F = lpSum(servings[i] * s["macros"]["fat"] for i, s in enumerate(all_subs))
        total_K = lpSum(servings[i] * s["macros"]["kcal"] for i, s in enumerate(all_subs))

        dev_P = LpVariable("dev_P", lowBound=0)
        dev_C = LpVariable("dev_C", lowBound=0)
        dev_F = LpVariable("dev_F", lowBound=0)

        # |total - target|
        prob += total_P - P_t <= dev_P
        prob += -(total_P - P_t) <= dev_P
        prob += total_C - C_t <= dev_C
        prob += -(total_C - C_t) <= dev_C
        prob += total_F - F_t <= dev_F
        prob += -(total_F - F_t) <= dev_F

        # Kcal by meal_type for constraints
        kcal_by_type: Dict[str, Any] = defaultdict(int)
        for i, s in enumerate(all_subs):
            meal_key = s["meal"]
            meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")
            if meal_type:
                kcal_by_type[meal_type] += servings[i] * s["macros"]["kcal"]

        types = set(kcal_by_type.keys())

        # Apply constraints according to which meal types exist
        if {"snack", "breakfast", "dinner", "lunch"} <= types:
            prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * kcal_t
            prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * kcal_t
            d = kcal_by_type["dinner"]
            l = kcal_by_type["lunch"]
            prob += d - l <= DINNER_LUNCH_DIFF_PCT * l
            prob += l - d <= DINNER_LUNCH_DIFF_PCT * d

        if "snack" in types and "breakfast" not in types and "dinner" in types and "lunch" in types:
            prob += kcal_by_type["snack"] <= (SNACK_MAX_PCT + 0.10) * kcal_t
            d = kcal_by_type["dinner"]
            l = kcal_by_type["lunch"]
            prob += d - l <= DINNER_LUNCH_DIFF_PCT * l
            prob += l - d <= DINNER_LUNCH_DIFF_PCT * d

        if "snack" not in types and "breakfast" not in types and "dinner" in types and "lunch" in types:
            d = kcal_by_type["dinner"]
            l = kcal_by_type["lunch"]
            prob += d - l <= DINNER_LUNCH_DIFF_PCT * l
            prob += l - d <= DINNER_LUNCH_DIFF_PCT * d

        if "snack" in types and "breakfast" in types and "dinner" not in types and "lunch" in types:
            prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * kcal_t
            prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * kcal_t
            prob += kcal_by_type["lunch"] <= NO_DINNER_YES_LUNCH_PCT * kcal_t

        if "snack" in types and "breakfast" in types and "dinner" in types and "lunch" not in types:
            prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * kcal_t
            prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * kcal_t
            prob += kcal_by_type["dinner"] <= NO_LUNCH_YES_DINNER_PCT * kcal_t

        # Kcal tolerance
        prob += total_K >= (1 - tol) * kcal_t
        prob += total_K <= (1 + tol) * kcal_t

        # Objective
        prob += (WEIGHT_PROTEIN * dev_P + WEIGHT_CARBS * dev_C + WEIGHT_FAT * dev_F)

        # Solve
        prob.solve(PULP_CBC_CMD(msg=False))

        if LpStatus[prob.status] == "Optimal":
            day_totals = {
                "protein": int(round(value(total_P))),
                "carbs": int(round(value(total_C))),
                "fat": int(round(value(total_F))),
                "kcal": int(round(value(total_K))),
                "tolerance_used": tol,
            }

            optimized = []
            for i, s in enumerate(all_subs):
                servings_val = int(value(servings[i]))
                meal_key = s["meal"]
                meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")

                mps = s["macros"]
                optimized_macros = {
                    "protein": mps["protein"] * servings_val,
                    "carbs": mps["carbs"] * servings_val,
                    "fat": mps["fat"] * servings_val,
                    "kcal": mps["kcal"] * servings_val,
                }

                optimized.append({
                    "subrecipe_id": s["subrecipe_id"],
                    "name": s["name"],
                    "meal_name": meal_key,
                    "meal_type": meal_type,
                    "servings": servings_val,
                    "macros": optimized_macros,
                })

            total_error = float(value(WEIGHT_PROTEIN * dev_P + WEIGHT_CARBS * dev_C + WEIGHT_FAT * dev_F))
            return optimized, total_error, day_totals

    # No feasible solution -> safe fallback (now guaranteed to work)
    return safe_fallback()
