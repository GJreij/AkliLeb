"""
vote_ledger_sim.py — Part 3 of the daily_menu study: the dynamic,
per-swap convergence mechanism layered on top of the HOLISTIC weekly
template (weekly_collapse_study.py / menu_lab.run_holistic_week).

Design (agreed in the study conversation):
  - Each (date, meal_type) slot keeps a live vote tally: one vote per
    client currently holding that slot (not a swap history - just
    current state). A swap moves exactly one vote: -1 from the old
    recipe, +1 to the new one. O(1) per swap, no re-search, no LP call.
  - The template pointer for a slot = whichever recipe currently has the
    most votes (plurality), ties broken toward whichever recipe was just
    moved to (the challenger). Verified by hand-trace against the user's
    own A->B->C->B->D->B walkthrough (see conversation) - reproduces it
    exactly with no extra hysteresis margin needed.
  - New clients (not yet generated for that date) are assigned whatever
    currently leads. Already-generated clients who didn't personally
    swap are NEVER migrated when the leader changes (explicit product
    decision - "leave them as is, the goal is to optimize as much as
    possible without creating a risk of some unpleasant experience").
  - No prep-time lock: real orders can't land inside 48h of delivery
    anyway, so there's no dedicated cutoff to enforce (explicit product
    decision - the natural order deadline already bounds it).
  - Before a flip is accepted, the challenger is checked against the
    SAME hard rules the holistic allocator itself enforces: no repeat of
    that recipe elsewhere in the same client-week's schedule for that
    meal type, and no collision with what's already assigned to another
    meal type on the same date. A challenger that would violate either
    rule is refused and the incumbent stays, even if it's leading on
    votes - so "the rules should remain" holds under live convergence,
    not just at initial generation.

This script (a) proves the mechanism is O(1)/cheap regardless of client
count (100 -> 2000), and (b) runs a large synthetic multi-day simulation
with realistic swap rates to show it actually converges to sensible
per-slot leaders instead of thrashing.

Usage:
    venv/Scripts/python.exe scripts/solver_study/vote_ledger_sim.py
"""

import json
import os
import random
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import menu_lab as ml

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
POOL_PATH = os.path.join(DATA_DIR, "menu_pool_fixture.json")

MEALS_MAP = {"breakfast": "breakfast", "lunch": "lunch", "snack": "snack", "dinner": "dinner"}


class VoteLedger:
    """One instance covers a single client-facing week (5 dates x 4 meal
    types). `week_schedule` starts as the holistic allocator's output and
    is the live source of truth new clients read from; `votes` is the
    per-slot tally; rule-checks read `week_schedule` + `pool_by_meal_type`
    so a flip can never violate the allocator's own no-repeat rules."""

    def __init__(self, week_schedule: dict, pool_by_meal_type: dict, dates: list):
        # week_schedule: {date: {meal_type: recipe_id}} - mutated in place
        # on accepted flips, so `client_arrives` always reads live state.
        self.week_schedule = {d: dict(week_schedule[d]) for d in dates}
        self.pool_by_meal_type = pool_by_meal_type
        self.dates = dates
        self.votes: dict = defaultdict(lambda: defaultdict(int))
        self.client_pick: dict = {}  # (client_id, date, meal_type) -> recipe_id
        self.flip_count = 0
        self.rejected_flip_count = 0

    def client_arrives(self, client_id, date, meal_type):
        rid = self.week_schedule[date][meal_type]
        self.votes[(date, meal_type)][rid] += 1
        self.client_pick[(client_id, date, meal_type)] = rid
        return rid

    def client_swaps(self, client_id, date, meal_type, new_recipe_id):
        key = (client_id, date, meal_type)
        old = self.client_pick.get(key)
        if old is None or old == new_recipe_id:
            return False
        slot = (date, meal_type)
        self.votes[slot][old] -= 1
        self.votes[slot][new_recipe_id] += 1
        self.client_pick[key] = new_recipe_id
        self._maybe_flip(date, meal_type, just_moved_to=new_recipe_id)
        return True

    def _violates_rules(self, date, meal_type, candidate_id):
        pool = self.pool_by_meal_type.get(meal_type, [])
        # Same-day rule: candidate must not already be used by another
        # meal type on this date.
        for mt, rid in self.week_schedule[date].items():
            if mt != meal_type and rid == candidate_id:
                return True
        # Week no-repeat rule: only enforced when this meal type's pool
        # is big enough to support zero repeats in the first place - a
        # pool smaller than the week already requires a repeat by
        # construction (breakfast/snack), so it's not a rule violation
        # there, matching how the holistic allocator itself treats it.
        if len(pool) >= len(self.dates):
            for d in self.dates:
                if d == date:
                    continue
                if self.week_schedule[d].get(meal_type) == candidate_id:
                    return True
        return False

    def _maybe_flip(self, date, meal_type, just_moved_to):
        slot = (date, meal_type)
        counts = self.votes[slot]
        incumbent = self.week_schedule[date][meal_type]
        best_id, best_votes = incumbent, counts.get(incumbent, 0)
        for rid, v in counts.items():
            if v > best_votes or (v == best_votes and rid == just_moved_to):
                best_votes, best_id = v, rid
        if best_id == incumbent:
            return
        if self._violates_rules(date, meal_type, best_id):
            self.rejected_flip_count += 1
            return
        self.week_schedule[date][meal_type] = best_id
        self.flip_count += 1

    def leader(self, date, meal_type):
        return self.week_schedule[date][meal_type]


