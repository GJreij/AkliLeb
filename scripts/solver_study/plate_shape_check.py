"""
plate_shape_check.py — follow-up to the ablation study's max_serving_5x
scenario. That ablation only recorded feasibility (Optimal or not); it
never looked at what the winning plate actually contained. This script
re-solves the same population under two scenarios and captures the ACTUAL
servings per subrecipe, to answer directly: does raising max_serving risk
a degenerate plate (e.g. many servings of one cheap-kcal subrecipe against
almost none of another)?

  A. max_serving_5x alone       — matches the ablation exactly (macro
                                   tolerance, balance ratio, culinary caps
                                   all still strict).
  B. max_serving_5x + macro_free — the realistic risk case: if a future
                                   v1 combines the two biggest ablation
                                   levers together, the macro-tolerance
                                   check that would reject a
                                   protein-deficient plate is also gone.

For every solved point, records the single largest per-subrecipe serving
count, which meal/subrecipe it was, and that subrecipe's share of the
day's total kcal -- then prints the worst offenders with full plate
breakdowns for a human sanity check.

Usage:
    venv/Scripts/python.exe scripts/solver_study/plate_shape_check.py [--limit N]
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solver_lab as svc
import diagnostics

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POPULATION_PATH = os.path.join(DATA_DIR, "population.json")
FIXTURE_PATH = os.path.join(DATA_DIR, "recipe_fixture.json")

GOOD_TOL = svc.KCAL_TOLERANCES[0]
BASE_MACRO = svc.BASE_MACRO_TOLERANCES[0]
HUGE = 1000.0
MAX_SERVING_MULTIPLIER = 5.0


def wire_fixture(fixture):
    subs_by_recipe, rules_by_recipe = {}, {}
    for meal_type, info in fixture["recipes_by_meal_type"].items():
        subs_by_recipe[info["recipe_id"]] = info["subrecipes"]
        rules_by_recipe[info["recipe_id"]] = info["rules"]
    svc.get_recipe_subrecipes = lambda rid: [dict(s) for s in subs_by_recipe.get(rid, [])]
    svc.get_recipe_rules = lambda rid: list(rules_by_recipe.get(rid, []))


def normal_macro_tols(P_t, C_t, F_t, kcal_t):
    return {
        "protein": svc.macro_tolerance("protein", P_t, kcal_t, BASE_MACRO),
        "carbs":   svc.macro_tolerance("carbs",   C_t, kcal_t, BASE_MACRO),
        "fat":     svc.macro_tolerance("fat",     F_t, kcal_t, BASE_MACRO),
    }


def attempt(all_subs, rbm, P_t, C_t, F_t, kcal_t, rules, macro_tols, hard_bounds=True):
    for step in (1.0, 0.5):
        result = svc._solve_lp_once(
            all_subs=all_subs, recipes_by_meal=rbm,
            P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
            serving_step=step, tol=GOOD_TOL, macro_tols=macro_tols,
            allow_under_kcal=False, strict_culinary=True,
            resolved_rules=rules, hard_bounds=hard_bounds,
        )
        if result is not None:
            return result
    return None


def plate_shape(optimized_subs):
    total_kcal = sum(r["macros"]["kcal"] for r in optimized_subs) or 1.0
    worst = max(optimized_subs, key=lambda r: r["servings"])
    return {
        "max_servings": worst["servings"],
        "max_servings_subrecipe": worst["name"],
        "max_servings_meal": worst["meal_type"],
        "max_servings_kcal_share_pct": round(worst["macros"]["kcal"] / total_kcal * 100, 1),
        "plate": [
            {"meal": r["meal_type"], "name": r["name"], "servings": r["servings"],
             "kcal": round(r["macros"]["kcal"], 0)}
            for r in optimized_subs
        ],
    }


def run_scenario(points, scenario_name, max_serving_mult, macro_free):
    rows = []
    for pt in points:
        subs = pt["all_subs"]
        if max_serving_mult != 1.0:
            subs = [dict(s, max_serving=s["max_serving"] * max_serving_mult) for s in subs]
        t = pt["target"]
        macro_tols = ({"protein": HUGE, "carbs": HUGE, "fat": HUGE} if macro_free
                      else normal_macro_tols(t["protein_g"], t["carbs_g"], t["fat_g"], t["kcal"]))
        result = attempt(subs, pt["rbm"], t["protein_g"], t["carbs_g"], t["fat_g"], t["kcal"],
                          pt["rules"], macro_tols)
        if result is None:
            continue
        optimized_subs, _err, totals = result
        shape = plate_shape(optimized_subs)
        rows.append({
            "diet_id": pt["diet_id"], "diet_label": pt["diet_label"], "diet_group": pt["diet_group"],
            "combo": pt["combo"], "scenario": scenario_name,
            "target_kcal": t["kcal"], "actual_kcal": totals["kcal"],
            "actual_protein_g": totals["protein"], "target_protein_g": t["protein_g"],
            **shape,
        })
    return rows


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

    points = []
    for diet in population:
        target = {"protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
                  "fat_g": diet["fat_g"], "kcal": diet["kcal"]}
        for combo_name, meal_types in meal_combos.items():
            rbm = {mt: {"recipe_id": fixed_recipes[mt], "meal_type": mt} for mt in meal_types}
            all_subs = diagnostics.build_all_subs(rbm)
            diagnostics.apply_scale_guard(all_subs, target["kcal"])
            rules = svc._resolve_rules_for_day(all_subs, rbm)
            points.append({
                "diet_id": diet["diet_id"], "diet_label": diet["label"], "diet_group": diet["group"],
                "combo": combo_name, "rbm": rbm, "all_subs": all_subs, "rules": rules, "target": target,
            })

    print(f"{len(points)} points\n")
    t0 = time.perf_counter()

    scenario_a = run_scenario(points, "max_serving_5x_alone", MAX_SERVING_MULTIPLIER, macro_free=False)
    scenario_b = run_scenario(points, "max_serving_5x_plus_macro_free", MAX_SERVING_MULTIPLIER, macro_free=True)

    print(f"Done in {time.perf_counter() - t0:.1f}s\n")

    for label, rows in [("A: max_serving_5x alone", scenario_a),
                        ("B: max_serving_5x + macro tolerance removed", scenario_b)]:
        print("=" * 90)
        print(f"{label}  ({len(rows)} solved points)")
        print("=" * 90)
        servs = sorted((r["max_servings"] for r in rows), reverse=True)
        shares = sorted((r["max_servings_kcal_share_pct"] for r in rows), reverse=True)
        print(f"  max single-subrecipe servings:  avg={sum(servs)/len(servs):.1f}  "
              f"median={servs[len(servs)//2]:.1f}  p90={servs[int(len(servs)*0.1)]:.1f}  max={servs[0]:.1f}")
        print(f"  max single-subrecipe kcal share: avg={sum(shares)/len(shares):.1f}%  "
              f"median={shares[len(shares)//2]:.1f}%  p90={shares[int(len(shares)*0.1)]:.1f}%  max={shares[0]:.1f}%")

        worst = sorted(rows, key=lambda r: -r["max_servings"])[:5]
        print(f"\n  Top 5 worst-case plates (by max single-subrecipe servings):")
        for r in worst:
            print(f"\n  [{r['diet_label']} / {r['combo']}]  target kcal={r['target_kcal']:.0f} "
                  f"protein={r['target_protein_g']:.0f}g  ->  actual kcal={r['actual_kcal']:.0f} "
                  f"protein={r['actual_protein_g']:.0f}g")
            for item in r["plate"]:
                flag = "  <== " if item["name"] == r["max_servings_subrecipe"] and item["meal"] == r["max_servings_meal"] else "      "
                print(f"    {flag}{item['meal']:<10} {item['name']:<35} servings={item['servings']:<6} kcal={item['kcal']:.0f}")
        print()

    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "plate_shape_check.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"scenario_a": scenario_a, "scenario_b": scenario_b}, f, indent=2)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
