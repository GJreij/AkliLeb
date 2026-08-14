"""
menu_lab.py — sandbox twin of the daily_menu template-selection logic in
routes/mealplan_routes.py (get_or_create_daily_template / build_day_candidate
/ composite_score / score_day). Pure in-memory against menu_pool_fixture.json,
no Supabase calls — same relationship solver_lab.py has to mealplan_service.py.

Three scoring/allocation variants, all runnable through the same interface,
so the study can attribute improvement correctly (which part of the fix
actually matters):

  BASELINE_CFG   - faithful clone of current production. Category-overlap
                   penalty is a permanent no-op (recipe_category table
                   doesn't exist) and weekday-popularity is a permanent
                   flat 0.5 (prod's query selects columns that don't exist
                   on meal_plan_day and silently falls back). Recipe
                   selection is greedy, day-by-day, driven by two deques
                   (recent_global, meal_history) that only reset per
                   /generate_meal_plan call.

  FIXED_SIGNALS_CFG - same greedy day-by-day walk, but with the two dead
                   signals repaired: real weekday popularity (from actual
                   meal_plan_day_recipe/meal_plan_day history) and a real
                   same-day/day-to-day similarity signal (subrecipe-set
                   overlap) replacing the fictional category overlap.
                   Isolates how much of the fix is "repair the signals"
                   vs "repair the allocation strategy".

  HOLISTIC_CFG   - full fix: fixed signals AND a whole-week allocator
                   (run_holistic_week) that ranks recipes by a
                   day-invariant quality score and schedules them across
                   the week with explicit spacing, instead of a greedy
                   walk that has no visibility past "yesterday". Also
                   drops personal like/dislike bias from template
                   generation (see run_holistic_week docstring) since a
                   SHARED template getting anchored to one anonymous
                   first-mover's taste is a separate bug this surfaced.
"""

import heapq
import math
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")

# ---- constants mirrored 1:1 from routes/mealplan_routes.py -----------------
CATEGORY_OVERLAP_PENALTY      = 4.0
SAME_RECIPE_YESTERDAY_PENALTY = 10.0
WEEKDAY_POPULARITY_WEIGHT     = 1.5
FLEX_SUB_COUNT_WEIGHT         = 0.5
FLEX_SUM_MAX_WEIGHT           = 0.1
ASSUMED_MAIN_HEADROOM         = 6
ASSUMED_NON_MAIN_HEADROOM     = 3
BEST_DAY_TRIES_DEFAULT        = 30
RECENT_GLOBAL_MAXLEN          = 10
MEAL_HISTORY_MAXLEN           = 20
POPULARITY_CAP                = 50
MACRO_COMPAT_WEIGHT           = 3.0
MACRO_COMPAT_HARD_FILTER      = -2.5

# New: replaces CATEGORY_OVERLAP_PENALTY. Jaccard similarity of subrecipe
# sets (0-1) * this weight. 8.0 chosen so two recipes sharing ~half their
# subrecipes land in the same penalty range the old category penalty
# occupied for a 1-2 category overlap (4.0-8.0) - comparable magnitude,
# real signal.
SUBRECIPE_SIMILARITY_WEIGHT   = 8.0

# A recipe's max achievable kcal (all subrecipes at max serving) must
# reach at least this fraction of an EVEN per-meal-type split of the
# client's daily target, or it's penalized as structurally too small to
# plausibly serve that slot - see _feasibility_penalty.
MIN_SHARE_OF_EVEN_SPLIT       = 0.5
INFEASIBLE_CEILING_PENALTY    = 10.0


@dataclass
class ScoringConfig:
    use_real_popularity:      bool = False
    use_subrecipe_similarity: bool = False
    use_prefs:                bool = True


BASELINE_CFG       = ScoringConfig(use_real_popularity=False, use_subrecipe_similarity=False, use_prefs=True)
FIXED_SIGNALS_CFG  = ScoringConfig(use_real_popularity=True,  use_subrecipe_similarity=True,  use_prefs=True)
HOLISTIC_CFG       = ScoringConfig(use_real_popularity=True,  use_subrecipe_similarity=True,  use_prefs=False)


