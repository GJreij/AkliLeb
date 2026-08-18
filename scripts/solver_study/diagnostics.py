"""
diagnostics.py — root-cause isolation for a (diet, recipe-combo) point that
did not solve in the tightest tier (strict culinary, 8% band).

Re-solves the SAME point with ONE thing changed at a time (via temporary
monkeypatches of module-level constants on solver_lab, always restored
immediately after) to see what specifically would flip it to feasible:

  STRICT_CULINARY_BLOCKED   Loosening only the named STRICT-pass culinary
                             cap(s) fixes it at the SAME 8% tolerance.
  MACRO_TOLERANCE_TOO_TIGHT Culinary constraints are not the problem --
                             even under STRICT culinary, the 8% band itself
                             is too tight; reports the tolerance tier that
                             first becomes feasible.
  CULINARY_AND_TOLERANCE    Neither alone fixes it at 8%; needs both a
                             wider band AND relaxed culinary.
  SERVING_BALANCE_RATIO_BLOCKED  The flat 2.5x intra-meal ratio (or an
                             explicit recipe_subrecipe_rule) is what blocks
                             every tolerance tier in both culinary passes.
  RECIPE_POOL_MISMATCH      Even with every soft constraint removed, this
                             combo structurally cannot reach the target --
                             a recipe-pool problem, not a rule/tolerance one.

This is this study's own copy of the isolation method used by the (now
deleted) diet_tolerance_diagnostic.py -- rewritten fresh against
scripts/solver_study/solver_lab.py rather than services/mealplan_service.py,
with no dependency on that script's data-fetching or day-building code.
"""

import math
from contextlib import contextmanager

import solver_lab as svc

GOOD_TOL = svc.KCAL_TOLERANCES[0]  # 0.08


