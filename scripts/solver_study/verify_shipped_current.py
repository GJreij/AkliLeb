"""
verify_shipped.py — re-runs this study's exact 528-point harness against
the REAL, shipped services/mealplan_service.py (not solver_lab.py's
sandbox approximation), using the REAL is_main + max_serving data pulled
live from Supabase by recipe_fixture_shipped.py.

This is the re-verification step flagged as open in §11/§13 of the study
doc: v2's numbers so far were all from a simulated fixture with 4 guessed
main labels and no manual max_serving overrides. The real data turned out
to differ in three ways -- two meals have 2 mains (not 1), one recipe has
zero mains, and several non-mains carry intentional manual overrides
(e.g. Akli House Salad capped at 1 -- "nobody wants 4 salads", not an
oversight). This script measures the ACTUAL shipped behavior, overrides
included, nothing altered.

Same CSV schema as run_study.py's output (tagged version="v2_shipped"),
so build_workbook.py treats it as just another version to append.

Usage:
    venv/Scripts/python.exe scripts/solver_study/verify_shipped.py [--limit N]
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
from services import mealplan_service as prod  # the REAL, shipped module

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_study import classify_mode  # shared, corrected classifier (2026-08-18) -- see run_study.py

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POPULATION_PATH = os.path.join(DATA_DIR, "population.json")
FIXTURE_PATH = os.path.join(DATA_DIR, "recipe_fixture_shipped.json")
VERSION = "current_shipped_2026-08-18"

GOOD_TOL = prod.KCAL_TOLERANCES[0]  # 0.08

CSV_FIELDS = [
    "version", "diet_id", "diet_label", "diet_group", "combo",
    "target_kcal", "target_protein_g", "target_carbs_g", "target_fat_g",
    "actual_kcal", "actual_protein_g", "actual_carbs_g", "actual_fat_g",
    "dev_kcal_pct", "dev_protein_pct", "dev_carbs_pct", "dev_fat_pct",
    "solve_mode", "tolerance_tier", "culinary_pass", "serving_step",
    "wall_time_ms", "lp_attempts",
    "root_cause_category", "root_cause_detail",
    "culinary_cap_adherent", "max_subrecipe_kcal_share_pct", "max_subrecipe_servings",
]


def wire_fixture(fixture):
    subs_by_recipe = {info["recipe_id"]: info["subrecipes"] for info in fixture["recipes_by_meal_type"].values()}

    def _get_subs(recipe_id):
        return [dict(s) for s in subs_by_recipe.get(recipe_id, [])]

    prod.get_recipe_subrecipes = _get_subs


def wrap_attempt_counter():
    state = {"count": 0}
    original = prod._solve_lp_once

    def counting(*args, **kwargs):
        state["count"] += 1
        return original(*args, **kwargs)

    prod._solve_lp_once = counting
    return (lambda: state["count"]), (lambda: state.update(count=0))


def pct_dev(actual, target):
    if not target:
        return 0.0 if not actual else float("inf")
    return round((actual - target) / target * 100, 1)


def culinary_adherence(optimized_subs):
    by_type = {}
    for row in optimized_subs:
        mt = row.get("meal_type")
        if mt:
            by_type[mt] = by_type.get(mt, 0.0) + row["macros"]["kcal"]
    total = sum(by_type.values())
    if total <= 0:
        return True
    if "breakfast" in by_type and by_type["breakfast"] > prod.RELAXED_BREAKFAST_MAX_PCT * total:
        return False
    if "snack" in by_type and by_type["snack"] > prod.RELAXED_SNACK_MAX_PCT * total:
        return False
    if "lunch" in by_type and "dinner" in by_type:
        lunch, dinner = by_type["lunch"], by_type["dinner"]
        smaller = min(lunch, dinner)
        if smaller > 0 and abs(lunch - dinner) / smaller > prod.RELAXED_DINNER_LUNCH_DIFF_PCT:
            return False
    return True


def plate_shape_guardrails(optimized_subs):
    total_kcal = sum(row["macros"]["kcal"] for row in optimized_subs) or 1.0
    max_share = max((row["macros"]["kcal"] / total_kcal * 100 for row in optimized_subs), default=0.0)
    max_servings = max((row["servings"] for row in optimized_subs), default=0.0)
    return round(max_share, 1), max_servings


def solve_one(diet, combo_name, meal_types, fixed_recipes, get_attempts, reset_attempts):
    recipes_by_meal = {mt: {"recipe_id": fixed_recipes[mt], "meal_type": mt} for mt in meal_types}
    target = {"protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
              "fat_g": diet["fat_g"], "kcal": diet["kcal"]}

    reset_attempts()
    t0 = time.perf_counter()
    optimized_subs, _loss, day_totals = prod.optimize_subrecipes(dict(recipes_by_meal), dict(target))
    wall_ms = round((time.perf_counter() - t0) * 1000, 2)
    attempts = get_attempts()

    mode = classify_mode(day_totals)
    adherent = culinary_adherence(optimized_subs)
    max_share, max_servings = plate_shape_guardrails(optimized_subs)

    return {
        "diet_id": diet["diet_id"], "diet_label": diet["label"], "diet_group": diet["group"],
        "combo": combo_name,
        "target": target,
        "actual": {
            "protein_g": day_totals.get("protein"), "carbs_g": day_totals.get("carbs"),
            "fat_g": day_totals.get("fat"), "kcal": day_totals.get("kcal"),
        },
        "deviation_pct": {
            "protein": pct_dev(day_totals.get("protein", 0), target["protein_g"]),
            "carbs":   pct_dev(day_totals.get("carbs", 0), target["carbs_g"]),
            "fat":     pct_dev(day_totals.get("fat", 0), target["fat_g"]),
            "kcal":    pct_dev(day_totals.get("kcal", 0), target["kcal"]),
        },
        "solve_mode": mode,
        "tolerance_tier": day_totals.get("tolerance_used"),
        "culinary_pass": day_totals.get("culinary_pass"),
        "serving_step": day_totals.get("serving_step_used"),
        "wall_time_ms": wall_ms,
        "lp_attempts": attempts,
        "root_cause_category": "",
        "root_cause_detail": "",
        "culinary_cap_adherent": adherent,
        "max_subrecipe_kcal_share_pct": max_share,
        "max_subrecipe_servings": max_servings,
    }


def to_csv_row(r):
    return {
        "version": VERSION,
        "diet_id": r["diet_id"], "diet_label": r["diet_label"], "diet_group": r["diet_group"],
        "combo": r["combo"],
        "target_kcal": r["target"]["kcal"], "target_protein_g": r["target"]["protein_g"],
        "target_carbs_g": r["target"]["carbs_g"], "target_fat_g": r["target"]["fat_g"],
        "actual_kcal": r["actual"]["kcal"], "actual_protein_g": r["actual"]["protein_g"],
        "actual_carbs_g": r["actual"]["carbs_g"], "actual_fat_g": r["actual"]["fat_g"],
        "dev_kcal_pct": r["deviation_pct"]["kcal"], "dev_protein_pct": r["deviation_pct"]["protein"],
        "dev_carbs_pct": r["deviation_pct"]["carbs"], "dev_fat_pct": r["deviation_pct"]["fat"],
        "solve_mode": r["solve_mode"], "tolerance_tier": r["tolerance_tier"],
        "culinary_pass": r["culinary_pass"], "serving_step": r["serving_step"],
        "wall_time_ms": r["wall_time_ms"], "lp_attempts": r["lp_attempts"],
        "root_cause_category": r["root_cause_category"], "root_cause_detail": r["root_cause_detail"],
        "culinary_cap_adherent": r["culinary_cap_adherent"],
        "max_subrecipe_kcal_share_pct": r["max_subrecipe_kcal_share_pct"],
        "max_subrecipe_servings": r["max_subrecipe_servings"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    with open(POPULATION_PATH, encoding="utf-8") as f:
        population = json.load(f)["diets"]
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        fixture = json.load(f)
    fixed_recipes = fixture["fixed_recipes"]
    meal_combos = fixture["meal_combos"]

    if args.limit:
        population = population[: args.limit]

    wire_fixture(fixture)
    get_attempts, reset_attempts = wrap_attempt_counter()

    total = len(population) * len(meal_combos)
    print(f"Running version={VERSION} against REAL services/mealplan_service.py: "
          f"{len(population)} diets x {len(meal_combos)} combos = {total} points")

    results = []
    t_start = time.perf_counter()
    done = 0
    for diet in population:
        for combo_name, meal_types in meal_combos.items():
            r = solve_one(diet, combo_name, meal_types, fixed_recipes, get_attempts, reset_attempts)
            results.append(r)
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  {done}/{total} ({time.perf_counter() - t_start:.1f}s elapsed)")

    elapsed = round(time.perf_counter() - t_start, 1)
    print(f"\nDone in {elapsed}s")

    os.makedirs(DATA_DIR, exist_ok=True)
    json_path = os.path.join(DATA_DIR, f"results_{VERSION}.json")
    csv_path = os.path.join(DATA_DIR, f"results_{VERSION}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "n_points": len(results), "elapsed_s": elapsed, "results": results}, f, indent=2)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow(to_csv_row(r))

    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")

    from collections import Counter
    modes = Counter(r["solve_mode"] for r in results)
    print("\nSolve-mode distribution:")
    for k, v in modes.most_common():
        print(f"  {k:<20} {v:>4}  ({v/len(results):.1%})")
    breaches = sum(1 for r in results if not r["culinary_cap_adherent"])
    print(f"\nCulinary-cap guardrail breaches: {breaches}/{len(results)}")


if __name__ == "__main__":
    main()