# =============================================================================
# Fixture -> in-memory pool
# =============================================================================

def build_pool(fixture: dict):
    """
    Returns (recipes, flex_stats, recipe_macros, subrecipe_sets, popularity)
    from a menu_pool_fixture.json dict.
    """
    recipes = {}
    flex_stats = {}
    recipe_macros = {}
    subrecipe_sets = {}

    for rid_str, r in fixture["recipes"].items():
        rid = int(rid_str)
        recipes[rid] = r

        subs = r.get("subrecipes", [])
        sub_count = max(len(subs), 1)
        sum_max = 0
        for s in subs:
            resolved = s.get("max_serving")
            if resolved is None:
                resolved = ASSUMED_MAIN_HEADROOM if s.get("is_main") else ASSUMED_NON_MAIN_HEADROOM
            sum_max += int(resolved)
        flex_stats[rid] = {"sub_count": sub_count, "sum_max": max(sum_max, 3)}

        totals = {"protein": 0.0, "carbs": 0.0, "fat": 0.0, "kcal": 0.0}
        for s in subs:
            m = s.get("macros", {})
            totals["protein"] += float(m.get("protein") or 0)
            totals["carbs"]   += float(m.get("carbs") or 0)
            totals["fat"]     += float(m.get("fat") or 0)
            totals["kcal"]    += float(m.get("kcal") or 0)
        recipe_macros[rid] = totals

        subrecipe_sets[rid] = frozenset(s["subrecipe_id"] for s in subs)

    popularity = {}
    for key, count in fixture.get("popularity_counts", {}).items():
        rid_str, wd_str = key.split(":")
        popularity[(int(rid_str), int(wd_str))] = min(count / POPULARITY_CAP, 1.0)

    return recipes, flex_stats, recipe_macros, subrecipe_sets, popularity


# =============================================================================
# Scoring (mirrors _macro_compat_score / composite_score in production,
# parameterized by ScoringConfig so baseline/fixed/holistic share one path)
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
    """Jaccard overlap of subrecipe sets vs a set of 'other' recipe ids
    (yesterday's picks, or same-day picks so far). Real replacement for
    the dead category-overlap penalty."""
    my_subs = subrecipe_sets.get(rid, frozenset())
    if not my_subs or not other_ids:
        return 0.0
    total = 0.0
    for oid in other_ids:
        other_subs = subrecipe_sets.get(oid, frozenset())
        union = my_subs | other_subs
        if not union:
            continue
        jaccard = len(my_subs & other_subs) / len(union)
        total += jaccard
    return SUBRECIPE_SIMILARITY_WEIGHT * total


def composite_score(
    rid: int, weekday: int, yesterday_recipe_ids: set, user_pref: dict,
    flex_stats: dict, popularity: dict, recipe_macros: dict, macro_target: dict,
    subrecipe_sets: dict, cfg: ScoringConfig, rng: random.Random,
) -> float:
    score = rng.uniform(0.0, 1.0)

    if cfg.use_prefs:
        if user_pref.get("like"):
            score += 2.0
        if user_pref.get("dislike"):
            score -= 5.0

    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    score += FLEX_SUB_COUNT_WEIGHT * flex["sub_count"]
    score += FLEX_SUM_MAX_WEIGHT   * flex["sum_max"]

    if cfg.use_real_popularity:
        score += WEEKDAY_POPULARITY_WEIGHT * popularity.get((rid, weekday), 0.5)
    else:
        score += WEEKDAY_POPULARITY_WEIGHT * 0.5  # dead signal, always flat

    score += macro_compat_score(rid, recipe_macros, macro_target)

    if cfg.use_subrecipe_similarity:
        score -= similarity_penalty(rid, yesterday_recipe_ids, subrecipe_sets)
    # else: dead signal, contributes 0 (matches empty-category behaviour)

    if rid in yesterday_recipe_ids:
        score -= SAME_RECIPE_YESTERDAY_PENALTY

    return score


