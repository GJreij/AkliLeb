"""
run_real_orders.py — re-solves every real historical client day (pulled by
pull_real_orders.py from meal_plan_day / daily_macro_order /
meal_plan_day_recipe / meal_plan_day_recipe_serving) through v3 and v4,
and compares both against:
  (a) the day's real macro target (daily_macro_order), and
  (b) what production actually delivered at the time (meal_plan_day_recipe_serving)

This is a genuinely different population from the 528-point synthetic+10-real
harness: real recipe combinations as clients actually received them, not the
4 fixed recipes x 6 synthetic combos.

Usage:
    venv/Scripts/python.exe scripts/solver_study/run_real_orders.py [--limit N]

Writes:
    data/real_orders_results.json
    data/real_orders_results.csv
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solver_lab as svc
from run_study import classify_mode, pct_dev, culinary_adherence, plate_shape_guardrails, wrap_attempt_counter

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DAYS_PATH = os.path.join(DATA_DIR, "real_orders_days.json")
FIXTURE_PATH = os.path.join(DATA_DIR, "real_orders_fixture.json")

VERSIONS = {
    "v3": svc.optimize_subrecipes_v3,
    "v4": svc.optimize_subrecipes_v4,
    "v5": svc.optimize_subrecipes_v5,
}

CSV_FIELDS = [
    "version", "day_id", "date", "meal_types",
    "target_kcal", "target_protein_g", "target_carbs_g", "target_fat_g",
    "actual_kcal", "actual_protein_g", "actual_carbs_g", "actual_fat_g",
    "dev_kcal_pct", "dev_protein_pct", "dev_carbs_pct", "dev_fat_pct",
    "prod_kcal", "prod_protein_g", "prod_carbs_g", "prod_fat_g",
    "prod_dev_kcal_pct", "prod_dev_protein_pct", "prod_dev_carbs_pct", "prod_dev_fat_pct",
    "solve_mode", "tolerance_tier", "culinary_pass", "serving_step",
    "wall_time_ms", "lp_attempts",
    "culinary_cap_adherent", "max_subrecipe_kcal_share_pct", "max_subrecipe_servings",
]


def wire_fixture(fixture):
    subs_by_recipe = {int(k): v for k, v in fixture["subs_by_recipe"].items()}

    def _get_subs(recipe_id):
        return [dict(s) for s in subs_by_recipe.get(recipe_id, [])]

    def _get_rules(recipe_id):
        return []

    svc.get_recipe_subrecipes = _get_subs
    svc.get_recipe_rules = _get_rules


def solve_one(day, version, solve_fn, get_attempts, reset_attempts):
    recipes_by_meal = day["recipes_by_meal"]
    target = day["target"]

    reset_attempts()
    t0 = time.perf_counter()
    optimized_subs, _loss, day_totals = solve_fn(dict(recipes_by_meal), dict(target))
    wall_ms = round((time.perf_counter() - t0) * 1000, 2)
    attempts = get_attempts()

    mode = classify_mode(day_totals)
    adherent = culinary_adherence(optimized_subs)
    max_share, max_servings = plate_shape_guardrails(optimized_subs)

    actual = {
        "protein_g": day_totals.get("protein"), "carbs_g": day_totals.get("carbs"),
        "fat_g": day_totals.get("fat"), "kcal": day_totals.get("kcal"),
    }
    dev = {
        "protein": pct_dev(actual["protein_g"] or 0, target["protein_g"]),
        "carbs":   pct_dev(actual["carbs_g"] or 0, target["carbs_g"]),
        "fat":     pct_dev(actual["fat_g"] or 0, target["fat_g"]),
        "kcal":    pct_dev(actual["kcal"] or 0, target["kcal"]),
    }

    prod = day.get("production_actual")
    prod_dev = None
    if prod:
        prod_dev = {
            "protein": pct_dev(prod["protein"], target["protein_g"]),
            "carbs":   pct_dev(prod["carbs"], target["carbs_g"]),
            "fat":     pct_dev(prod["fat"], target["fat_g"]),
            "kcal":    pct_dev(prod["kcal"], target["kcal"]),
        }

    return {
        "version": version,
        "day_id": day["day_id"], "date": day["date"],
        "meal_types": sorted(recipes_by_meal.keys()),
        "target": target,
        "actual": actual,
        "deviation_pct": dev,
        "production_actual": prod,
        "production_deviation_pct": prod_dev,
        "solve_mode": mode,
        "tolerance_tier": day_totals.get("tolerance_used"),
        "culinary_pass": day_totals.get("culinary_pass"),
        "serving_step": day_totals.get("serving_step_used"),
        "wall_time_ms": wall_ms,
        "lp_attempts": attempts,
        "culinary_cap_adherent": adherent,
        "max_subrecipe_kcal_share_pct": max_share,
        "max_subrecipe_servings": max_servings,
    }


def to_csv_row(r):
    prod = r.get("production_actual") or {}
    prod_dev = r.get("production_deviation_pct") or {}
    return {
        "version": r["version"], "day_id": r["day_id"], "date": r["date"],
        "meal_types": "|".join(r["meal_types"]),
        "target_kcal": r["target"]["kcal"], "target_protein_g": r["target"]["protein_g"],
        "target_carbs_g": r["target"]["carbs_g"], "target_fat_g": r["target"]["fat_g"],
        "actual_kcal": r["actual"]["kcal"], "actual_protein_g": r["actual"]["protein_g"],
        "actual_carbs_g": r["actual"]["carbs_g"], "actual_fat_g": r["actual"]["fat_g"],
        "dev_kcal_pct": r["deviation_pct"]["kcal"], "dev_protein_pct": r["deviation_pct"]["protein"],
        "dev_carbs_pct": r["deviation_pct"]["carbs"], "dev_fat_pct": r["deviation_pct"]["fat"],
        "prod_kcal": prod.get("kcal"), "prod_protein_g": prod.get("protein"),
        "prod_carbs_g": prod.get("carbs"), "prod_fat_g": prod.get("fat"),
        "prod_dev_kcal_pct": prod_dev.get("kcal"), "prod_dev_protein_pct": prod_dev.get("protein"),
        "prod_dev_carbs_pct": prod_dev.get("carbs"), "prod_dev_fat_pct": prod_dev.get("fat"),
        "solve_mode": r["solve_mode"], "tolerance_tier": r["tolerance_tier"],
        "culinary_pass": r["culinary_pass"], "serving_step": r["serving_step"],
        "wall_time_ms": r["wall_time_ms"], "lp_attempts": r["lp_attempts"],
        "culinary_cap_adherent": r["culinary_cap_adherent"],
        "max_subrecipe_kcal_share_pct": r["max_subrecipe_kcal_share_pct"],
        "max_subrecipe_servings": r["max_subrecipe_servings"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    with open(DAYS_PATH, encoding="utf-8") as f:
        days = json.load(f)["days"]
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        fixture = json.load(f)

    if args.limit:
        days = days[: args.limit]

    wire_fixture(fixture)
    get_attempts, reset_attempts = wrap_attempt_counter()

    all_results = []
    for version, solve_fn in VERSIONS.items():
        print(f"\n=== version={version}: {len(days)} real historical days ===")
        t_start = time.perf_counter()
        for i, day in enumerate(days, 1):
            r = solve_one(day, version, solve_fn, get_attempts, reset_attempts)
            all_results.append(r)
            if i % 50 == 0 or i == len(days):
                print(f"  {i}/{len(days)} ({time.perf_counter() - t_start:.1f}s elapsed)")
        elapsed = round(time.perf_counter() - t_start, 1)
        print(f"  Done in {elapsed}s")

    os.makedirs(DATA_DIR, exist_ok=True)
    json_path = os.path.join(DATA_DIR, "real_orders_results.json")
    csv_path = os.path.join(DATA_DIR, "real_orders_results.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"n_days": len(days), "results": all_results}, f, indent=2)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in all_results:
            writer.writerow(to_csv_row(r))

    print(f"\nWrote {json_path}")
    print(f"Wrote {csv_path}")

    for version in VERSIONS:
        subset = [r for r in all_results if r["version"] == version]
        modes = Counter(r["solve_mode"] for r in subset)
        breaches = sum(1 for r in subset if not r["culinary_cap_adherent"])
        print(f"\n--- {version} solve-mode distribution ({len(subset)} days) ---")
        for k, v in modes.most_common():
            print(f"  {k:<20} {v:>4}  ({v/len(subset):.1%})")
        print(f"  Culinary-cap breaches: {breaches}/{len(subset)}")


if __name__ == "__main__":
    main()
