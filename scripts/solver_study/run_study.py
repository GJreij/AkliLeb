"""
run_study.py — the 528-run harness (88 diets x 6 meal combos) for one
tagged solver version. Reads the frozen population + recipe fixture (no
Supabase calls at run time), solves every point through solver_lab, and
writes a full results table.

For each point, captures every metric from the study's Word document,
Part B §3: solve mode, tolerance tier, culinary pass, serving step,
wall-clock time, LP attempts before success, per-macro deviation %,
root-cause category (only computed when the point misses the tight tier --
see diagnostics.py), and two guardrails (culinary-cap adherence at the
RELAXED band, and the max kcal share any single subrecipe holds).

Usage:
    venv/Scripts/python.exe scripts/solver_study/run_study.py [--version v0] [--limit N]

Writes:
    data/results_<version>.json  (full detail, one object per point)
    data/results_<version>.csv   (flat, one row per point -- feeds the workbook)
"""

import argparse
import csv
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
FIXTURE_WITH_MAINS_PATH = os.path.join(DATA_DIR, "recipe_fixture_with_mains.json")
FIXTURE_SHIPPED_PATH = os.path.join(DATA_DIR, "recipe_fixture_shipped.json")

GOOD_TOL = svc.KCAL_TOLERANCES[0]  # 0.08

# Maps a version id to the entry-point function in solver_lab.py. New
# candidates get a new function (per the study's convention) and a new
# entry here -- the rest of this harness is version-agnostic.
VERSION_ENTRYPOINT = {
    "v0": "optimize_subrecipes",
    "v1": "optimize_subrecipes_v1",
    "v2": "optimize_subrecipes_v2",
    "v3": "optimize_subrecipes_v3",
    "v4": "optimize_subrecipes_v4",  # v3 + proportional kcal tolerance; reuses _solve_lp_once_v3 directly
    "v5": "optimize_subrecipes_v5",  # v4 + protein hard-bound on single-meal days (multi-meal path unchanged)
}
# v2 needs the is_main-labeled fixture (simulated, not from the real DB --
# see apply_main_labels.py); v3/v4 need the REAL is_main + real max_serving
# resolution (recipe_fixture_shipped.json, pulled live by
# recipe_fixture_shipped.py) since they deliberately respect max_serving
# where it exists; every other version reads the plain fixture.
VERSION_FIXTURE = {"v2": FIXTURE_WITH_MAINS_PATH, "v3": FIXTURE_SHIPPED_PATH, "v4": FIXTURE_SHIPPED_PATH, "v5": FIXTURE_SHIPPED_PATH}
# diagnostics.py's root-cause isolation re-solves through svc._solve_lp_once
# (baseline's constraint set) -- meaningless for any version with a
# different constraint set, so it only runs for v0.
VERSIONS_WITH_DIAGNOSTICS = {"v0"}

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


# -----------------------------------------------------------------------
# Fixture wiring -- monkeypatch solver_lab's data-fetch functions so the
# whole harness runs with zero Supabase calls once the two fixtures exist.
# -----------------------------------------------------------------------
def wire_fixture(fixture):
    subs_by_recipe = {}
    rules_by_recipe = {}
    for meal_type, info in fixture["recipes_by_meal_type"].items():
        subs_by_recipe[info["recipe_id"]] = info["subrecipes"]
        rules_by_recipe[info["recipe_id"]] = info.get("rules", [])  # recipe_fixture_shipped.json has none -- production dropped rules entirely

    def _get_subs(recipe_id):
        return [dict(s) for s in subs_by_recipe.get(recipe_id, [])]

    def _get_rules(recipe_id):
        return list(rules_by_recipe.get(recipe_id, []))

    svc.get_recipe_subrecipes = _get_subs
    svc.get_recipe_rules = _get_rules
    diagnostics.svc.get_recipe_subrecipes = _get_subs
    diagnostics.svc.get_recipe_rules = _get_rules