def weighted_choice_by_score(candidates: list, scores: list, rng: random.Random):
    min_score = min(scores)
    weights = [max(s - min_score + 0.001, 0.001) for s in scores]
    return rng.choices(candidates, weights=weights, k=1)[0]


def score_day(chosen_by_meal: dict, flex_stats: dict, recipe_macros: dict, macro_target: dict) -> float:
    total_sub, total_sum_max, single_sub_meals = 0, 0, 0
    for rid in chosen_by_meal.values():
        flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
        total_sub     += flex["sub_count"]
        total_sum_max += flex["sum_max"]
        if flex["sub_count"] <= 1:
            single_sub_meals += 1

    base = (10.0 * total_sub) + (1.5 * total_sum_max) - (12.0 * single_sub_meals)

    kcal_t = macro_target.get("kcal", 0.0)
    if kcal_t > 0:
        est_max_kcal = 0.0
        for rid in chosen_by_meal.values():
            flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
            avg_max = flex["sum_max"] / max(flex["sub_count"], 1)
            est_max_kcal += recipe_macros.get(rid, {}).get("kcal", 0.0) * avg_max
        shortfall = max(0.0, 1.0 - est_max_kcal / kcal_t)
        base -= 50.0 * shortfall

    return base, single_sub_meals


# =============================================================================
# BASELINE / FIXED_SIGNALS: greedy day-by-day walk (faithful clone of
# build_day_candidate + get_or_create_daily_template's "no template yet"
# branch + generate_meal_plan's day loop)
# =============================================================================

def build_day_candidate(
    meals_map: dict, eligible_by_meal_type: dict, recent_global: deque, meal_hist: deque,
    weekday: int, yesterday_recipe_ids: set, user_prefs: dict, flex_stats: dict,
    popularity: dict, recipe_macros: dict, macro_target: dict, subrecipe_sets: dict,
    cfg: ScoringConfig, rng: random.Random,
) -> Tuple[Optional[dict], dict]:
    """Returns (chosen_by_meal | None, pool_sizes_by_meal_key) — pool_sizes
    is the metric the collapse study cares about: how many real candidates
    were actually available for each slot before ONE got picked."""
    chosen_by_meal: dict = {}
    used_today: set = set()
    pool_sizes: dict = {}

    for meal_key, meal_type in meals_map.items():
        def score_candidates(strict: bool):
            pairs = []
            for rid in eligible_by_meal_type.get(meal_type, []):
                if rid in used_today:
                    continue
                if strict and (rid in recent_global or rid in meal_hist):
                    continue
                if macro_compat_score(rid, recipe_macros, macro_target) < MACRO_COMPAT_HARD_FILTER:
                    continue
                sc = composite_score(
                    rid, weekday, yesterday_recipe_ids, user_prefs.get(rid, {}),
                    flex_stats, popularity, recipe_macros, macro_target,
                    subrecipe_sets, cfg, rng,
                )
                pairs.append((rid, sc))
            return pairs

        pairs = score_candidates(strict=True)
        used_relaxed = False
        if not pairs:
            pairs = score_candidates(strict=False)
            used_relaxed = True
        pool_sizes[meal_key] = {"count": len(pairs), "used_relaxed": used_relaxed}
        if not pairs:
            return None, pool_sizes

        candidates, scores = zip(*pairs)
        chosen = weighted_choice_by_score(list(candidates), list(scores), rng)
        chosen_by_meal[meal_key] = chosen
        used_today.add(chosen)

    return chosen_by_meal, pool_sizes