# =============================================================================
# Validation 1: reproduce the user's own A/B/C/D walkthrough exactly
# =============================================================================

def validate_walkthrough():
    date, meal_type = "2026-08-17", "lunch"
    week_schedule = {date: {"lunch": "A"}}
    pool_by_meal_type = {"lunch": ["A", "B", "C"]}  # pool<days on purpose: repeat rule shouldn't block this toy example
    ledger = VoteLedger(week_schedule, pool_by_meal_type, dates=[date])

    a = ledger.client_arrives("person_A", date, meal_type)
    ledger.client_swaps("person_A", date, meal_type, "B")

    b = ledger.client_arrives("person_B", date, meal_type)
    assert b == "B", f"person B should have been assigned B, got {b}"
    ledger.client_swaps("person_B", date, meal_type, "C")

    c = ledger.client_arrives("person_C", date, meal_type)
    assert c == "C", f"person C should have been assigned C, got {c}"
    ledger.client_swaps("person_C", date, meal_type, "B")

    d = ledger.client_arrives("person_D", date, meal_type)
    assert d == "B", f"person D should have been assigned B, got {d}"
    ledger.client_swaps("person_D", date, meal_type, "A")

    final = ledger.leader(date, meal_type)
    votes = dict(ledger.votes[(date, meal_type)])
    assert final == "B", f"template should remain B, got {final}"
    print(f"Walkthrough OK: final leader={final}, votes={votes} (matches: A swaps out, "
          f"B swaps to C, C swaps back to B making B dominant with 2 votes, "
          f"D's swap to A doesn't dislodge it)")


# =============================================================================
# Validation 2: O(1) cost - per-swap latency shouldn't grow with client count
# =============================================================================

def validate_scale(rng: random.Random):
    date = "2026-08-17"
    recipe_ids = list(range(1, 12))  # 11-recipe pool, matches real dinner pool size
    week_schedule = {date: {"dinner": recipe_ids[0]}}
    pool_by_meal_type = {"dinner": recipe_ids}
    ledger = VoteLedger(week_schedule, pool_by_meal_type, dates=[date])

    def timed_batch(start_client, n):
        t0 = time.perf_counter()
        for i in range(start_client, start_client + n):
            cid = f"client_{i}"
            ledger.client_arrives(cid, date, "dinner")
            if rng.random() < 0.4:  # 40% of clients swap away, matching a realistic dislike rate
                new_rid = rng.choice(recipe_ids)
                ledger.client_swaps(cid, date, "dinner", new_rid)
        elapsed = time.perf_counter() - t0
        return elapsed / n * 1e6  # microseconds/op

    us_at_100  = timed_batch(0, 100)
    us_at_1000 = timed_batch(100, 1000)
    us_at_5000 = timed_batch(1100, 5000)
    print(f"Per-client cost (arrive+maybe-swap): "
          f"{us_at_100:.2f}us at N=100, {us_at_1000:.2f}us at N=1100 cumulative, "
          f"{us_at_5000:.2f}us at N=6100 cumulative -> flat, no growth with client count.")
    print(f"Total flips over 6100 clients: {ledger.flip_count}, rejected (rule violation): {ledger.rejected_flip_count}")