def wrap_attempt_counter():
    """Wraps solver_lab._solve_lp_once, _solve_lp_once_v1, _solve_lp_once_v2,
    _solve_lp_once_v3, and _solve_lp_once_v4 to count calls -- whichever
    the active version's entry point calls through. Returns a zero-arg
    getter and a zero-arg resetter; all wrapped functions stay installed
    for the whole run (diagnostics.py's isolation solves also go through
    _solve_lp_once, but their counts are irrelevant since we only read the
    counter right after each real solve_fn() call)."""
    state = {"count": 0}
    original = svc._solve_lp_once
    original_v1 = svc._solve_lp_once_v1
    original_v2 = svc._solve_lp_once_v2
    original_v3 = svc._solve_lp_once_v3
    original_v4 = svc._solve_lp_once_v4
    original_v5 = svc._solve_lp_once_v5

    def counting(*args, **kwargs):
        state["count"] += 1
        return original(*args, **kwargs)

    def counting_v1(*args, **kwargs):
        state["count"] += 1
        return original_v1(*args, **kwargs)

    def counting_v2(*args, **kwargs):
        state["count"] += 1
        return original_v2(*args, **kwargs)

    def counting_v3(*args, **kwargs):
        state["count"] += 1
        return original_v3(*args, **kwargs)

    def counting_v4(*args, **kwargs):
        state["count"] += 1
        return original_v4(*args, **kwargs)

    def counting_v5(*args, **kwargs):
        state["count"] += 1
        return original_v5(*args, **kwargs)

    svc._solve_lp_once = counting
    svc._solve_lp_once_v1 = counting_v1
    svc._solve_lp_once_v3 = counting_v3
    svc._solve_lp_once_v2 = counting_v2
    svc._solve_lp_once_v4 = counting_v4
    svc._solve_lp_once_v5 = counting_v5

    def get():
        return state["count"]

    def reset():
        state["count"] = 0

    return get, reset


# -----------------------------------------------------------------------
# Per-point solve + metrics
# -----------------------------------------------------------------------
def classify_mode(day_totals):
    tol = day_totals.get("tolerance_used")
    culinary = day_totals.get("culinary_pass")
    macro_hard_bounds = day_totals.get("macro_hard_bounds")
    protein_hard_bound = day_totals.get("protein_hard_bound")

    if tol == "SAFE_FALLBACK":
        return "safe_fallback"
    if tol == "BEST_EFFORT_LP":
        return "best_effort_lp"
    # v1/v3 single-meal path: kcal-bounded, balance ratio/rules deliberately
    # off -- not a meaningful "strict culinary" solve even though
    # culinary_pass reads "strict". v5 added a protein-hard-bound sub-tier
    # tried FIRST, falling back to the fully-macro-free tier only when
    # that's infeasible -- split into two labels instead of one
    # "single_meal_kcal_only" bucket (found while fixing this classifier,
    # 2026-08-18) so it's visible whether v5's protein bound is actually
    # firing, not silently folded together with the fallback it exists to
    # avoid.
    if day_totals.get("skip_balance"):
        return "single_meal_kcal_protein" if protein_hard_bound else "single_meal_kcal_only"
    # Safety-net tier (multi-meal only): kcal still hard-bounded, but
    # protein/carbs/fat are all soft-only (macro_hard_bounds=False) --
    # structurally the same "macros unbounded" character as best_effort_lp,
    # just with kcal still walled. Confirmed bug (2026-08-18): this
    # classifier had no case for it at all, so it silently fell through to
    # strict_relaxed_tol/relaxed_culinary as if macros were still bounded --
    # made solve-mode distributions look far better than they actually are
    # (528-point rerun: 42% "relaxed_culinary" before this fix, almost all
    # of it actually safety-net).
    if macro_hard_bounds is False:
        return "safety_net"
    if culinary == "strict" and tol == GOOD_TOL:
        return "strict_tight"
    if culinary == "strict":
        return "strict_relaxed_tol"
    if culinary == "relaxed":
        return "relaxed_culinary"
    return "unknown"


def pct_dev(actual, target):
    if not target:
        return 0.0 if not actual else float("inf")
    return round((actual - target) / target * 100, 1)


def culinary_adherence(optimized_subs):
    """Checked against the RELAXED band -- the loosest band production
    itself ever allows -- so a violation here is a real guardrail breach,
    not just 'didn't hit the tighter strict-pass caps'."""
    by_type = {}
    for row in optimized_subs:
        mt = row.get("meal_type")
        if mt:
            by_type[mt] = by_type.get(mt, 0.0) + row["macros"]["kcal"]
    total = sum(by_type.values())
    if total <= 0:
        return True

    if "breakfast" in by_type and by_type["breakfast"] > svc.RELAXED_BREAKFAST_MAX_PCT * total:
        return False
    if "snack" in by_type and by_type["snack"] > svc.RELAXED_SNACK_MAX_PCT * total:
        return False
    if "lunch" in by_type and "dinner" in by_type:
        lunch, dinner = by_type["lunch"], by_type["dinner"]
        smaller = min(lunch, dinner)
        if smaller > 0 and abs(lunch - dinner) / smaller > svc.RELAXED_DINNER_LUNCH_DIFF_PCT:
            return False
    return True