def run_greedy_week(
    meals_map: dict, eligible_by_meal_type: dict, user_prefs: dict, flex_stats: dict,
    popularity: dict, recipe_macros: dict, macro_target: dict, subrecipe_sets: dict,
    cfg: ScoringConfig, num_days: int = 5, best_tries: int = BEST_DAY_TRIES_DEFAULT,
    seed: int = 0,
) -> List[dict]:
    """Faithful clone of generate_meal_plan's day loop when no daily_menu
    template exists yet for any date in the week (the actual code path
    that creates the template everyone else inherits)."""
    rng = random.Random(seed)
    recent_global = deque(maxlen=RECENT_GLOBAL_MAXLEN)
    meal_history  = deque(maxlen=MEAL_HISTORY_MAXLEN)
    yesterday_recipe_ids: set = set()

    days_out = []
    for day_index in range(num_days):
        weekday = day_index  # Mon=0..Fri=4
        best_day, best_score, best_pool_sizes, best_single_sub = None, float("-inf"), None, None

        # aggregate pool-size stats across all best_tries attempts for this
        # day (the metric: how constrained was candidate selection today)
        agg_pool_sizes = defaultdict(list)

        for _ in range(best_tries):
            candidate, pool_sizes = build_day_candidate(
                meals_map, eligible_by_meal_type, recent_global, meal_history,
                weekday, yesterday_recipe_ids, user_prefs, flex_stats,
                popularity, recipe_macros, macro_target, subrecipe_sets, cfg, rng,
            )
            for mk, info in pool_sizes.items():
                agg_pool_sizes[mk].append(info["count"])
            if not candidate:
                continue
            sc, single_sub = score_day(candidate, flex_stats, recipe_macros, macro_target)
            if sc > best_score:
                best_score, best_day, best_single_sub = sc, candidate, single_sub

        if best_day is None:
            days_out.append({
                "day_index": day_index, "weekday": weekday, "failed": True,
                "pool_sizes": {mk: (min(v) if v else 0) for mk, v in agg_pool_sizes.items()},
            })
            break

        for rid in best_day.values():
            meal_history.append(rid)
            recent_global.append(rid)
        yesterday_recipe_ids = set(best_day.values())

        days_out.append({
            "day_index": day_index, "weekday": weekday, "failed": False,
            "chosen": dict(best_day), "score": best_score, "single_sub_meals": best_single_sub,
            "pool_sizes": {mk: (min(v) if v else 0) for mk, v in agg_pool_sizes.items()},
        })

    return days_out


# =============================================================================
# HOLISTIC: whole-week allocator. Ranks recipes by a day-invariant quality
# score (no yesterday/recency dependence), decides a fair per-recipe
# appearance count for the week, then schedules appearances across days
# maximizing the minimum gap between repeats of the same recipe (classic
# task-scheduler-with-cooldown greedy). Runs once per meal_type, then
# assembles days by combining the four independent schedules.
# =============================================================================

def _feasibility_penalty(rid, flex_stats, recipe_macros, macro_target, num_meal_types=4) -> float:
    """Penalizes a recipe whose own kcal CEILING (all subrecipes at max
    serving) can't plausibly reach a workable share of the client's
    daily target - found via this same study's live production test:
    "Seasonal Fruit" (one 56kcal subrecipe, max_serving=2 -> ~168kcal
    ceiling) ranked fine on macro-compat/flex/popularity alone and got
    deterministically locked into the snack rotation for an 1800kcal
    target, where the LP then had nowhere near enough headroom to give
    snack a workable share - see services/daily_menu_service.py for the
    production fix this mirrors."""
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    avg_max = flex["sum_max"] / max(flex["sub_count"], 1)
    est_max_kcal = recipe_macros.get(rid, {}).get("kcal", 0.0) * avg_max
    even_split = macro_target.get("kcal", 0.0) / max(num_meal_types, 1)
    floor = MIN_SHARE_OF_EVEN_SPLIT * even_split
    if floor <= 0 or est_max_kcal >= floor:
        return 0.0
    shortfall_frac = 1.0 - (est_max_kcal / floor)
    return INFEASIBLE_CEILING_PENALTY * shortfall_frac


