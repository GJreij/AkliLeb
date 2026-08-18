"""
macro_free_deviation_study.py — for every (diet, combo) point, drop the
protein/carbs/fat hard tolerance bands entirely and let the existing
objective (percentage-normalised weighted deviation, same weights as
production) find its best fit. Kcal's hard band stays at strict/8% — the
ablation study showed kcal is never the sole blocker, so it isn't "a
macro tolerance" in the same sense protein/carbs/fat are, and holding it
fixed keeps this a clean single-variable question: given free rein on
macro composition, how far off does each macro actually land?

Two tiers, tried in order, per point:
  A. kcal_strict_macro_free — hard_bounds=True, tol=8% (kcal only binds),
     macro_tols effectively infinite, strict culinary, step 1.0 then 0.5.
  B. full_best_effort — A was infeasible (kcal itself unreachable at this
     recipe combo/serving ceiling) -> drop ALL hard bounds, same objective,
     strict culinary then relaxed, step 1.0 then 0.5. Mirrors production's
     own BEST_EFFORT_LP pass exactly, so every point still gets a number.

Usage:
    venv/Scripts/python.exe scripts/solver_study/macro_free_deviation_study.py [--limit N]
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solver_lab as svc
import diagnostics

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POPULATION_PATH = os.path.join(DATA_DIR, "population.json")
FIXTURE_PATH = os.path.join(DATA_DIR, "recipe_fixture.json")

GOOD_TOL = svc.KCAL_TOLERANCES[0]  # 0.08
HUGE = 1000.0

CSV_FIELDS = [
    "diet_id", "diet_label", "diet_group", "combo",
    "target_kcal", "target_protein_g", "target_carbs_g", "target_fat_g",
    "actual_kcal", "actual_protein_g", "actual_carbs_g", "actual_fat_g",
    "dev_kcal_pct", "dev_protein_pct", "dev_carbs_pct", "dev_fat_pct",
    "tier", "culinary_pass", "serving_step",
]


def wire_fixture(fixture):
    subs_by_recipe, rules_by_recipe = {}, {}
    for meal_type, info in fixture["recipes_by_meal_type"].items():
        subs_by_recipe[info["recipe_id"]] = info["subrecipes"]
        rules_by_recipe[info["recipe_id"]] = info["rules"]

    def _get_subs(recipe_id):
        return [dict(s) for s in subs_by_recipe.get(recipe_id, [])]

    def _get_rules(recipe_id):
        return list(rules_by_recipe.get(recipe_id, []))

    svc.get_recipe_subrecipes = _get_subs
    svc.get_recipe_rules = _get_rules


def pct_dev(actual, target):
    if not target:
        return 0.0 if not actual else float("inf")
    return round((actual - target) / target * 100, 1)


def try_attempt(all_subs, rbm, P_t, C_t, F_t, kcal_t, rules, tol, hard_bounds, strict_culinary, step):
    macro_tols = {"protein": HUGE, "carbs": HUGE, "fat": HUGE}
    return svc._solve_lp_once(
        all_subs=all_subs, recipes_by_meal=rbm,
        P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
        serving_step=step, tol=tol, macro_tols=macro_tols,
        allow_under_kcal=False, strict_culinary=strict_culinary,
        resolved_rules=rules, hard_bounds=hard_bounds,
    )


def solve_point(all_subs, rbm, target, rules):
    P_t, C_t, F_t, kcal_t = target["protein_g"], target["carbs_g"], target["fat_g"], target["kcal"]

    # Tier A: kcal strict (8%), macro bands free, culinary strict.
    for step in (1.0, 0.5):
        result = try_attempt(all_subs, rbm, P_t, C_t, F_t, kcal_t, rules,
                              tol=GOOD_TOL, hard_bounds=True, strict_culinary=True, step=step)
        if result is not None:
            optimized_subs, _err, totals = result
            return optimized_subs, totals, "kcal_strict_macro_free", "strict", step

    # Tier B: kcal itself unreachable at this combo -> full best-effort,
    # exactly mirroring production's own last-resort pass (culinary strict
    # then relaxed, step 1.0 then 0.5).
    for strict_culinary in (True, False):
        for step in (1.0, 0.5):
            result = try_attempt(all_subs, rbm, P_t, C_t, F_t, kcal_t, rules,
                                  tol=GOOD_TOL, hard_bounds=False, strict_culinary=strict_culinary, step=step)
            if result is not None:
                optimized_subs, _err, totals = result
                pass_label = "strict" if strict_culinary else "relaxed"
                return optimized_subs, totals, "full_best_effort", pass_label, step

    return None, None, "INFEASIBLE", None, None


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

    total = len(population) * len(meal_combos)
    print(f"Running macro-free deviation study: {len(population)} diets x {len(meal_combos)} combos = {total} points")

    rows = []
    t0 = time.perf_counter()
    done = 0
    for diet in population:
        target = {"protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
                  "fat_g": diet["fat_g"], "kcal": diet["kcal"]}
        for combo_name, meal_types in meal_combos.items():
            rbm = {mt: {"recipe_id": fixed_recipes[mt], "meal_type": mt} for mt in meal_types}
            all_subs = diagnostics.build_all_subs(rbm)
            diagnostics.apply_scale_guard(all_subs, target["kcal"])
            rules = svc._resolve_rules_for_day(all_subs, rbm)

            _optimized_subs, totals, tier, culinary_pass, step = solve_point(all_subs, rbm, target, rules)

            if tier == "INFEASIBLE":
                rows.append({
                    "diet_id": diet["diet_id"], "diet_label": diet["label"], "diet_group": diet["group"],
                    "combo": combo_name, "target_kcal": target["kcal"], "target_protein_g": target["protein_g"],
                    "target_carbs_g": target["carbs_g"], "target_fat_g": target["fat_g"],
                    "actual_kcal": None, "actual_protein_g": None, "actual_carbs_g": None, "actual_fat_g": None,
                    "dev_kcal_pct": None, "dev_protein_pct": None, "dev_carbs_pct": None, "dev_fat_pct": None,
                    "tier": tier, "culinary_pass": None, "serving_step": None,
                })
            else:
                rows.append({
                    "diet_id": diet["diet_id"], "diet_label": diet["label"], "diet_group": diet["group"],
                    "combo": combo_name, "target_kcal": target["kcal"], "target_protein_g": target["protein_g"],
                    "target_carbs_g": target["carbs_g"], "target_fat_g": target["fat_g"],
                    "actual_kcal": totals["kcal"], "actual_protein_g": totals["protein"],
                    "actual_carbs_g": totals["carbs"], "actual_fat_g": totals["fat"],
                    "dev_kcal_pct": pct_dev(totals["kcal"], target["kcal"]),
                    "dev_protein_pct": pct_dev(totals["protein"], target["protein_g"]),
                    "dev_carbs_pct": pct_dev(totals["carbs"], target["carbs_g"]),
                    "dev_fat_pct": pct_dev(totals["fat"], target["fat_g"]),
                    "tier": tier, "culinary_pass": culinary_pass, "serving_step": step,
                })
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  {done}/{total} ({time.perf_counter() - t0:.1f}s elapsed)")

    elapsed = round(time.perf_counter() - t0, 1)
    print(f"\nDone in {elapsed}s")

    os.makedirs(DATA_DIR, exist_ok=True)
    json_path = os.path.join(DATA_DIR, "macro_free_deviation.json")
    csv_path = os.path.join(DATA_DIR, "macro_free_deviation.csv")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"n_points": len(rows), "elapsed_s": elapsed, "rows": rows}, f, indent=2)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")

    tiers = defaultdict(int)
    for r in rows:
        tiers[r["tier"]] += 1
    print("\nTier distribution:")
    for k, v in sorted(tiers.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<24} {v:>4}  ({v/len(rows):.1%})")

    solved_rows = [r for r in rows if r["tier"] != "INFEASIBLE"]
    print(f"\nDeviation stats across {len(solved_rows)} solved points (kcal held strict/8%, macros free):")
    for dev_key, label in [("dev_protein_pct", "protein"), ("dev_carbs_pct", "carbs"),
                            ("dev_fat_pct", "fat"), ("dev_kcal_pct", "kcal")]:
        vals = sorted(abs(r[dev_key]) for r in solved_rows)
        n = len(vals)
        avg = sum(vals) / n
        median = vals[n // 2]
        p90 = vals[int(n * 0.9)]
        print(f"  |{label:<8}dev|  avg={avg:>6.1f}%  median={median:>6.1f}%  p90={p90:>6.1f}%  max={vals[-1]:>6.1f}%")


if __name__ == "__main__":
    main()
