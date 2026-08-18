"""
kitchen_batching_study.py — validates the point-1/3/4 redesign (see
project plan) against real pulled data before any production port:

  Point 1 - kitchen batching: menu_lab.apply_kitchen_batching layered on
            run_holistic_week. Metric: distinct recipes cooked per date,
            before vs after.
  Point 3 - hard macro-fit bar + bounded repair: for each client/day, run
            the REAL production LP solver (services.mealplan_service.
            optimize_subrecipes, monkeypatched to read subrecipe/macro
            data from the local fixture instead of Supabase - same
            tolerance ladder, same culinary constraints, no DB
            dependency). If a day lands in BEST_EFFORT_LP, run the single
            bounded repair step (worst-slot swap + one re-solve) and
            confirm the "at most 2 solves per day" bound holds.
  Point 4 - substitution advisory: for any day a repair couldn't fully
            resolve, search the client's other requested dates for a real
            slot where the original recipe would clear the macro-compat
            hard filter against this client's own target.

Run with:
    venv/Scripts/python.exe scripts/solver_study/kitchen_batching_study.py [--diets N] [--range-days N]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import menu_lab as ml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import services.mealplan_service as mps
from services.daily_menu_service import needs_repair  # real production trigger, not reimplemented here

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POOL_PATH = os.path.join(DATA_DIR, "menu_pool_fixture.json")
POPULATION_PATH = os.path.join(DATA_DIR, "population.json")

MEALS_MAP = {"breakfast": "breakfast", "lunch": "lunch", "snack": "snack", "dinner": "dinner"}


# =============================================================================
# Monkeypatch the real solver's DB-backed subrecipe fetch with the fixture's
# already-embedded subrecipe data, so optimize_subrecipes() runs its real,
# unmodified tolerance-ladder logic with zero Supabase dependency.
# =============================================================================

def install_fixture_subrecipe_source(fixture_recipes: dict):
    def fixture_get_recipe_subrecipes(recipe_id):
        r = fixture_recipes.get(recipe_id) or fixture_recipes.get(str(recipe_id))
        if not r:
            return []
        out = []
        for s in r.get("subrecipes", []):
            m = s.get("macros", {})
            out.append({
                "id": s["subrecipe_id"],
                "name": s.get("name"),
                "max_serving": s.get("max_serving"),
                "is_main": bool(s.get("is_main")),
                "macros": {
                    "kcal":    float(m.get("kcal")    or 0.0),
                    "protein": float(m.get("protein") or 0.0),
                    "carbs":   float(m.get("carbs")   or 0.0),
                    "fat":     float(m.get("fat")     or 0.0),
                },
            })
        return out

    mps.get_recipe_subrecipes = fixture_get_recipe_subrecipes


# =============================================================================
# NEW: flexibility classification + fixed dinner-then-lunch repair
# (prototyping the 2026-08-17 redesign before touching production)
# =============================================================================

RIGID_SUB_COUNT_THRESHOLD = 1  # sub_count <= this -> rigid (can't rebalance across items)
RIGID_SUM_MAX_THRESHOLD   = 4  # sum_max <= this -> rigid (little serving headroom either way)


def is_rigid(rid: int, flex_stats: dict) -> bool:
    f = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    return f["sub_count"] <= RIGID_SUB_COUNT_THRESHOLD or f["sum_max"] <= RIGID_SUM_MAX_THRESHOLD


REPAIR_FLEX_SUB_COUNT_WEIGHT = 1.0
REPAIR_FLEX_SUM_MAX_WEIGHT   = 0.15


def _flexibility_score(rid: int, flex_stats: dict) -> float:
    f = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    return REPAIR_FLEX_SUB_COUNT_WEIGHT * f["sub_count"] + REPAIR_FLEX_SUM_MAX_WEIGHT * f["sum_max"]


def best_alternative_flexible(
    meal_type: str, exclude: set, eligible_by_meal_type: dict,
    recipe_macros: dict, macro_target: dict, flex_stats: dict,
) -> int | None:
    """Mirrors production's _best_alternative tier-1 ranking (services/
    daily_menu_service.py): among non-excluded candidates, most flexible
    (most subrecipes/serving headroom) wins, macro fit as tiebreak — not
    macro fit alone. A repair swap's job is giving the LP room to
    rebalance, not just landing a macro-plausible recipe on paper."""
    pool = [rid for rid in eligible_by_meal_type.get(meal_type, []) if rid not in exclude]
    if not pool:
        return None
    return max(
        pool,
        key=lambda rid: (
            _flexibility_score(rid, flex_stats),
            ml.macro_compat_score(rid, recipe_macros, macro_target),
        ),
    )


def repair_day_fixed_sequence(
    chosen: dict, meals_map: dict, eligible_by_meal_type: dict,
    recipe_macros: dict, macro_target: dict, week_used_by_type: dict,
    flex_stats: dict, trial_meal_types=("dinner", "lunch"),
) -> dict:
    """Fixed dinner-then-lunch repair: at most len(trial_meal_types) extra
    solves total (one per trial), each trial working from the ORIGINAL
    day (not stacked on the previous trial), stopping regardless of
    outcome after the last trial. Never targets breakfast/snack.
    `trial_meal_types` lets the caller reproduce production's >7-day cap
    (dinner-only) for timing comparisons."""
    meal_key_by_type = {mt: mk for mk, mt in meals_map.items()}

    def build_recipes_by_meal(c):
        return {mk: {"recipe_id": rid, "meal_type": meals_map[mk]} for mk, rid in c.items()}

    _, _, day_totals = mps.optimize_subrecipes(build_recipes_by_meal(chosen), macro_target)
    solves = 1
    if not needs_repair(day_totals, macro_target):
        return {"chosen": chosen, "day_totals": day_totals, "solves": solves, "trial_used": None}

    for target_type in trial_meal_types:
        mk = meal_key_by_type.get(target_type)
        if mk is None or mk not in chosen:
            continue  # this meal type wasn't requested today
        exclude = set(chosen.values()) | week_used_by_type.get(target_type, set())
        alt = best_alternative_flexible(
            target_type, exclude, eligible_by_meal_type, recipe_macros, macro_target, flex_stats,
        )
        if alt is None:
            continue
        candidate = dict(chosen)
        candidate[mk] = alt
        _, _, candidate_totals = mps.optimize_subrecipes(build_recipes_by_meal(candidate), macro_target)
        solves += 1
        if not needs_repair(candidate_totals, macro_target):
            return {"chosen": candidate, "day_totals": candidate_totals, "solves": solves, "trial_used": target_type}

    return {"chosen": chosen, "day_totals": day_totals, "solves": solves, "trial_used": "both_failed"}


# =============================================================================
# Point-3: single bounded repair step
# =============================================================================

def worst_macro_fit_slot(chosen: dict, recipe_macros: dict, macro_target: dict) -> str:
    """Returns the meal_key whose recipe has the lowest macro-compat score
    against this client's real target - the slot "most responsible" for a
    day landing in BEST_EFFORT_LP."""
    return min(
        chosen.keys(),
        key=lambda mk: ml.macro_compat_score(chosen[mk], recipe_macros, macro_target),
    )


def best_alternative(
    meal_type: str, exclude: set, eligible_by_meal_type: dict,
    recipe_macros: dict, macro_target: dict,
) -> int | None:
    pool = [rid for rid in eligible_by_meal_type.get(meal_type, []) if rid not in exclude]
    if not pool:
        return None
    return max(pool, key=lambda rid: ml.macro_compat_score(rid, recipe_macros, macro_target))


def repair_day_if_needed(
    chosen: dict, meals_map: dict, eligible_by_meal_type: dict,
    recipe_macros: dict, macro_target: dict, week_used_by_type: dict,
) -> dict:
    """Single bounded repair step (point 3). Calls optimize_subrecipes at
    most twice total: once to check, once more only if repair is
    attempted. Returns a dict with the final chosen/day_totals/solve_count
    and whether repair was attempted/succeeded, for measurement."""
    meal_key_by_type = {mt: mk for mk, mt in meals_map.items()}

    def build_recipes_by_meal(c):
        return {mk: {"recipe_id": rid, "meal_type": meals_map[mk]} for mk, rid in c.items()}

    _, _, day_totals = mps.optimize_subrecipes(build_recipes_by_meal(chosen), macro_target)
    solves = 1
    if not needs_repair(day_totals, macro_target):
        return {"chosen": chosen, "day_totals": day_totals, "solves": solves,
                "repair_attempted": False, "repair_succeeded": None, "swapped_meal_key": None}

    worst_mk = worst_macro_fit_slot(chosen, recipe_macros, macro_target)
    worst_mt = meals_map[worst_mk]
    exclude = set(chosen.values()) | week_used_by_type.get(worst_mt, set())
    alt = best_alternative(worst_mt, exclude, eligible_by_meal_type, recipe_macros, macro_target)

    if alt is None:
        return {"chosen": chosen, "day_totals": day_totals, "solves": solves,
                "repair_attempted": True, "repair_succeeded": False, "swapped_meal_key": None}

    candidate_chosen = dict(chosen)
    candidate_chosen[worst_mk] = alt
    _, _, candidate_totals = mps.optimize_subrecipes(build_recipes_by_meal(candidate_chosen), macro_target)
    solves += 1

    if not needs_repair(candidate_totals, macro_target):
        return {"chosen": candidate_chosen, "day_totals": candidate_totals, "solves": solves,
                "repair_attempted": True, "repair_succeeded": True, "swapped_meal_key": worst_mk,
                "original_recipe_id": chosen[worst_mk]}

    return {"chosen": chosen, "day_totals": day_totals, "solves": solves,
            "repair_attempted": True, "repair_succeeded": False, "swapped_meal_key": None}


# =============================================================================
# Point-4: substitution advisory search
# =============================================================================

def find_advisory_slot(
    original_recipe_id: int, exclude_date_index: int, week_days: list,
    meals_map: dict, eligible_by_meal_type: dict, recipe_macros: dict, macro_target: dict,
) -> dict | None:
    """Searches this client's OTHER requested dates for a real slot where
    original_recipe_id would clear the macro-compat hard filter against
    their own target. Cheap - reuses the already-computed scored pool, no
    extra LP solve."""
    if ml.macro_compat_score(original_recipe_id, recipe_macros, macro_target) < ml.MACRO_COMPAT_HARD_FILTER:
        return None  # not a fit anywhere for this client, no point recommending it
    for day in week_days:
        if day["day_index"] == exclude_date_index:
            continue
        for mk, mt in meals_map.items():
            if original_recipe_id in eligible_by_meal_type.get(mt, []) and day["chosen"].get(mk) != original_recipe_id:
                return {"suggested_date_index": day["day_index"], "suggested_meal_type": mt}
    return None


# =============================================================================
# Full pipeline for one client
# =============================================================================

def run_client_week(
    diet: dict, weekdays: list, eligible_by_meal_type: dict, flex_stats: dict,
    popularity: dict, recipe_macros: dict, reference_target: dict,
) -> dict:
    macro_target = {
        "protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
        "fat_g": diet["fat_g"], "kcal": diet["kcal"],
    }
    days = ml.run_holistic_week(
        MEALS_MAP, eligible_by_meal_type, flex_stats, popularity, recipe_macros,
        macro_target, weekdays=weekdays,
    )
    batched_days = ml.apply_kitchen_batching(
        days, MEALS_MAP, eligible_by_meal_type, recipe_macros, reference_target,
    )

    meal_key_by_type = {mt: mk for mk, mt in MEALS_MAP.items()}
    week_used_by_type: Dict[str, set] = defaultdict(set)
    for day in batched_days:
        for mk, rid in day["chosen"].items():
            week_used_by_type[MEALS_MAP[mk]].add(rid)

    results = []
    max_solves = 0
    for day in batched_days:
        outcome = repair_day_if_needed(
            day["chosen"], MEALS_MAP, eligible_by_meal_type, recipe_macros,
            macro_target, week_used_by_type,
        )
        max_solves = max(max_solves, outcome["solves"])

        advisory = None
        if outcome["repair_attempted"] and not outcome["repair_succeeded"] and outcome.get("swapped_meal_key") is None:
            pass  # no alternative existed at all - nothing to recommend
        if outcome["repair_attempted"] and not outcome["repair_succeeded"]:
            # real infeasibility for this day/slot combination - honest advisory
            worst_mk = worst_macro_fit_slot(day["chosen"], recipe_macros, macro_target)
            advisory = find_advisory_slot(
                day["chosen"][worst_mk], day["day_index"], batched_days,
                MEALS_MAP, eligible_by_meal_type, recipe_macros, macro_target,
            )

        results.append({
            "day_index": day["day_index"],
            "distinct_recipe_count_before_repair": day["distinct_recipe_count"],
            "batched_pairs": day["batched_pairs"],
            "day_totals": outcome["day_totals"],
            "tolerance_used": outcome["day_totals"].get("tolerance_used"),
            "solves": outcome["solves"],
            "repair_attempted": outcome["repair_attempted"],
            "repair_succeeded": outcome["repair_succeeded"],
            "advisory": advisory,
        })

    return {"diet_id": diet["diet_id"], "days": results, "max_solves_per_day": max_solves}


def run_client_week_new_repair(
    diet: dict, weekdays: list, eligible_by_meal_type: dict, flex_stats: dict,
    popularity: dict, recipe_macros: dict, reference_target: dict,
) -> dict:
    """Same day-building as run_client_week (identical selection, so this
    isolates the effect of JUST the repair-mechanism change) but repairs
    via repair_day_fixed_sequence (dinner-then-lunch, at most 2 extra
    solves) instead of the old worst-slot single swap."""
    macro_target = {
        "protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"],
        "fat_g": diet["fat_g"], "kcal": diet["kcal"],
    }
    days = ml.run_holistic_week(
        MEALS_MAP, eligible_by_meal_type, flex_stats, popularity, recipe_macros,
        macro_target, weekdays=weekdays,
    )
    batched_days = ml.apply_kitchen_batching(
        days, MEALS_MAP, eligible_by_meal_type, recipe_macros, reference_target,
    )

    week_used_by_type: Dict[str, set] = defaultdict(set)
    for day in batched_days:
        for mk, rid in day["chosen"].items():
            week_used_by_type[MEALS_MAP[mk]].add(rid)

    results = []
    max_solves = 0
    for day in batched_days:
        outcome = repair_day_fixed_sequence(
            day["chosen"], MEALS_MAP, eligible_by_meal_type, recipe_macros,
            macro_target, week_used_by_type, flex_stats,
        )
        max_solves = max(max_solves, outcome["solves"])
        results.append({
            "day_index": day["day_index"],
            "day_totals": outcome["day_totals"],
            "tolerance_used": outcome["day_totals"].get("tolerance_used"),
            "solves": outcome["solves"],
            "trial_used": outcome["trial_used"],
        })

    return {"diet_id": diet["diet_id"], "days": results, "max_solves_per_day": max_solves}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diets", type=int, default=20)
    parser.add_argument("--range-days", type=int, default=5, help="weekday-range length; >5 exercises the multi-week generalization")
    args = parser.parse_args()

    with open(POOL_PATH) as f:
        fixture = json.load(f)
    with open(POPULATION_PATH) as f:
        population = json.load(f)

    recipes, flex_stats, recipe_macros, subrecipe_sets, popularity = ml.build_pool(fixture)
    eligible = ml.eligible_by_meal_type_from_pool(recipes)
    reference_target = ml.population_reference_target(population["diets"])
    install_fixture_subrecipe_source(fixture["recipes"])

    weekdays = [d % 5 for d in range(args.range_days)]
    diets = population["diets"][: args.diets]

    print(f"Pool: {len(recipes)} recipes, eligible={ {k: len(v) for k, v in eligible.items()} }")
    print(f"Population reference target: {reference_target}")
    print(f"Range: {args.range_days} weekdays, {len(diets)} clients\n")

    all_results = [
        run_client_week(diet, weekdays, eligible, flex_stats, popularity, recipe_macros, reference_target)
        for diet in diets
    ]

    # ---- Point-1 metric: distinct recipes cooked per date, aggregated ----
    total_slots = sum(len(r["days"]) * len(MEALS_MAP) for r in all_results)
    total_distinct_before = sum(
        sum(d["distinct_recipe_count_before_repair"] for d in r["days"]) for r in all_results
    )
    total_batched_pairs = sum(
        sum(len(d["batched_pairs"]) for d in r["days"]) for r in all_results
    )
    print("=== Point 1: kitchen batching ===")
    print(f"Total meal-slots: {total_slots}")
    print(f"Total distinct-recipe-instances after batching: {total_distinct_before} "
          f"({total_batched_pairs} slots consolidated onto an already-cooked recipe)")
    print(f"Batching rate: {100.0 * total_batched_pairs / total_slots:.1f}% of slots reused a same-date recipe\n")

    # ---- Point-3 metric: tolerance tier distribution + solve bound ----
    tier_counts = defaultdict(int)
    repair_attempted = repair_succeeded = 0
    max_solves_seen = 0
    for r in all_results:
        max_solves_seen = max(max_solves_seen, r["max_solves_per_day"])
        for d in r["days"]:
            tier_counts[d["tolerance_used"]] += 1
            if d["repair_attempted"]:
                repair_attempted += 1
                if d["repair_succeeded"]:
                    repair_succeeded += 1

    total_days = sum(len(r["days"]) for r in all_results)
    print("=== Point 3: macro-fit bar + bounded repair ===")
    print(f"Total client-days solved: {total_days}")
    print(f"Final tolerance tier distribution: {dict(tier_counts)}")
    print(f"Repair attempted on {repair_attempted} days, succeeded on {repair_succeeded} "
          f"({100.0 * repair_succeeded / max(repair_attempted, 1):.1f}%)")
    print(f"Max solves observed for any single day: {max_solves_seen} (bound is 2)\n")

    # ---- Point-4 metric: advisories ----
    advisories = [d["advisory"] for r in all_results for d in r["days"] if d.get("advisory")]
    unresolved_no_advisory = [
        d for r in all_results for d in r["days"]
        if d["repair_attempted"] and not d["repair_succeeded"] and not d.get("advisory")
    ]
    print("=== Point 4: substitution advisory ===")
    print(f"Advisories generated: {len(advisories)}")
    print(f"Unresolved days with no advisory possible (recipe doesn't fit anywhere for this client): {len(unresolved_no_advisory)}")

    # ---- NEW: fixed dinner-then-lunch repair, same days, compare vs above ----
    new_results = [
        run_client_week_new_repair(diet, weekdays, eligible, flex_stats, popularity, recipe_macros, reference_target)
        for diet in diets
    ]
    new_tier_counts = defaultdict(int)
    new_max_solves = 0
    trial_used_counts = defaultdict(int)
    for r in new_results:
        new_max_solves = max(new_max_solves, r["max_solves_per_day"])
        for d in r["days"]:
            new_tier_counts[d["tolerance_used"]] += 1
            if d["trial_used"] is not None:
                trial_used_counts[d["trial_used"]] += 1

    def _diet_target(diet):
        return {"protein_g": diet["protein_g"], "carbs_g": diet["carbs_g"], "fat_g": diet["fat_g"], "kcal": diet["kcal"]}

    def _count_still_bad(results, diets_list):
        # Re-checks needs_repair per day using the REAL delivered day_totals
        # against the correct per-client target — tier label alone isn't
        # enough post-safety-net-tier fix (see needs_repair's docstring).
        total = 0
        for diet, r in zip(diets_list, results):
            target = _diet_target(diet)
            for d in r["days"]:
                if needs_repair(d["day_totals"], target):
                    total += 1
        return total

    old_still_bad = _count_still_bad(all_results, diets)
    new_still_bad = _count_still_bad(new_results, diets)

    print("\n=== NEW: fixed dinner-then-lunch repair (same days as above) ===")
    print(f"Final tolerance tier distribution: {dict(new_tier_counts)}")
    print(f"Trials used (dinner/lunch/both_failed): {dict(trial_used_counts)}")
    print(f"Max solves observed for any single day: {new_max_solves} (bound is 3: 1 original + 2 trials)")
    print(f"Still genuinely needs repair after: OLD={old_still_bad} vs NEW={new_still_bad} "
          f"(out of {total_days} client-days)")


if __name__ == "__main__":
    main()
