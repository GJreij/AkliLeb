import random
from typing import Dict, Any, List, Tuple
from datetime import datetime
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
                "kcal": sub.get("kcal") or 0.0,
                "protein": sub.get("protein") or 0.0,
                "carbs": sub.get("carbs") or 0.0,
                "fat": sub.get("fat") or 0.0,
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
      { "protein_g": 150, "carbs_g": 200, "fat_g": 60 }

    Returns:
      optimized_subs: [
        {
          "subrecipe_id": 10,
          "name": "Oats base",
          "meal_name": "breakfast",
          "meal_type": "breakfast",
          "servings": 2,
          "macros": { "protein": ..., "carbs": ..., "fat": ..., "kcal": ... }
        },
        ...
      ],
      total_error: float,
      totals: { "protein": ..., "carbs": ..., "fat": ..., "kcal": ..., "tolerance_used": ... }
    """

    # -------------------------------------------------------------
    # Config
    # -------------------------------------------------------------
    KCAL_TOLERANCES = [0.10, 0.15, 0.20]

    BREAKFAST_MAX_PCT = 0.30
    SNACK_MAX_PCT = 0.20
    DINNER_LUNCH_DIFF_PCT = 0.20
    NO_DINNER_YES_LUNCH_PCT = 0.6
    NO_LUNCH_YES_DINNER_PCT = 0.6

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
                "max_serving": s["max_serving"] or DEFAULT_MAX_SERVING,
            })

    if not all_subs:
        return [], 0.0, {"protein": 0, "carbs": 0, "fat": 0, "kcal": 0}

    # Targets
    P_t = macro_target.get("protein_g") or 0.0
    C_t = macro_target.get("carbs_g") or 0.0
    F_t = macro_target.get("fat_g") or 0.0
    kcal_t = 4 * (P_t + C_t) + 9 * F_t

    # Try several kcal tolerances until feasible
    for tol in KCAL_TOLERANCES:
        prob = LpProblem(f"MealPlanOptimization_{int(tol * 100)}", LpMinimize)

        # Variables: servings per subrecipe
        servings = {
            i: LpVariable(
                f"x_{i}",
                lowBound=SERVING_MIN,
                upBound=s["max_serving"],
                cat=LpInteger,
            )
            for i, s in enumerate(all_subs)
        }

        # Total macros
        total_P = lpSum(servings[i] * s["macros"]["protein"] for i, s in enumerate(all_subs))
        total_C = lpSum(servings[i] * s["macros"]["carbs"] for i, s in enumerate(all_subs))
        total_F = lpSum(servings[i] * s["macros"]["fat"] for i, s in enumerate(all_subs))
        total_K = lpSum(servings[i] * s["macros"]["kcal"] for i, s in enumerate(all_subs))

        # Deviation variables
        dev_P = LpVariable("dev_P", lowBound=0)
        dev_C = LpVariable("dev_C", lowBound=0)
        dev_F = LpVariable("dev_F", lowBound=0)

        # |x - target| via linear constraints
        prob += total_P - P_t <= dev_P
        prob += -(total_P - P_t) <= dev_P
        prob += total_C - C_t <= dev_C
        prob += -(total_C - C_t) <= dev_C
        prob += total_F - F_t <= dev_F
        prob += -(total_F - F_t) <= dev_F

        # Kcal by meal_type
        kcal_by_type: Dict[str, Any] = {}
        for i, s in enumerate(all_subs):
            meal_key = s["meal"]
            meal_type = recipes_by_meal.get(meal_key, {}).get("meal_type")
            if not meal_type:
                continue
            if meal_type not in kcal_by_type:
                kcal_by_type[meal_type] = servings[i] * s["macros"]["kcal"]
            else:
                kcal_by_type[meal_type] += servings[i] * s["macros"]["kcal"]

        # Apply constraints according to which meal types exist
        if {"snack", "breakfast", "dinner", "lunch"} <= set(kcal_by_type.keys()):
            prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * kcal_t
            prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * kcal_t
            d = kcal_by_type["dinner"]
            l = kcal_by_type["lunch"]
            prob += d - l <= DINNER_LUNCH_DIFF_PCT * l
            prob += l - d <= DINNER_LUNCH_DIFF_PCT * d

        if "snack" in kcal_by_type and "breakfast" not in kcal_by_type and "dinner" in kcal_by_type and "lunch" in kcal_by_type:
            prob += kcal_by_type["snack"] <= (SNACK_MAX_PCT + 0.1) * kcal_t
            d = kcal_by_type["dinner"]
            l = kcal_by_type["lunch"]
            prob += d - l <= DINNER_LUNCH_DIFF_PCT * l
            prob += l - d <= DINNER_LUNCH_DIFF_PCT * d

        if "snack" not in kcal_by_type and "breakfast" not in kcal_by_type and "dinner" in kcal_by_type and "lunch" in kcal_by_type:
            d = kcal_by_type["dinner"]
            l = kcal_by_type["lunch"]
            prob += d - l <= DINNER_LUNCH_DIFF_PCT * l
            prob += l - d <= DINNER_LUNCH_DIFF_PCT * d

        if "snack" in kcal_by_type and "breakfast" in kcal_by_type and "dinner" not in kcal_by_type and "lunch" in kcal_by_type:
            prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * kcal_t
            prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * kcal_t
            prob += kcal_by_type["lunch"] <= NO_DINNER_YES_LUNCH_PCT * kcal_t

        if "snack" in kcal_by_type and "breakfast" in kcal_by_type and "dinner" in kcal_by_type and "lunch" not in kcal_by_type:
            prob += kcal_by_type["snack"] <= SNACK_MAX_PCT * kcal_t
            prob += kcal_by_type["breakfast"] <= BREAKFAST_MAX_PCT * kcal_t
            prob += kcal_by_type["dinner"] <= NO_LUNCH_YES_DINNER_PCT * kcal_t

        # Kcal tolerance
        prob += total_K >= (1 - tol) * kcal_t
        prob += total_K <= (1 + tol) * kcal_t

        # Objective
        prob += (
            WEIGHT_PROTEIN * dev_P +
            WEIGHT_CARBS * dev_C +
            WEIGHT_FAT * dev_F
        )

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

                macros_per_serving = s["macros"]
                optimized_macros = {
                    "protein": macros_per_serving["protein"] * servings_val,
                    "carbs": macros_per_serving["carbs"] * servings_val,
                    "fat": macros_per_serving["fat"] * servings_val,
                    "kcal": macros_per_serving["kcal"] * servings_val,
                }

                optimized.append({
                    "subrecipe_id": s["subrecipe_id"],
                    "name": s["name"],
                    "meal_name": meal_key,     # will match meal_key in recipes_by_meal
                    "meal_type": meal_type,
                    "servings": servings_val,
                    "macros": optimized_macros,
                })

            total_error = float(value(
                WEIGHT_PROTEIN * dev_P +
                WEIGHT_CARBS * dev_C +
                WEIGHT_FAT * dev_F
            ))

            return optimized, total_error, day_totals

    # No feasible solution
    return [], None, {
        "protein": 0,
        "carbs": 0,
        "fat": 0,
        "kcal": 0,
        "tolerance_used": None,
        "error": "No feasible solution found within ±20% kcal tolerance",
    }