@contextmanager
def patched(**kwargs):
    saved = {k: getattr(svc, k) for k in kwargs}
    try:
        for k, v in kwargs.items():
            setattr(svc, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(svc, k, v)


def build_all_subs(recipes_by_meal):
    all_subs = []
    for meal_key, info in recipes_by_meal.items():
        for s in svc.get_recipe_subrecipes(info["recipe_id"]):
            all_subs.append({
                "meal": meal_key, "subrecipe_id": s["id"], "name": s["name"],
                "macros": s["macros"],
                "max_serving": float(int(s.get("max_serving") or svc.DEFAULT_MAX_SERVING)),
            })
    return all_subs


def apply_scale_guard(all_subs, kcal_t):
    if kcal_t <= 0:
        return
    max_achievable = sum(s["max_serving"] * s["macros"]["kcal"] for s in all_subs)
    min_needed = (1.0 - svc.KCAL_TOLERANCES[-1]) * kcal_t
    if max_achievable < min_needed:
        scale = min((kcal_t / max(max_achievable, 1.0)) * 1.05, svc.MAX_SERVING_SCALE_FACTOR)
        for s in all_subs:
            s["max_serving"] = math.ceil(s["max_serving"] * scale)


def try_solve(all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, resolved_rules,
              strict_culinary, tol, base_macro_tol, step=1.0, hard_bounds=True):
    macro_tols = {
        "protein": svc.macro_tolerance("protein", P_t, kcal_t, base_macro_tol),
        "carbs":   svc.macro_tolerance("carbs",   C_t, kcal_t, base_macro_tol),
        "fat":     svc.macro_tolerance("fat",     F_t, kcal_t, base_macro_tol),
    }
    return svc._solve_lp_once(
        all_subs=all_subs, recipes_by_meal=recipes_by_meal,
        P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
        serving_step=step, tol=tol, macro_tols=macro_tols,
        allow_under_kcal=False, strict_culinary=strict_culinary,
        resolved_rules=resolved_rules, hard_bounds=hard_bounds,
    ) is not None


def isolate_culinary_caps(all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, resolved_rules, tol, base_macro_tol):
    hits = []
    trials = [
        ("snack_max_pct",         {"STRICT_SNACK_MAX_PCT": svc.RELAXED_SNACK_MAX_PCT}),
        ("dinner_lunch_diff_pct", {"STRICT_DINNER_LUNCH_DIFF_PCT": svc.RELAXED_DINNER_LUNCH_DIFF_PCT}),
        ("solo_meal_cap",         {"STRICT_NO_DINNER_YES_LUNCH_PCT": 1.0, "STRICT_NO_LUNCH_YES_DINNER_PCT": 1.0}),
    ]
    for name, patch in trials:
        with patched(**patch):
            if try_solve(all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, resolved_rules, True, tol, base_macro_tol, 1.0):
                hits.append(name)
    return hits


def diagnose(recipes_by_meal, target, real_totals):
    """real_totals is the day_totals dict already returned by a production
    optimize_subrecipes() call for this exact point -- diagnose() only runs
    the extra isolation solves if that call did NOT land in the good tier."""
    P_t, C_t, F_t, kcal_t = target["protein_g"], target["carbs_g"], target["fat_g"], target["kcal"]

    tol_used  = real_totals.get("tolerance_used")
    pass_used = real_totals.get("culinary_pass")
    if tol_used == GOOD_TOL and pass_used == "strict":
        return {"category": "GOOD", "detail": ""}

    all_subs = build_all_subs(recipes_by_meal)
    apply_scale_guard(all_subs, kcal_t)
    resolved_rules = svc._resolve_rules_for_day(all_subs, recipes_by_meal)

    base0 = svc.BASE_MACRO_TOLERANCES[0]
    args = (all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, resolved_rules)

    if try_solve(*args, False, GOOD_TOL, base0, 1.0):
        caps = isolate_culinary_caps(*args, GOOD_TOL, base0)
        return {"category": "STRICT_CULINARY_BLOCKED", "detail": ",".join(caps) or "combination_of_caps"}

    for tol, bmt in zip(svc.KCAL_TOLERANCES[1:], svc.BASE_MACRO_TOLERANCES[1:]):
        if try_solve(*args, True, tol, bmt, 1.0):
            return {"category": "MACRO_TOLERANCE_TOO_TIGHT", "detail": f"needs >= {int(tol*100)}% band"}

    for tol, bmt in zip(svc.KCAL_TOLERANCES[1:], svc.BASE_MACRO_TOLERANCES[1:]):
        if try_solve(*args, False, tol, bmt, 1.0):
            caps = isolate_culinary_caps(*args, tol, bmt)
            cap_str = ",".join(caps) or "combination_of_caps"
            return {"category": "CULINARY_AND_TOLERANCE", "detail": f">= {int(tol*100)}% band AND {cap_str}"}

    widest_tol, widest_bmt = svc.KCAL_TOLERANCES[-1], svc.BASE_MACRO_TOLERANCES[-1]
    with patched(DEFAULT_SERVING_BALANCE_RATIO=1000.0):
        ratio_free = try_solve(*args, False, widest_tol, widest_bmt, 1.0)
    rules_free_args = (all_subs, recipes_by_meal, P_t, C_t, F_t, kcal_t, [])
    rules_free = try_solve(*rules_free_args, False, widest_tol, widest_bmt, 1.0)

    if ratio_free or rules_free:
        parts = []
        if ratio_free:
            parts.append("flat_2.5x_default_ratio")
        if rules_free:
            parts.append("recipe_subrecipe_rule")
        return {"category": "SERVING_BALANCE_RATIO_BLOCKED", "detail": " / ".join(parts)}

    min_k = sum(s["macros"]["kcal"] for s in all_subs)
    max_k = sum(s["max_serving"] * s["macros"]["kcal"] for s in all_subs)
    return {
        "category": "RECIPE_POOL_MISMATCH",
        "detail": f"achievable kcal [{min_k:.0f},{max_k:.0f}] vs target {kcal_t:.0f}",
    }
