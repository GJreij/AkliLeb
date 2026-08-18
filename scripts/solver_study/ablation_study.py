"""
ablation_study.py — global, one-at-a-time constraint ablation.

Different question than diagnostics.py's per-point root-cause isolation.
diagnostics.py stops at the FIRST thing that would unblock a given point,
in a fixed priority order — a point blocked by two independent constraints
only ever gets attributed to whichever is checked first, so it under-counts
anything checked later.

This script instead holds EVERY constraint at its strictest (Pass 1,
8% band) setting, removes exactly ONE constraint globally, and re-solves
all 528 points with a SINGLE LP attempt (not the full ladder). The delta
in solved count vs. the no-ablation baseline is that constraint's
marginal impact, independent of any other constraint or check order.

Baseline (no ablation) should reproduce v0's strict_tight count (106/528)
almost exactly — it's the same single attempt the real ladder tries first.

Usage:
    venv/Scripts/python.exe scripts/solver_study/ablation_study.py
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

GOOD_TOL = svc.KCAL_TOLERANCES[0]        # 0.08
BASE_MACRO = svc.BASE_MACRO_TOLERANCES[0]  # 0.15
HUGE = 1000.0
MAX_SERVING_MULTIPLIER = 5.0


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


def normal_macro_tols(P_t, C_t, F_t, kcal_t):
    return {
        "protein": svc.macro_tolerance("protein", P_t, kcal_t, BASE_MACRO),
        "carbs":   svc.macro_tolerance("carbs",   C_t, kcal_t, BASE_MACRO),
        "fat":     svc.macro_tolerance("fat",     F_t, kcal_t, BASE_MACRO),
    }


def solve(all_subs, rbm, P_t, C_t, F_t, kcal_t, rules, tol=GOOD_TOL, macro_tols=None,
          strict_culinary=True, steps=(1.0, 0.5)):
    """Tries serving_step=1.0 then 0.5, same as production's per-rung
    behavior — so a point that only solves at the fine step still counts
    as solved here, matching how v0's "strict_tight" tier was classified
    (culinary_pass=="strict" and tolerance_used==8%, regardless of which
    step it took). Ablation scenarios that change tol/macro_tols/culinary
    caps go through this unchanged; only ab_fine_step below deliberately
    overrides `steps` to isolate the step dimension itself."""
    mt = macro_tols if macro_tols is not None else normal_macro_tols(P_t, C_t, F_t, kcal_t)
    for step in steps:
        ok = svc._solve_lp_once(
            all_subs=all_subs, recipes_by_meal=rbm,
            P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
            serving_step=step, tol=tol, macro_tols=mt,
            allow_under_kcal=False, strict_culinary=strict_culinary,
            resolved_rules=rules, hard_bounds=True,
        ) is not None
        if ok:
            return True
    return False


# -----------------------------------------------------------------------
# Ablation scenarios — each removes exactly one constraint vs. the
# strict/8% baseline. Every other constraint stays at its strictest value.
# -----------------------------------------------------------------------
def ab_baseline(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules)


def ab_kcal_tolerance(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules, tol=HUGE)


def ab_macro_tolerance(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules,
                 macro_tols={"protein": HUGE, "carbs": HUGE, "fat": HUGE})


def ab_breakfast_cap(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    with diagnostics.patched(STRICT_BREAKFAST_MAX_PCT=1.0):
        return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules)


def ab_snack_cap(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    with diagnostics.patched(STRICT_SNACK_MAX_PCT=1.0):
        return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules)


def ab_dinner_lunch_cap(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    with diagnostics.patched(STRICT_DINNER_LUNCH_DIFF_PCT=HUGE):
        return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules)


def ab_all_culinary(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    with diagnostics.patched(STRICT_BREAKFAST_MAX_PCT=1.0, STRICT_SNACK_MAX_PCT=1.0,
                              STRICT_DINNER_LUNCH_DIFF_PCT=HUGE):
        return solve(subs, rbm, P_t, C_t, F_t, kcal_t, rules)


def ab_serving_balance(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    with diagnostics.patched(DEFAULT_SERVING_BALANCE_RATIO=HUGE):
        return solve(subs, rbm, P_t, C_t, F_t, kcal_t, [])  # rules=[] drops explicit rules too


def ab_max_serving(subs, rbm, P_t, C_t, F_t, kcal_t, rules):
    scaled = [dict(s, max_serving=s["max_serving"] * MAX_SERVING_MULTIPLIER) for s in subs]
    return solve(scaled, rbm, P_t, C_t, F_t, kcal_t, rules)


# No "force fine step" scenario: solve() already tries step=0.5 as a
# fallback at every rung (matching production), and the 0.5 lattice is a
# strict superset of the 1.0 lattice (any whole-serving solution is also a
# valid half-serving solution) — so "only ever try 0.5" can never solve
# anything the baseline doesn't already credit. There's no meaningful
# ablation on serving-step granularity in this single-rung framing.

SCENARIOS = [
    ("baseline_no_ablation",      "(none — sanity check, should match v0's strict_tight count)", ab_baseline),
    ("kcal_tolerance",            "Kcal tolerance band removed", ab_kcal_tolerance),
    ("macro_tolerance",           "Protein/carbs/fat tolerance bands removed", ab_macro_tolerance),
    ("breakfast_cap",             "Breakfast kcal-share cap removed", ab_breakfast_cap),
    ("snack_cap",                 "Snack kcal-share cap removed", ab_snack_cap),
    ("dinner_lunch_cap",          "Lunch/dinner balance cap removed", ab_dinner_lunch_cap),
    ("all_culinary_caps",         "All culinary caps removed together", ab_all_culinary),
    ("serving_balance",           "Serving-balance ratio (flat 2.5x + recipe rules) removed", ab_serving_balance),
    ("max_serving_5x",            f"Max-serving ceiling raised {MAX_SERVING_MULTIPLIER}x", ab_max_serving),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="cap diets processed, for a quick smoke run")
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
                "combo": combo_name, "rbm": rbm, "all_subs": all_subs, "rules": rules,
                "target": target,
            })

    print(f"{len(points)} points x {len(SCENARIOS)} scenarios = {len(points) * len(SCENARIOS)} single-attempt solves\n")

    detail_rows = []
    summary = []
    t0 = time.perf_counter()

    for key, label, fn in SCENARIOS:
        solved = 0
        by_group = defaultdict(lambda: [0, 0])   # group -> [solved, total]
        by_combo = defaultdict(lambda: [0, 0])
        for pt in points:
            t = pt["target"]
            ok = fn(pt["all_subs"], pt["rbm"], t["protein_g"], t["carbs_g"], t["fat_g"], t["kcal"], pt["rules"])
            solved += int(ok)
            by_group[pt["diet_group"]][0] += int(ok); by_group[pt["diet_group"]][1] += 1
            by_combo[pt["combo"]][0] += int(ok); by_combo[pt["combo"]][1] += 1
            detail_rows.append({
                "scenario": key, "diet_id": pt["diet_id"], "diet_group": pt["diet_group"],
                "combo": pt["combo"], "solved": ok,
            })
        summary.append({
            "scenario": key, "label": label, "solved": solved, "total": len(points),
            "pct": round(solved / len(points) * 100, 1),
            "by_group": {g: {"solved": v[0], "total": v[1]} for g, v in by_group.items()},
            "by_combo": {c: {"solved": v[0], "total": v[1]} for c, v in by_combo.items()},
        })
        print(f"  {key:<22} {solved:>4}/{len(points)}  ({solved/len(points):.1%})   {label}")

    elapsed = round(time.perf_counter() - t0, 1)
    print(f"\nDone in {elapsed}s")

    baseline_solved = summary[0]["solved"]
    print(f"\n=== Ranked by points UNLOCKED vs. no-ablation baseline ({baseline_solved}/{len(points)}) ===")
    ranked = sorted(summary[1:], key=lambda s: -(s["solved"] - baseline_solved))
    for s in ranked:
        delta = s["solved"] - baseline_solved
        print(f"  {s['scenario']:<22} +{delta:>4} points unlocked ({delta/len(points):.1%} of population)   [{s['label']}]")

    os.makedirs(DATA_DIR, exist_ok=True)
    json_path = os.path.join(DATA_DIR, "ablation_results.json")
    csv_path = os.path.join(DATA_DIR, "ablation_results.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"n_points": len(points), "elapsed_s": elapsed, "summary": summary}, f, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["scenario", "diet_id", "diet_group", "combo", "solved"])
        writer.writeheader()
        writer.writerows(detail_rows)

    print(f"\nWrote {json_path}")
    print(f"Wrote {csv_path}")


if __name__ == "__main__":
    main()
