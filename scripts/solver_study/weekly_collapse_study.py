"""
weekly_collapse_study.py — Parts 1 & 2 of the daily_menu template study.

Part 1: quantifies the "Friday collapse" — proves that BASELINE (faithful
clone of current production, including the two dead signals: category
overlap always 0, weekday popularity always flat 0.5) degrades across the
week because recent_global/meal_history are single-call deques with no
whole-week visibility, on a recipe pool this small (18 recipes, 4-5 of
them breakfast-eligible - see menu_pool_fixture.json / pull_menu_pool.py).

Part 2: compares three variants on the SAME real pool + SAME diet
population, to attribute how much of the fix is which part:

  BASELINE       - current production, dead signals included.
  FIXED_SIGNALS  - same greedy day-by-day walk, real popularity +
                   real subrecipe-similarity signal instead of the dead
                   ones. Isolates "does repairing the signals alone help".
  HOLISTIC       - whole-week allocator (menu_lab.run_holistic_week):
                   quality-ranks recipes independent of day, schedules
                   appearances with explicit spacing, resolves same-day
                   cross-meal-type collisions. No random walk, no
                   recency-window compounding.

BASELINE/FIXED_SIGNALS are stochastic (weighted random choice + best-of-N
tries) so they're run across N_SEEDS per diet and averaged. HOLISTIC is
deterministic given a diet, run once per diet.

Usage:
    venv/Scripts/python.exe scripts/solver_study/weekly_collapse_study.py [--seeds N] [--diets N]
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import menu_lab as ml

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POOL_PATH = os.path.join(DATA_DIR, "menu_pool_fixture.json")
POPULATION_PATH = os.path.join(DATA_DIR, "population.json")

MEALS_MAP = {"breakfast": "breakfast", "lunch": "lunch", "snack": "snack", "dinner": "dinner"}
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]

CSV_FIELDS = [
    "config", "diet_id", "seed", "weekday", "weekday_name",
    "failed", "single_sub_meals", "score",
    "pool_breakfast", "pool_lunch", "pool_dinner", "pool_snack",
    "used_relaxed_any",
]


def macro_target_from_diet(diet: dict) -> dict:
    return {
        "protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
        "fat_g": diet["fat_g"], "kcal": diet["kcal"],
    }


def run_holistic_rows(eligible, flex_stats, popularity, recipe_macros, diets):
    rows = []
    for diet in diets:
        macro_target = macro_target_from_diet(diet)
        days = ml.run_holistic_week(MEALS_MAP, eligible, flex_stats, popularity, recipe_macros, macro_target)
        for d in days:
            rows.append({
                "config": "HOLISTIC", "diet_id": diet["diet_id"], "seed": 0,
                "weekday": d["weekday"], "weekday_name": WEEKDAY_NAMES[d["weekday"]],
                "failed": d["failed"],
                "single_sub_meals": d.get("single_sub_meals"),
                "score": d.get("score"),
                "pool_breakfast": None, "pool_lunch": None, "pool_dinner": None, "pool_snack": None,
                "used_relaxed_any": None,
            })
    return rows


def summarize(rows, config_name):
    by_weekday = defaultdict(list)
    for r in rows:
        if r["config"] == config_name:
            by_weekday[r["weekday"]].append(r)

    print(f"\n=== {config_name} ===")
    header = f"{'day':<5}{'fail%':>7}{'avg_score':>12}{'single_sub':>12}"
    if config_name != "HOLISTIC":
        header += f"{'pool_bkfst':>12}{'pool_lunch':>12}{'pool_dinner':>12}{'pool_snack':>12}"
    print(header)

    for wd in range(5):
        wd_rows = by_weekday.get(wd, [])
        if not wd_rows:
            continue
        n = len(wd_rows)
        fail_pct = 100.0 * sum(1 for r in wd_rows if r["failed"]) / n
        scored = [r["score"] for r in wd_rows if r["score"] is not None]
        avg_score = sum(scored) / len(scored) if scored else float("nan")
        ss = [r["single_sub_meals"] for r in wd_rows if r["single_sub_meals"] is not None]
        avg_ss = sum(ss) / len(ss) if ss else float("nan")
        line = f"{WEEKDAY_NAMES[wd]:<5}{fail_pct:>6.1f}%{avg_score:>12.1f}{avg_ss:>12.2f}"
        if config_name != "HOLISTIC":
            for pk in ("pool_breakfast", "pool_lunch", "pool_dinner", "pool_snack"):
                vals = [r[pk] for r in wd_rows if r[pk] is not None]
                avg_pool = sum(vals) / len(vals) if vals else float("nan")
                line += f"{avg_pool:>12.2f}"
        print(line)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--diets", type=int, default=8)
    args = parser.parse_args()

    with open(POOL_PATH) as f:
        fixture = json.load(f)
    with open(POPULATION_PATH) as f:
        population = json.load(f)

    recipes, flex_stats, recipe_macros, subrecipe_sets, popularity = ml.build_pool(fixture)
    eligible = ml.eligible_by_meal_type_from_pool(recipes)

    # Spread diets across the population's groups so the study isn't
    # accidentally tuned to one macro profile.
    diets_all = population["diets"]
    step = max(1, len(diets_all) // args.diets)
    diets = diets_all[::step][:args.diets]
    seeds = list(range(args.seeds))

    print(f"Pool: {len(recipes)} recipes, eligible={ {k: len(v) for k, v in eligible.items()} }")
    print(f"Diets tested: {[d['diet_id'] for d in diets]}")
    print(f"Seeds per (config, diet): {len(seeds)}")

    rows = []

    def run_greedy(config_name, cfg):
        for diet in diets:
            macro_target = macro_target_from_diet(diet)
            for seed in seeds:
                days = ml.run_greedy_week(
                    MEALS_MAP, eligible, {}, flex_stats, popularity, recipe_macros,
                    macro_target, subrecipe_sets, cfg, seed=seed,
                )
                for d in days:
                    pool = d.get("pool_sizes", {})
                    rows.append({
                        "config": config_name, "diet_id": diet["diet_id"], "seed": seed,
                        "weekday": d["weekday"], "weekday_name": WEEKDAY_NAMES[d["weekday"]],
                        "failed": d["failed"],
                        "single_sub_meals": d.get("single_sub_meals"),
                        "score": d.get("score"),
                        "pool_breakfast": pool.get("breakfast"),
                        "pool_lunch": pool.get("lunch"),
                        "pool_dinner": pool.get("dinner"),
                        "pool_snack": pool.get("snack"),
                        "used_relaxed_any": None,
                    })
                    if d["failed"]:
                        break

    run_greedy("BASELINE", ml.BASELINE_CFG)
    run_greedy("FIXED_SIGNALS", ml.FIXED_SIGNALS_CFG)
    rows.extend(run_holistic_rows(eligible, flex_stats, popularity, recipe_macros, diets))

    os.makedirs(DATA_DIR, exist_ok=True)
    out_csv = os.path.join(DATA_DIR, "weekly_collapse_results.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

    summarize(rows, "BASELINE")
    summarize(rows, "FIXED_SIGNALS")
    summarize(rows, "HOLISTIC")

    print(f"\nWrote {len(rows)} rows -> {out_csv}")


if __name__ == "__main__":
    main()
