"""
services/daily_menu_service.py — shared daily_menu template generation +
live convergence toward what clients actually order.

Replaces the old greedy day-by-day get_or_create_daily_template that used
to live in routes/mealplan_routes.py. Validated in scripts/solver_study/
(menu_lab.py + weekly_collapse_study.py + vote_ledger_sim.py) before
shipping - see that study for the "why": the old greedy walk collapsed
candidate pools by the end of the week (recency deques scoped to a
single /generate_meal_plan call, no visibility past "yesterday"), and
two of its five scoring signals were silently dead in production -
recipe_category doesn't exist as a table at all, and the old weekday-
popularity query selected columns (meal_plan_day.recipe_id/.weekday)
that don't exist on that table, silently excepted, and fell back to a
flat 0.5 for every recipe.

Two-layer design:

  1. INITIAL TEMPLATE (bootstrap, before any real order exists for a
     given date): a whole-range holistic allocator ranks recipes by a
     day-invariant quality score and schedules them across the request's
     date range with explicit spacing, instead of a random greedy walk
     that only remembers its own immediate past. Persisted to daily_menu
     exactly as before (same table, same upsert shape).

  2. LIVE CONVERGENCE (once real orders exist): nothing new gets written
     anywhere for this. /update_meal_plan never persists to the DB (it's
     a pre-checkout preview only - confirmed by reading
     mealplan_update_dynamic_service.py and the frontend's OrderFlow.tsx
     call order: generate -> update (in-memory) -> confirm_order is the
     ONE point a client's final recipe choice gets written to
     meal_plan_day_recipe). So "the current leader" for an already-
     templated slot is simply whichever recipe has the most CONFIRMED
     orders for that date+meal_type - a live COUNT query against data
     that already exists, not a separate vote table or counter that
     could drift out of sync.
"""

import heapq
import math
import random
import threading
from collections import defaultdict
from datetime import date as date_cls, datetime
from typing import Dict, List, Optional

from utils.supabase_client import supabase

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

# Shared template is regenerated if it is older than this many days.
TEMPLATE_TTL_DAYS = 7

# Users whose daily kcal target exceeds this bypass the shared daily_menu
# template entirely and always receive a fresh personalised day.
HIGH_KCAL_THRESHOLD = 2800

WEEKDAY_POPULARITY_WEIGHT     = 1.5
FLEX_SUB_COUNT_WEIGHT         = 0.5
FLEX_SUM_MAX_WEIGHT           = 0.1
ASSUMED_MAIN_HEADROOM         = 6
ASSUMED_NON_MAIN_HEADROOM     = 3
POPULARITY_CAP                = 50
MACRO_COMPAT_WEIGHT           = 3.0
MACRO_COMPAT_HARD_FILTER      = -2.5
SAME_RECIPE_YESTERDAY_PENALTY = 10.0

# Replaces CATEGORY_OVERLAP_PENALTY (recipe_category table doesn't exist -
# confirmed against the live schema). Jaccard similarity of subrecipe
# sets (0-1) * this weight; comparable magnitude to what the old
# category penalty occupied for a typical 1-2 category overlap.
SUBRECIPE_SIMILARITY_WEIGHT   = 8.0

# A recipe's max achievable kcal (all subrecipes at max serving) must
# reach at least this fraction of an EVEN per-meal-type split of the
# client's daily target, or it's penalized as structurally too small to
# plausibly serve that slot - see _feasibility_penalty.
MIN_SHARE_OF_EVEN_SPLIT       = 0.5
INFEASIBLE_CEILING_PENALTY    = 10.0


# =============================================================================
# BATCH PREFETCHERS
# =============================================================================

def prefetch_flex_stats(recipe_ids: list) -> dict:
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, max_serving, is_main, subrecipe(max_serving)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    counts: dict = defaultdict(int)
    maxes:  dict = defaultdict(int)
    for rs in (resp.data or []):
        rid = rs.get("recipe_id")
        if rid is None:
            continue
        sub = rs.get("subrecipe") or {}
        override = rs.get("max_serving")
        base = sub.get("max_serving")
        resolved = override if override is not None else base
        if resolved is None:
            resolved = ASSUMED_MAIN_HEADROOM if rs.get("is_main") else ASSUMED_NON_MAIN_HEADROOM
        maxes[rid]  += int(resolved)
        counts[rid] += 1
    return {
        rid: {"sub_count": max(counts.get(rid, 0), 1), "sum_max": max(maxes.get(rid, 0), 3)}
        for rid in recipe_ids
    }


def prefetch_subrecipe_sets(recipe_ids: list) -> dict:
    """{recipe_id: frozenset(subrecipe_id)} - real replacement for the
    dead recipe_category table."""
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, subrecipe_id")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    out: dict = defaultdict(set)
    for row in (resp.data or []):
        rid, sid = row.get("recipe_id"), row.get("subrecipe_id")
        if rid is not None and sid is not None:
            out[rid].add(sid)
    return {rid: frozenset(s) for rid, s in out.items()}