def _quality_score(
    rid: int, flex_stats: dict, popularity: dict, recipe_macros: dict,
    macro_target: dict, num_days: int,
) -> float:
    flex = flex_stats.get(rid, {"sub_count": 1, "sum_max": 3})
    q = FLEX_SUB_COUNT_WEIGHT * flex["sub_count"] + FLEX_SUM_MAX_WEIGHT * flex["sum_max"]
    q += macro_compat_score(rid, recipe_macros, macro_target)
    avg_pop = sum(popularity.get((rid, wd), 0.5) for wd in range(num_days)) / num_days
    q += WEEKDAY_POPULARITY_WEIGHT * avg_pop
    q -= _feasibility_penalty(rid, flex_stats, recipe_macros, macro_target)
    return q


def _fair_counts(ranked_ids: List[int], num_days: int) -> Dict[int, int]:
    """Given recipes ranked best-first, decide how many of the num_days
    slots each gets. If pool >= num_days: top num_days recipes get 1 each
    (no repeats - achievable, so we take it). If pool < num_days: repeats
    are unavoidable; distribute them so better recipes repeat more, using
    largest-remainder apportionment weighted by rank (best recipe gets the
    most extra slots, but everyone eligible gets at least 1)."""
    pool = len(ranked_ids)
    if pool >= num_days:
        return {rid: 1 for rid in ranked_ids[:num_days]}

    # weight = inverse rank (rank 0 heaviest), largest-remainder method
    weights = [pool - i for i in range(pool)]
    total_w = sum(weights)
    raw = [num_days * w / total_w for w in weights]
    counts = [max(1, math.floor(x)) for x in raw]
    remainder = num_days - sum(counts)
    # hand out leftover slots to the highest-remainder (best-ranked-first
    # on ties) recipes
    fractional = sorted(
        range(pool), key=lambda i: (raw[i] - math.floor(raw[i]), -i), reverse=True
    )
    i = 0
    while remainder > 0 and i < len(fractional):
        counts[fractional[i]] += 1
        remainder -= 1
        i += 1
    return {ranked_ids[i]: counts[i] for i in range(pool)}