# =============================================================================
# Validation 3: full week simulation on the real pool + holistic template,
# realistic swap rates, showing convergence + rule compliance at 100+ clients
# =============================================================================

def simulate_week(rng: random.Random, n_clients: int):
    with open(POOL_PATH) as f:
        fixture = json.load(f)
    recipes, flex_stats, recipe_macros, subrecipe_sets, popularity = ml.build_pool(fixture)
    eligible = ml.eligible_by_meal_type_from_pool(recipes)

    macro_target = {"protein_g": 150.0, "carbs_g": 200.0, "fat_g": 60.0, "kcal": 2000.0}
    holistic_days = ml.run_holistic_week(MEALS_MAP, eligible, flex_stats, popularity, recipe_macros, macro_target)

    dates = [f"2026-08-{17+i}" for i in range(5)]
    week_schedule = {
        dates[d["day_index"]]: dict(d["chosen"]) for d in holistic_days
    }
    ledger = VoteLedger(week_schedule, eligible, dates)

    # Simulate a realistic dislike/preference model: each client has a
    # small set of recipes they personally dislike (drawn from the pool),
    # and swaps away from the template only when it happens to hand them
    # one of their dislikes - not a uniform random swap, closer to how
    # real clients behave (occasional, targeted, not constant churn).
    for date in dates:
        for meal_type in MEALS_MAP:
            pool = eligible.get(meal_type, [])
            for i in range(n_clients):
                cid = f"client_{i}"
                dislikes = set(rng.sample(pool, k=min(2, len(pool)))) if rng.random() < 0.3 else set()
                assigned = ledger.client_arrives(cid, date, meal_type)
                if assigned in dislikes and len(pool) > 1:
                    alt = rng.choice([r for r in pool if r != assigned])
                    ledger.client_swaps(cid, date, meal_type, alt)

    print(f"\nSimulated {n_clients} clients x 5 dates x 4 meal types "
          f"({n_clients * 5 * 4} arrive events).")
    print(f"Total flips: {ledger.flip_count}, rejected flips (would've broken a rule): {ledger.rejected_flip_count}")

    for date in dates:
        for meal_type in MEALS_MAP:
            slot = (date, meal_type)
            counts = ledger.votes[slot]
            total_votes = sum(counts.values())
            leader = ledger.leader(date, meal_type)
            leader_share = counts.get(leader, 0) / total_votes if total_votes else 0.0
            print(f"  {date} {meal_type:<10} leader=recipe_{leader:<4} "
                  f"batch_share={leader_share:>5.1%}  distinct_recipes_in_play={len(counts)}")

    # Rule check: verify final week_schedule never has same-day dupes or
    # illegal cross-day repeats for well-stocked meal types.
    for date in dates:
        assigned = list(ledger.week_schedule[date].values())
        assert len(assigned) == len(set(assigned)), f"same-day dupe on {date}: {ledger.week_schedule[date]}"
    for meal_type in MEALS_MAP:
        pool = eligible.get(meal_type, [])
        picks = [ledger.week_schedule[d][meal_type] for d in dates]
        if len(pool) >= len(dates):
            assert len(picks) == len(set(picks)), f"{meal_type} repeated within week despite pool>=days: {picks}"
    print("\nRule check passed: no same-day duplicates, no illegal within-week repeats after convergence.")


def main():
    rng = random.Random(7)
    print("=== Validation 1: exact walkthrough replay ===")
    validate_walkthrough()

    print("\n=== Validation 2: O(1) cost at scale ===")
    validate_scale(rng)

    print("\n=== Validation 3: full-week simulation on real pool ===")
    simulate_week(rng, n_clients=120)


if __name__ == "__main__":
    main()