def prefetch_weekday_popularity(recipe_ids: list) -> dict:
    """Fixed join. Prod's old version (prefetch_weekday_popularity in
    routes/mealplan_routes.py) selected meal_plan_day.recipe_id/.weekday,
    neither of which exists on that table (recipe_id lives on
    meal_plan_day_recipe; weekday isn't stored, must be derived from
    meal_plan_day.date) - it silently threw, was caught, and fell back
    to a flat 0.5 for every recipe. Correct join below."""
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("meal_plan_day_recipe")
        .select("recipe_id, meal_plan_day(date)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    counts: dict = defaultdict(int)
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        day = row.get("meal_plan_day") or {}
        date_str = day.get("date")
        if rid is None or not date_str:
            continue
        weekday = datetime.strptime(date_str[:10], "%Y-%m-%d").weekday()
        counts[(rid, weekday)] += 1
    return {key: min(c / POPULARITY_CAP, 1.0) for key, c in counts.items()}


def prefetch_recipe_macros(recipe_ids: list) -> dict:
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, subrecipe(kcal, protein, carbs, fat)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    totals: dict = defaultdict(lambda: {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0})
    for rs in (resp.data or []):
        rid = rs.get("recipe_id")
        sub = rs.get("subrecipe") or {}
        if rid is None:
            continue
        totals[rid]["protein"] += float(sub.get("protein") or 0)
        totals[rid]["carbs"]   += float(sub.get("carbs")   or 0)
        totals[rid]["fat"]     += float(sub.get("fat")     or 0)
        totals[rid]["kcal"]    += float(sub.get("kcal")    or 0)
    return dict(totals)


def prefetch_full_subrecipes(recipe_ids: list) -> dict:
    """{recipe_id: [{id, name, max_serving, is_main, macros}, ...]} - same
    shape services/mealplan_service.py's get_recipe_subrecipes() returns
    per-recipe, batched into one query. Feeds the day-level LP-feasibility
    check below so it doesn't hit the DB once per recipe per candidate
    during allocation (get_recipe_subrecipes itself is a live per-recipe
    Supabase call with no batching)."""
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select("recipe_id, max_serving, is_main, subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    out: dict = defaultdict(list)
    for rs in (resp.data or []):
        rid = rs.get("recipe_id")
        sub = rs.get("subrecipe") or {}
        if rid is None or not sub.get("id"):
            continue
        override = rs.get("max_serving")
        base = sub.get("max_serving")
        out[rid].append({
            "id":          sub.get("id"),
            "name":        sub.get("name"),
            "max_serving": override if override is not None else base,
            "is_main":     bool(rs.get("is_main")),
            "macros": {
                "kcal":    float(sub.get("kcal")    or 0.0),
                "protein": float(sub.get("protein") or 0.0),
                "carbs":   float(sub.get("carbs")   or 0.0),
                "fat":     float(sub.get("fat")     or 0.0),
            },
        })
    return dict(out)


# =============================================================================
# SCORING
# =============================================================================

def macro_compat_score(recipe_id: int, recipe_macros: dict, macro_target: dict) -> float:
    m = recipe_macros.get(recipe_id)
    if not m or m.get("kcal", 0) <= 0:
        return 0.0
    kcal = max(m["kcal"], 1.0)
    tgt_kcal = max(macro_target.get("kcal", 1.0), 1.0)
    rec_p = (m["protein"] * 4) / kcal
    rec_c = (m["carbs"]   * 4) / kcal
    rec_f = (m["fat"]     * 9) / kcal
    tgt_p = (macro_target.get("protein_g", 0) * 4) / tgt_kcal
    tgt_c = (macro_target.get("carbs_g",   0) * 4) / tgt_kcal
    tgt_f = (macro_target.get("fat_g",     0) * 9) / tgt_kcal
    diff = abs(rec_p - tgt_p) + abs(rec_c - tgt_c) + abs(rec_f - tgt_f)
    return MACRO_COMPAT_WEIGHT * (0.5 - diff)


def similarity_penalty(rid: int, other_ids, subrecipe_sets: dict) -> float:
    my_subs = subrecipe_sets.get(rid, frozenset())
    if not my_subs or not other_ids:
        return 0.0
    total = 0.0
    for oid in other_ids:
        other_subs = subrecipe_sets.get(oid, frozenset())
        union = my_subs | other_subs
        if not union:
            continue
        total += len(my_subs & other_subs) / len(union)
    return SUBRECIPE_SIMILARITY_WEIGHT * total


def _feasibility_penalty(rid, flex_stats, recipe_macros, macro_target, num_meal_types) -> float:
    """Penalizes a recipe whose own kcal CEILING (all subrecipes at their
    max serving) can't plausibly reach a workable share of this client's
    daily target, regardless of how well it scores on macro-ratio or
    popularity - a real bug found in production: "Seasonal Fruit" (one
    56kcal subrecipe, max_serving=2 -> 112-168kcal ceiling) ranked fine
    on macro-compat and got locked into the snack rotation for an
    1800kcal target, where a workable snack needs real headroom above
    that. The old greedy walk only hit recipes like this occasionally
    (random weighted draw each time); this deterministic ranking would
    otherwise lock it in for every date it's ranked into the rotation.

    Floor is relative to an EVEN per-meal-type split of the target, not
    a fixed number, so it scales correctly across the real kcal range
    (population.json diets run 1000-3500kcal) instead of over-penalizing
    small targets or under-penalizing large ones."""
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    avg_max = flex["sum_max"] / max(flex["sub_count"], 1)
    est_max_kcal = recipe_macros.get(rid, {}).get("kcal", 0.0) * avg_max

    even_split = macro_target.get("kcal", 0.0) / max(num_meal_types, 1)
    floor = MIN_SHARE_OF_EVEN_SPLIT * even_split
    if floor <= 0 or est_max_kcal >= floor:
        return 0.0
    shortfall_frac = 1.0 - (est_max_kcal / floor)
    return INFEASIBLE_CEILING_PENALTY * shortfall_frac


def _quality_score(rid, weekday_range, flex_stats, popularity, recipe_macros, macro_target, num_meal_types) -> float:
    """Day-invariant ranking score for the holistic allocator (seeding a
    SHARED template - no personal like/dislike bias belongs here)."""
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    q = FLEX_SUB_COUNT_WEIGHT * flex["sub_count"] + FLEX_SUM_MAX_WEIGHT * flex["sum_max"]
    q += macro_compat_score(rid, recipe_macros, macro_target)
    if weekday_range:
        avg_pop = sum(popularity.get((rid, wd), 0.5) for wd in weekday_range) / len(weekday_range)
    else:
        avg_pop = 0.5
    q += WEEKDAY_POPULARITY_WEIGHT * avg_pop
    q -= _feasibility_penalty(rid, flex_stats, recipe_macros, macro_target, num_meal_types)
    return q


def composite_score(
    rid, weekday, yesterday_recipe_ids, user_pref, flex_stats, popularity,
    recipe_macros, macro_target, subrecipe_sets, rng: random.Random,
) -> float:
    """Per-CLIENT single-slot swap scoring - used only when a client's
    personal dislike/exclusion forces a swap away from the shared
    template. Not used to seed the template itself (see _quality_score)."""
    score = rng.uniform(0.0, 1.0)
    if user_pref.get("like"):
        score += 2.0
    if user_pref.get("dislike"):
        score -= 5.0
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    score += FLEX_SUB_COUNT_WEIGHT * flex["sub_count"] + FLEX_SUM_MAX_WEIGHT * flex["sum_max"]
    score += WEEKDAY_POPULARITY_WEIGHT * popularity.get((rid, weekday), 0.5)
    score += macro_compat_score(rid, recipe_macros, macro_target)
    score -= similarity_penalty(rid, yesterday_recipe_ids, subrecipe_sets)
    if rid in yesterday_recipe_ids:
        score -= SAME_RECIPE_YESTERDAY_PENALTY
    return score


def _fair_counts(ranked_ids: List[int], num_slots: int) -> Dict[int, int]:
    pool = len(ranked_ids)
    if pool == 0 or num_slots == 0:
        return {}
    if pool >= num_slots:
        return {rid: 1 for rid in ranked_ids[:num_slots]}
    weights = [pool - i for i in range(pool)]
    total_w = sum(weights)
    raw = [num_slots * w / total_w for w in weights]
    counts = [max(1, math.floor(x)) for x in raw]
    remainder = num_slots - sum(counts)
    fractional = sorted(range(pool), key=lambda i: (raw[i] - math.floor(raw[i]), -i), reverse=True)
    i = 0
    while remainder > 0 and i < len(fractional):
        counts[fractional[i]] += 1
        remainder -= 1
        i += 1
    return {ranked_ids[i]: counts[i] for i in range(pool)}


# =============================================================================
# LIVE VOTE QUERY (no new table - aggregates confirmed orders directly)
# =============================================================================

def get_confirmed_vote_counts(date, meal_type: str) -> Dict[int, int]:
    """{recipe_id: confirmed_order_count} for one (date, meal_type) slot,
    read straight from meal_plan_day_recipe / meal_plan_day - the same
    rows order_service.confirm_order() writes at checkout. No separate
    vote table: every confirmed order already IS a vote, and this stays
    correct automatically even if a kitchen admin edits an order via the
    FlutterFlow admin app instead of this backend."""
    resp = (
        supabase.table("meal_plan_day_recipe")
        .select("recipe_id, meal_plan_day!inner(date)")
        .eq("meal_type", meal_type)
        .eq("meal_plan_day.date", str(date))
        .execute()
    )
    counts: Dict[int, int] = defaultdict(int)
    for row in (resp.data or []):
        rid = row.get("recipe_id")
        if rid is not None:
            counts[rid] += 1
    return dict(counts)


def get_current_leader(date, meal_type: str, fallback_recipe_id) -> tuple:
    """Returns (recipe_id, vote_counts). If real confirmed orders exist
    for this slot, the leader is whichever recipe has the most of them
    (plurality). If nobody's confirmed yet, falls back to the bootstrap
    template pick (daily_menu row / holistic allocator output)."""
    counts = get_confirmed_vote_counts(date, meal_type)
    if not counts:
        return fallback_recipe_id, counts
    leader = max(counts.items(), key=lambda kv: kv[1])[0]
    return leader, counts


# =============================================================================
# DAY-LEVEL LP FEASIBILITY CHECK
#
# Found live: ranking each meal type independently (as the allocator
# below does) never checks whether a given DAY's specific 4-recipe
# combination is jointly solvable by the real LP under its normal
# tolerance ladder (hard kcal band + hard macro bounds + meal-type
# distribution caps, all simultaneously). The old greedy day-by-day walk
# checked this implicitly - build_day_candidate drew 30 random full-day
# combinations and score_day penalized ones whose combined estimated max
# kcal fell short of target - so a badly-mismatched combination got
# discarded before it was ever used. Ranking meal types independently
# and assembling a day from four separate schedules has no equivalent
# check, and because the schedules are deterministic (not re-rolled per
# day), a day that happens to combine poorly-matched recipes doesn't
# just fail once - it can recur every time that combination comes up.
#
# Real production symptom this fixes: a 2000kcal client hit
# BEST_EFFORT_LP (mealplan_service.py's last-resort pass, which drops
# BOTH the hard kcal band and hard macro bounds - meant to be rare) on
# every single generated day, producing meal-type kcal swings like
# snack=1000/dinner=500 in a ~2000kcal day.
# =============================================================================

# Single-worker (gunicorn default: sync, no threads - see Procfile) makes
# this safe without a lock, but the lock is cheap insurance against a
# future deploy-config change enabling threaded workers.
_subrecipe_patch_lock = threading.Lock()


def _day_is_lp_feasible(chosen_by_meal: dict, macro_target: dict, subrecipe_cache: dict) -> bool:
    """Cheap feasibility probe: ONE direct call to mealplan_service's own
    _solve_lp_once, at its loosest real tolerance tier (relaxed culinary,
    widest kcal/macro bands, single serving-step) - not the full ladder
    optimize_subrecipes() runs (up to ~24 solves: 2 culinary passes x 4
    tolerance tiers x 3 serving-steps, before even trying BEST_EFFORT_LP).

    Originally called optimize_subrecipes() directly via a monkey-patched
    get_recipe_subrecipes, ~20-30x more expensive per check - fine for a
    handful of calls, but with up to 20 repair attempts x multiple days,
    real requests were taking minutes against Heroku's hard 30s request
    timeout. This checks the loosest tier only: if that fails, tighter
    tiers would too (superset check), and if it succeeds, some tier in
    the real ladder will too - not necessarily the same one, but "clears
    the ladder at all" is all this needs to answer.

    Builds all_subs directly from the prefetched subrecipe_cache instead
    of calling mealplan_service.get_recipe_subrecipes (a live per-recipe
    Supabase call) - no monkey-patching needed at all."""
    import services.mealplan_service as mealplan_service

    recipes_by_meal = {
        mt: {"recipe_id": rid, "meal_type": mt}
        for mt, rid in chosen_by_meal.items()
        if rid is not None
    }
    if len(recipes_by_meal) != len(chosen_by_meal):
        return False

    all_subs = []
    for meal_key, info in recipes_by_meal.items():
        for s in subrecipe_cache.get(info["recipe_id"], []):
            max_serving = s.get("max_serving")
            all_subs.append({
                "meal":         meal_key,
                "subrecipe_id": s["id"],
                "name":         s["name"],
                "macros":       s["macros"],
                "is_main":      bool(s.get("is_main")),
                "max_serving":  (float(int(max_serving)) if max_serving is not None else None),
            })
    if not all_subs:
        return False

    P_t = float(macro_target.get("protein_g") or 0.0)
    C_t = float(macro_target.get("carbs_g")   or 0.0)
    F_t = float(macro_target.get("fat_g")     or 0.0)
    kcal_t = float(macro_target.get("kcal") or (4.0 * (P_t + C_t) + 9.0 * F_t))
    if kcal_t <= 0:
        return False

    tol = mealplan_service.kcal_tolerance(kcal_t, mealplan_service.KCAL_TOLERANCES[-1])
    macro_tols = {
        m: mealplan_service.macro_tolerance(m, t, kcal_t, mealplan_service.BASE_MACRO_TOLERANCES[-1])
        for m, t in (("protein", P_t), ("carbs", C_t), ("fat", F_t))
    }

    with _subrecipe_patch_lock:
        result = mealplan_service._solve_lp_once(
            all_subs=all_subs, recipes_by_meal=recipes_by_meal,
            P_t=P_t, C_t=C_t, F_t=F_t, kcal_t=kcal_t,
            serving_step=1.0, tol=tol, macro_tols=macro_tols,
            allow_under_kcal=False, strict_culinary=False,
            hard_bounds=True, macro_hard_bounds=True,
        )
    return result is not None


# Measured empirically against real weekly_menu 28 data: plain random
# 4-recipe day combinations clear the real solver's tolerance ladder
# ~43% of the time (13/30 in the measurement that found this bug). At
# that rate, 20 attempts fails to find ANY working combo with probability
# 0.57^20 =~ 0.00006 - reliably succeeds. Trying the next-best-RANKED
# alternative instead of a random one was tried first and performed
# WORSE than chance (1/10) - recipes ranked close together tend to share
# a similar macro profile (ranking is macro-compat-heavy), so "nearby"
# swaps don't add the ratio DIVERSITY a day's 4 meals actually need to
# jointly satisfy hard bounds on all three macros at once.
MAX_REPAIR_ATTEMPTS_PER_DAY = 10


def _repair_day_if_infeasible(
    chosen_by_meal: dict, meal_types_by_pool_size: list,
    eligible_today_by_type: dict, macro_target: dict, subrecipe_cache: dict,
    rng: random.Random,
) -> dict:
    """If chosen_by_meal doesn't clear the real LP's normal tolerance
    ladder, try random full-day recombinations (this rescue path only -
    the primary allocation logic above stays fully deterministic/ranked)
    and re-check each with the real solver. Returns the first combination
    that clears the ladder, or the original if none of the bounded random
    attempts do (kept over an infeasible-but-unchanged day since a
    partial improvement is still better than none)."""
    if _day_is_lp_feasible(chosen_by_meal, macro_target, subrecipe_cache):
        return chosen_by_meal

    for _ in range(MAX_REPAIR_ATTEMPTS_PER_DAY):
        trial: dict = {}
        used_today: set = set()
        for mt in meal_types_by_pool_size:
            eligible_today = [r for r in eligible_today_by_type.get(mt, []) if r not in used_today]
            if not eligible_today:
                trial = None
                break
            rid = rng.choice(eligible_today)
            trial[mt] = rid
            used_today.add(rid)
        if trial is None:
            continue
        if _day_is_lp_feasible(trial, macro_target, subrecipe_cache):
            return trial

    return chosen_by_meal


def repair_day_if_infeasible(
    recipes_by_meal: dict, day_target: dict, eligible_by_meal_type: dict,
    recipes_by_id: dict, subrecipe_cache: dict, rng: random.Random,
) -> dict:
    """Public entry point for routes/mealplan_routes.py's day loop.

    Why this has to live there and not just inside get_or_create_week_
    templates: weekly carry-over (mealplan_service.apply_weekly_carryover)
    shifts a day's target by up to 25% based on the actual solved totals
    of EARLIER days in the SAME request, computed sequentially in the
    route's own loop - it isn't known yet at template-allocation time.
    A combo that comfortably clears the ladder against the flat target
    can still fail against a carry-over-shifted one, so the route calls
    this right before/after its own optimize_subrecipes call, against
    the real day_target that call will actually use.

    `recipes_by_meal` is the route's own shape ({meal_key: {recipe_id,
    meal_type, recipe_name, photo}}); returns the same shape, repaired
    if needed and possible, unchanged otherwise."""
    meal_key_by_type = {info["meal_type"]: mk for mk, info in recipes_by_meal.items()}
    chosen_by_meal = {info["meal_type"]: info["recipe_id"] for info in recipes_by_meal.values()}
    meal_types = list(chosen_by_meal.keys())

    repaired = _repair_day_if_infeasible(
        chosen_by_meal, meal_types, eligible_by_meal_type, day_target, subrecipe_cache, rng,
    )
    if repaired == chosen_by_meal:
        return recipes_by_meal

    out: dict = {}
    for mt, rid in repaired.items():
        mk = meal_key_by_type[mt]
        recipe = recipes_by_id.get(rid, {})
        out[mk] = {
            "recipe_id":   rid,
            "meal_key":    mk,
            "meal_type":   mt,
            "recipe_name": recipe.get("name"),
            "photo":       recipe.get("photo"),
        }
    return out


# =============================================================================
# HOLISTIC WHOLE-RANGE ALLOCATOR (bootstrap template for un-templated dates)
# =============================================================================

def _allocate_holistic(
    to_generate: List, meal_types: List[str], eligible_by_date_and_type: Dict,
    flex_stats: dict, popularity: dict, recipe_macros: dict, macro_target: dict,
    fixed_by_date: Dict, subrecipe_cache: Dict, rng: random.Random,
) -> Dict:
    """Returns {date: {meal_type: recipe_id}} for dates in `to_generate`
    only. `eligible_by_date_and_type` is {date: {meal_type: [recipe_id,...]}}
    - per-date eligibility, already resolved by the caller.

    Ranks each meal_type's pool (unioned across `to_generate`) by a
    day-invariant quality score, gives each recipe a fair quota across
    `to_generate`, then walks the dates one at a time (not meal-type-by-
    meal-type) so a same-day cross-meal-type collision - e.g. lunch/
    dinner sharing most of their pool, confirmed against the real
    catalog during the study - gets routed around live, smallest-pool
    meal type first each day. `fixed_by_date` (already-templated dates
    in this same request range) seeds same-day/week-repeat state so
    newly generated dates route around what's already fixed, without
    ever reassigning those dates themselves.

    See scripts/solver_study/menu_lab.py for the validated reference
    this mirrors (weekly_collapse_study.py / vote_ledger_sim.py proved
    it out on real data before this port)."""
    if not to_generate:
        return {}

    n = len(to_generate)
    weekday_range = [d.weekday() for d in to_generate]

    union_pool: Dict[str, set] = defaultdict(set)
    for d in to_generate:
        for mt in meal_types:
            union_pool[mt].update(eligible_by_date_and_type.get(d, {}).get(mt, []))

    meal_types_by_pool_size = sorted(meal_types, key=lambda mt: len(union_pool.get(mt, [])))
    num_meal_types = len(meal_types)

    state: Dict[str, dict] = {}
    for mt in meal_types_by_pool_size:
        pool = list(union_pool.get(mt, []))
        ranked = sorted(
            pool,
            key=lambda rid: _quality_score(
                rid, weekday_range, flex_stats, popularity, recipe_macros, macro_target, num_meal_types
            ),
            reverse=True,
        )
        counts = _fair_counts(ranked, n)
        distinct = len(counts)
        cooldown = max(0, math.ceil(n / distinct) - 1) if distinct else 0
        heap = [(-c, rid) for rid, c in counts.items()]
        heapq.heapify(heap)
        state[mt] = {"heap": heap, "cooldown_q": [], "cooldown": cooldown, "ranked": ranked}

    # Seed same-day / within-range usage from already-fixed dates so
    # newly generated dates never collide with them.
    used_this_range: Dict[str, set] = {mt: set() for mt in meal_types_by_pool_size}
    for d, picks in fixed_by_date.items():
        for mt, rid in picks.items():
            if mt in used_this_range:
                used_this_range[mt].add(rid)

    result: Dict = {}
    for day_index, d in enumerate(to_generate):
        used_today: set = set(fixed_by_date.get(d, {}).values())
        chosen_by_meal: dict = {}

        for mt in meal_types_by_pool_size:
            eligible_today = set(eligible_by_date_and_type.get(d, {}).get(mt, []))
            st = state[mt]
            while st["cooldown_q"] and st["cooldown_q"][0][0] <= day_index:
                _, neg_c, rid = heapq.heappop(st["cooldown_q"])
                heapq.heappush(st["heap"], (neg_c, rid))

            skipped, chosen_rid = [], None
            while st["heap"]:
                neg_c, rid = heapq.heappop(st["heap"])
                if rid not in used_today and rid in eligible_today:
                    chosen_rid = rid
                    remaining = -neg_c - 1
                    if remaining > 0:
                        heapq.heappush(st["cooldown_q"], (day_index + st["cooldown"] + 1, -remaining, rid))
                    break
                skipped.append((neg_c, rid))
            for item in skipped:
                heapq.heappush(st["heap"], item)

            if chosen_rid is None:
                candidates = [e for e in st["cooldown_q"] if e[2] not in used_today and e[2] in eligible_today]
                if candidates:
                    candidates.sort()
                    entry = candidates[0]
                    st["cooldown_q"].remove(entry)
                    heapq.heapify(st["cooldown_q"])
                    _, neg_c, chosen_rid = entry
                    remaining = -neg_c - 1
                    if remaining > 0:
                        heapq.heappush(st["cooldown_q"], (day_index + st["cooldown"] + 1, -remaining, chosen_rid))
                else:
                    week_used = used_this_range[mt]
                    for rid in st["ranked"]:
                        if rid not in used_today and rid in eligible_today and rid not in week_used:
                            chosen_rid = rid
                            break
                    if chosen_rid is None:
                        for rid in st["ranked"]:
                            if rid not in used_today and rid in eligible_today:
                                chosen_rid = rid
                                break
                    if chosen_rid is None:
                        # Truly nothing eligible today for this meal type
                        # (per-date eligibility narrower than the union
                        # pool it was ranked against) - leave unresolved,
                        # caller treats a missing meal_type as a failure
                        # for this date.
                        continue

            chosen_by_meal[mt] = chosen_rid
            used_today.add(chosen_rid)
            used_this_range[mt].add(chosen_rid)

        # Day-level LP-feasibility check: this specific 4-recipe combo,
        # not just each recipe individually, needs to be jointly solvable
        # under the real solver's normal tolerance ladder - see the
        # module comment above _day_is_lp_feasible for the production
        # bug this closes (meal-type kcal swings from unchecked
        # BEST_EFFORT_LP fallbacks).
        eligible_today_by_type = eligible_by_date_and_type.get(d, {})
        repaired = _repair_day_if_infeasible(
            chosen_by_meal, meal_types_by_pool_size,
            eligible_today_by_type, macro_target, subrecipe_cache, rng,
        )
        if repaired is not chosen_by_meal:
            for mt, new_rid in repaired.items():
                old_rid = chosen_by_meal.get(mt)
                if new_rid != old_rid:
                    used_this_range[mt].discard(old_rid)
                    used_this_range[mt].add(new_rid)
            chosen_by_meal = repaired

        result[d] = chosen_by_meal

    return result


# =============================================================================
# PER-CLIENT PERSONAL-PREFERENCE SWAP (dislike / dont_include / missing)
# =============================================================================

def _swap_pick(
    eligible_today: list, used_today: set, weekday: int, yesterday_ids: set,
    user_prefs: dict, flex_stats: dict, popularity: dict, recipe_macros: dict,
    macro_target: dict, subrecipe_sets: dict, rng: random.Random,
) -> Optional[int]:
    pairs = []
    for rid in eligible_today:
        if rid in used_today:
            continue
        if user_prefs.get(rid, {}).get("dont_include"):
            continue
        if macro_compat_score(rid, recipe_macros, macro_target) < MACRO_COMPAT_HARD_FILTER:
            continue
        sc = composite_score(
            rid, weekday, yesterday_ids, user_prefs.get(rid, {}), flex_stats,
            popularity, recipe_macros, macro_target, subrecipe_sets, rng,
        )
        pairs.append((rid, sc))
    if not pairs:
        return None
    min_score = min(sc for _, sc in pairs)
    weights = [max(sc - min_score + 0.001, 0.001) for _, sc in pairs]
    return rng.choices([rid for rid, _ in pairs], weights=weights, k=1)[0]


def _finalize(chosen_by_date: dict, dates: list, meals_map: dict, recipes_by_id: dict) -> dict:
    meal_keys_by_type: Dict[str, list] = defaultdict(list)
    for mk, mt in meals_map.items():
        meal_keys_by_type[mt].append(mk)

    out: Dict = {}
    for d in dates:
        picks = chosen_by_date.get(d)
        if not picks:
            out[d] = None
            continue
        day_out: Dict = {}
        for mt, rid in picks.items():
            recipe = recipes_by_id.get(rid, {})
            for mk in meal_keys_by_type.get(mt, [mt]):
                day_out[mk] = {
                    "recipe_id":   rid,
                    "meal_key":    mk,
                    "meal_type":   mt,
                    "recipe_name": recipe.get("name"),
                    "photo":       recipe.get("photo"),
                }
        out[d] = day_out
    return out


# =============================================================================
# TOP-LEVEL ORCHESTRATOR
# =============================================================================

def get_or_create_week_templates(
    dates: list, meals_map: Dict[str, str], allowed_recipe_ids_by_date: Dict,
    recipes_by_id: Dict, user_prefs: Dict, macro_target: Dict,
    flex_stats: Dict, popularity: Dict, recipe_macros: Dict, subrecipe_sets: Dict,
    rng: random.Random,
) -> Dict:
    """Returns {date: {meal_key: {recipe_id, meal_key, meal_type,
    recipe_name, photo}} or None}. None for a date means it couldn't be
    filled (caller should treat it the same way the old code's `if not
    recipes_by_meal: return 404` did).

    High-kcal clients (> HIGH_KCAL_THRESHOLD) bypass the shared template
    and live-vote convergence entirely and get a personalised range that
    is never persisted to daily_menu - unchanged in spirit from the old
    code, still gets the benefit of the fixed signals + holistic spacing.
    """
    is_high_kcal = macro_target.get("kcal", 0) > HIGH_KCAL_THRESHOLD
    meal_types = sorted(set(meals_map.values()))

    # eligible_by_date_and_type must cover ALL meal types, not just the
    # ones this request asked for. `picks` below is read straight off the
    # shared daily_menu table and can carry meal-type slots from an
    # earlier, broader request (e.g. an original 4-meal order) even when
    # this call only asks for one of them. The per-client swap pass
    # further down evaluates every slot in `picks` (a client's dislike on
    # a meal type they didn't even order still has to be repairable), and
    # was looking up eligible_by_date_and_type.get(d, {}).get(mt, []) for
    # those unrequested types - which used to come back [] because this
    # dict only had keys for the requested subset, so any unrequested
    # slot the client happened to dislike had zero swap candidates and
    # took the whole day down with a false "Not enough unique recipes"
    # 404 - confirmed live: user 00818acd's daily_menu for 2026-08-17 had
    # lunch/snack slots baked in from an earlier 4-meal order that this
    # client dislikes, and a follow-up 1-meal (dinner-only) order failed
    # even though dinner itself was a liked recipe with plenty of
    # candidates. `meal_types` (requested-only) is still what's passed to
    # _allocate_holistic below, so template generation for brand-new
    # dates is unaffected - this only widens the swap-repair's pool.
    eligible_by_date_and_type: Dict = {}
    for d in dates:
        allowed = allowed_recipe_ids_by_date.get(d, set())
        by_type = {mt: [] for mt in MEAL_TYPES}
        for rid in allowed:
            r = recipes_by_id.get(rid)
            if not r:
                continue
            for mt in MEAL_TYPES:
                if r.get(f"could_be_{mt}"):
                    by_type[mt].append(rid)
        eligible_by_date_and_type[d] = by_type

    if is_high_kcal:
        chosen_by_date = _allocate_holistic(
            dates, meal_types, eligible_by_date_and_type,
            flex_stats, popularity, recipe_macros, macro_target, fixed_by_date={},
            subrecipe_cache=subrecipe_cache, rng=rng,
        )
        return _finalize(chosen_by_date, dates, meals_map, recipes_by_id)

    # 1. Batch-fetch existing daily_menu rows for every date at once.
    resp = (
        supabase.table("daily_menu")
        .select("date, meal_type, recipe_id, created_at")
        .in_("date", [str(d) for d in dates])
        .execute()
    )
    rows_by_date: Dict = defaultdict(list)
    for row in (resp.data or []):
        try:
            row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        rows_by_date[row_date].append(row)

    fixed_by_date: Dict = {}
    for d in dates:
        rows = rows_by_date.get(d, [])
        if not rows:
            continue
        try:
            oldest = min(
                datetime.strptime(r["created_at"][:19], "%Y-%m-%dT%H:%M:%S")
                for r in rows if r.get("created_at")
            )
            if (datetime.utcnow() - oldest).days > TEMPLATE_TTL_DAYS:
                continue  # stale - treat as not-templated, regenerate below
        except Exception:
            pass
        picks = {r["meal_type"]: r["recipe_id"] for r in rows if r.get("meal_type") and r.get("recipe_id")}
        if picks:
            fixed_by_date[d] = picks

    to_generate = [d for d in dates if d not in fixed_by_date]

    # 2. Holistic allocation for the un-templated dates only.
    newly_generated = _allocate_holistic(
        to_generate, meal_types, eligible_by_date_and_type,
        flex_stats, popularity, recipe_macros, macro_target, fixed_by_date,
        subrecipe_cache=subrecipe_cache, rng=rng,
    )

    # 3. Persist newly generated dates - same table/upsert shape as before.
    upsert_rows = [
        {"date": str(d), "meal_type": mt, "recipe_id": rid}
        for d, picks in newly_generated.items()
        for mt, rid in picks.items()
    ]
    if upsert_rows:
        supabase.table("daily_menu").upsert(
            upsert_rows, on_conflict="date,meal_type", ignore_duplicates=True,
        ).execute()

    base_by_date: Dict = dict(fixed_by_date)
    base_by_date.update(newly_generated)

    # 4. Prefer the live vote leader (real confirmed orders) over the
    #    frozen/bootstrap pick, per slot.
    resolved_by_date: Dict = {}
    for d in dates:
        picks = base_by_date.get(d)
        if not picks:
            resolved_by_date[d] = None
            continue
        resolved_by_date[d] = {
            mt: get_current_leader(d, mt, base_rid)[0] for mt, base_rid in picks.items()
        }

    # 5. Per-client personal-preference swap pass (dislike / dont_include /
    #    missing recipe), applied on top of the converged shared pick -
    #    unchanged in spirit from the old per-slot swap, now scored with
    #    real popularity + real subrecipe-similarity instead of the two
    #    dead signals.
    final_by_date: Dict = {}
    yesterday_ids: set = set()
    for d in dates:
        picks = resolved_by_date.get(d)
        if not picks:
            final_by_date[d] = None
            continue
        weekday = d.weekday()
        used_today = set(picks.values())
        resolved: Dict[str, int] = {}
        needs_swap = []

        for mt, rid in picks.items():
            pref = user_prefs.get(rid, {})
            recipe = recipes_by_id.get(rid)
            if not recipe or pref.get("dont_include") or pref.get("dislike"):
                needs_swap.append(mt)
                continue
            resolved[mt] = rid

        for mt in needs_swap:
            eligible_today = eligible_by_date_and_type.get(d, {}).get(mt, [])
            new_rid = _swap_pick(
                eligible_today, used_today, weekday, yesterday_ids, user_prefs,
                flex_stats, popularity, recipe_macros, macro_target, subrecipe_sets, rng,
            )
            if new_rid is None:
                final_by_date[d] = None
                break
            resolved[mt] = new_rid
            used_today.add(new_rid)
        else:
            # NOTE: a day-level feasibility check against `macro_target`
            # (the flat, un-adjusted target) doesn't belong here - the
            # ACTUAL solve for this day uses a weekly-carry-over-adjusted
            # target computed sequentially in routes/mealplan_routes.py's
            # day loop, which isn't known yet at this point. That route
            # calls daily_menu_service.repair_day_if_infeasible() itself,
            # against the real target, right where it calls
            # optimize_subrecipes - see that function's docstring.
            final_by_date[d] = resolved
            yesterday_ids = set(resolved.values())
            continue
        # only reached via the inner `break` above (a slot truly couldn't
        # be filled even after the swap pass)
        yesterday_ids = set()

    return _finalize(final_by_date, dates, meals_map, recipes_by_id)