def run_holistic_week(
    meals_map: dict, eligible_by_meal_type: dict, flex_stats: dict, popularity: dict,
    recipe_macros: dict, macro_target: dict, num_days: int = 5,
) -> List[dict]:
    """Deterministic - no rng needed, this is a ranking + scheduling
    problem, not a random draw. meals_map values are meal_type strings
    (breakfast/lunch/dinner/snack); this assumes one meal_key per
    meal_type (matches the default meals_map used in production).

    Each meal_type gets its own quota (via _fair_counts) and its own
    cooldown-spaced heap, but heaps are advanced DAY-BY-DAY together (not
    meal-type-by-meal-type independently) so a same-day cross-meal-type
    collision can be detected and routed around - e.g. lunch and dinner
    pools overlap heavily in this catalog (many recipes are eligible for
    both), so scheduling them independently would sometimes hand a client
    the identical recipe for lunch and dinner on the same day, breaking
    the "no repeat within a day" rule. Meal types are processed smallest-
    pool-first each day (least flexibility gets first pick); a meal type
    with a bigger pool routes around whatever's already used that day.
    """
    meal_types_by_pool_size = sorted(
        set(meals_map.values()), key=lambda mt: len(eligible_by_meal_type.get(mt, []))
    )
    meal_key_by_type = {mt: mk for mk, mt in meals_map.items()}

    state: Dict[str, dict] = {}
    for meal_type in meal_types_by_pool_size:
        pool = eligible_by_meal_type.get(meal_type, [])
        ranked = sorted(
            pool,
            key=lambda rid: _quality_score(rid, flex_stats, popularity, recipe_macros, macro_target, num_days),
            reverse=True,
        )
        counts = _fair_counts(ranked, num_days)
        distinct = len(counts)
        cooldown = max(0, math.ceil(num_days / distinct) - 1) if distinct else 0
        heap = [(-c, rid) for rid, c in counts.items()]
        heapq.heapify(heap)
        # `ranked` (the FULL eligible pool, not just the ones _fair_counts
        # gave a quota to) is kept as a same-day-collision fallback below -
        # a well-stocked meal type (e.g. dinner's pool of 11 trimmed to a
        # 5-recipe weekly rotation) still has real alternatives to reach
        # for if its preferred pick collides with another meal type today.
        state[meal_type] = {"heap": heap, "cooldown_q": [], "cooldown": cooldown, "ranked": ranked}

    used_this_week: Dict[str, set] = {mt: set() for mt in meal_types_by_pool_size}

    days_out = []
    for day in range(num_days):
        used_today: set = set()
        chosen_by_meal: dict = {}

        for meal_type in meal_types_by_pool_size:
            st = state[meal_type]
            while st["cooldown_q"] and st["cooldown_q"][0][0] <= day:
                _, neg_c, rid = heapq.heappop(st["cooldown_q"])
                heapq.heappush(st["heap"], (neg_c, rid))

            skipped, chosen_rid = [], None
            while st["heap"]:
                neg_c, rid = heapq.heappop(st["heap"])
                if rid not in used_today:
                    chosen_rid = rid
                    remaining = -neg_c - 1
                    if remaining > 0:
                        heapq.heappush(st["cooldown_q"], (day + st["cooldown"] + 1, -remaining, rid))
                    break
                skipped.append((neg_c, rid))
            for item in skipped:
                heapq.heappush(st["heap"], item)

            if chosen_rid is None:
                # This meal type's quota'd rotation collided entirely with
                # today's already-used recipes. First try the cooldown
                # queue, ignoring the spacing timer (same-day exclusion
                # still enforced) ...
                candidates = [e for e in st["cooldown_q"] if e[2] not in used_today]
                if candidates:
                    candidates.sort()  # most negative = most remaining, first
                    entry = candidates[0]
                    st["cooldown_q"].remove(entry)
                    heapq.heapify(st["cooldown_q"])
                    _, neg_c, chosen_rid = entry
                    remaining = -neg_c - 1
                    if remaining > 0:
                        heapq.heappush(st["cooldown_q"], (day + st["cooldown"] + 1, -remaining, chosen_rid))
                else:
                    # ... then reach past the quota'd subset into the full
                    # eligible pool for this meal type, best-quality first.
                    # Same-day distinctness is a hard rule and always wins,
                    # but within that we still try HARD not to introduce a
                    # within-week repeat: first pass excludes anything
                    # already used elsewhere this week for this meal type;
                    # only if that's completely exhausted (pool too small
                    # to avoid it - the breakfast/snack case) do we accept
                    # a within-week repeat as the true last resort.
                    week_used = used_this_week[meal_type]
                    for rid in st["ranked"]:
                        if rid not in used_today and rid not in week_used:
                            chosen_rid = rid
                            break
                    if chosen_rid is None:
                        for rid in st["ranked"]:
                            if rid not in used_today:
                                chosen_rid = rid
                                break

            chosen_by_meal[meal_key_by_type[meal_type]] = chosen_rid
            if chosen_rid is not None:
                used_today.add(chosen_rid)
                used_this_week[meal_type].add(chosen_rid)

        sc, single_sub = score_day(chosen_by_meal, flex_stats, recipe_macros, macro_target)
        days_out.append({
            "day_index": day, "weekday": day, "failed": any(v is None for v in chosen_by_meal.values()),
            "chosen": chosen_by_meal, "score": sc, "single_sub_meals": single_sub,
        })
    return days_out


def eligible_by_meal_type_from_pool(recipes: dict) -> dict:
    out = defaultdict(list)
    for rid, r in recipes.items():
        for mt in MEAL_TYPES:
            if r.get(f"could_be_{mt}"):
                out[mt].append(rid)
    return dict(out)