def plate_shape_guardrails(optimized_subs):
    total_kcal = sum(row["macros"]["kcal"] for row in optimized_subs) or 1.0
    max_share = max((row["macros"]["kcal"] / total_kcal * 100 for row in optimized_subs), default=0.0)
    max_servings = max((row["servings"] for row in optimized_subs), default=0.0)
    return round(max_share, 1), max_servings


def solve_one(diet, combo_name, meal_types, fixed_recipes, get_attempts, reset_attempts,
              solve_fn, run_diagnostics):
    recipes_by_meal = {
        mt: {"recipe_id": fixed_recipes[mt], "meal_type": mt}
        for mt in meal_types
    }
    target = {
        "protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
        "fat_g": diet["fat_g"], "kcal": diet["kcal"],
    }

    reset_attempts()
    t0 = time.perf_counter()
    optimized_subs, _loss, day_totals = solve_fn(dict(recipes_by_meal), dict(target))
    wall_ms = round((time.perf_counter() - t0) * 1000, 2)
    attempts = get_attempts()

    mode = classify_mode(day_totals)
    adherent = culinary_adherence(optimized_subs)
    max_share, max_servings = plate_shape_guardrails(optimized_subs)

    root_cause = {"category": "", "detail": ""}
    if run_diagnostics and mode != "strict_tight":
        root_cause = diagnostics.diagnose(recipes_by_meal, target, day_totals)

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
        "root_cause_category": root_cause["category"],
        "root_cause_detail": root_cause["detail"],
        "culinary_cap_adherent": adherent,
        "max_subrecipe_kcal_share_pct": max_share,
        "max_subrecipe_servings": max_servings,
    }


def to_csv_row(version, r):
    return {
        "version": version,
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
    ap.add_argument("--version", default="v0")
    ap.add_argument("--limit", type=int, default=None, help="cap diets processed, for a quick smoke run")
    args = ap.parse_args()

    with open(POPULATION_PATH, encoding="utf-8") as f:
        population = json.load(f)["diets"]
    fixture_path = VERSION_FIXTURE.get(args.version, FIXTURE_PATH)
    with open(fixture_path, encoding="utf-8") as f:
        fixture = json.load(f)
    fixed_recipes = fixture["fixed_recipes"]
    meal_combos = fixture["meal_combos"]

    if args.limit:
        population = population[: args.limit]

    wire_fixture(fixture)
    get_attempts, reset_attempts = wrap_attempt_counter()

    if args.version not in VERSION_ENTRYPOINT:
        raise SystemExit(f"Unknown version {args.version!r} -- add it to VERSION_ENTRYPOINT in run_study.py")
    solve_fn = getattr(svc, VERSION_ENTRYPOINT[args.version])
    run_diagnostics = args.version in VERSIONS_WITH_DIAGNOSTICS

    total = len(population) * len(meal_combos)
    print(f"Running version={args.version} ({VERSION_ENTRYPOINT[args.version]}), "
          f"fixture={os.path.basename(fixture_path)}: "
          f"{len(population)} diets x {len(meal_combos)} combos = {total} points"
          f"{'' if run_diagnostics else '  [diagnostics skipped -- not v0]'}")

    results = []
    t_start = time.perf_counter()
    done = 0
    for diet in population:
        for combo_name, meal_types in meal_combos.items():
            r = solve_one(diet, combo_name, meal_types, fixed_recipes, get_attempts, reset_attempts,
                          solve_fn, run_diagnostics)
            results.append(r)
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  {done}/{total} ({time.perf_counter() - t_start:.1f}s elapsed)")

    elapsed = round(time.perf_counter() - t_start, 1)
    print(f"\nDone in {elapsed}s ({len(results)} points, {round(elapsed / max(len(results),1)*1000,1)}ms/point avg wall time overhead incl. diagnostics)")

    os.makedirs(DATA_DIR, exist_ok=True)
    json_path = os.path.join(DATA_DIR, f"results_{args.version}.json")
    csv_path = os.path.join(DATA_DIR, f"results_{args.version}.csv")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"version": args.version, "n_points": len(results), "elapsed_s": elapsed, "results": results}, f, indent=2)

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in results:
            writer.writerow(to_csv_row(args.version, r))

    print(f"Wrote {json_path}")
    print(f"Wrote {csv_path}")

    # Quick summary to stdout
    from collections import Counter
    modes = Counter(r["solve_mode"] for r in results)
    print("\nSolve-mode distribution:")
    for k, v in modes.most_common():
        print(f"  {k:<20} {v:>4}  ({v/len(results):.1%})")
    breaches = sum(1 for r in results if not r["culinary_cap_adherent"])
    print(f"\nCulinary-cap guardrail breaches: {breaches}/{len(results)}")


if __name__ == "__main__":
    main()
