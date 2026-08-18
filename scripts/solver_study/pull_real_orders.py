"""
pull_real_orders.py — pulls ALL real historical meal_plan_day rows (not the
4 fixed study recipes) so the solver can be re-run against genuine past
client orders: the client's REAL diet target (daily_macro_target, keyed by
user_id and versioned over time -- this is where the client's actual diet
lives, not daily_macro_order, which turned out to be a same-transaction,
per-day-adjusted record written alongside the delivered servings rather
than an independent pre-solve goal), the actual recipe assigned per
meal_type that day (meal_plan_day_recipe), the actual servings production
delivered (meal_plan_day_recipe_serving, for a "what did production really
ship" reference only -- not a solver-accuracy baseline), and the real
recipe_subrecipe data (is_main + max_serving resolution, same resolution
rule as services/mealplan_service.py's get_recipe_subrecipes()) for every
distinct recipe_id encountered.

For each day, the client's active diet is the daily_macro_target row for
that user with the latest created_at on or before the day's date (a user's
diet can be updated over time, e.g. "high_protein" -> "Custom Diet").

Run with:
    venv/Scripts/python.exe scripts/solver_study/pull_real_orders.py

Writes:
    data/real_orders_days.json     -- one record per historical day
    data/real_orders_fixture.json  -- recipe_id -> subrecipes, for every
                                       recipe referenced by those days
"""

import bisect
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.supabase_client import supabase

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DAYS_OUT_PATH = os.path.join(DATA_DIR, "real_orders_days.json")
FIXTURE_OUT_PATH = os.path.join(DATA_DIR, "real_orders_fixture.json")

PAGE_SIZE = 1000


def fetch_all(table, select, filters=None):
    rows = []
    start = 0
    while True:
        q = supabase.table(table).select(select)
        if filters:
            for col, op, val in filters:
                q = getattr(q, op)(col, val)
        resp = q.range(start, start + PAGE_SIZE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def main():
    print("Fetching meal_plan_day ...")
    days = fetch_all("meal_plan_day", "id, date, daily_macro_order_id, meal_plan_id")
    print(f"  {len(days)} rows")

    print("Fetching daily_macro_order (per-day record, NOT the real diet target -- kept for reference only) ...")
    orders = fetch_all("daily_macro_order", "id, protein_ordered, carbs_ordered, fat_ordered, kcal_ordered")
    orders_by_id = {o["id"]: o for o in orders}
    print(f"  {len(orders)} rows")

    print("Fetching meal_plan (for user_id) ...")
    plans = fetch_all("meal_plan", "id, user_id")
    plan_user_by_id = {p["id"]: p["user_id"] for p in plans}
    print(f"  {len(plans)} rows")

    print("Fetching daily_macro_target (the REAL client diet, versioned per user) ...")
    targets = fetch_all(
        "daily_macro_target",
        "id, user_id, created_at, diet_type, protein_g, carbs_g, fat_g, kcal_target",
    )
    print(f"  {len(targets)} rows")

    targets_by_user = defaultdict(list)
    for t in targets:
        targets_by_user[t["user_id"]].append(t)
    for user_id in targets_by_user:
        targets_by_user[user_id].sort(key=lambda t: t["created_at"])

    def active_target_for(user_id, date_str):
        rows = targets_by_user.get(user_id)
        if not rows:
            return None
        # date_str is a plain date ("2025-11-26"); compare against created_at's
        # date component so a target created same-day still counts as active.
        created_dates = [r["created_at"][:10] for r in rows]
        idx = bisect.bisect_right(created_dates, date_str) - 1
        if idx < 0:
            return None
        return rows[idx]

    print("Fetching meal_plan_day_recipe ...")
    mpdr_rows = fetch_all("meal_plan_day_recipe", "id, meal_plan_day_id, meal_type, recipe_id")
    print(f"  {len(mpdr_rows)} rows")

    print("Fetching meal_plan_day_recipe_serving ...")
    serving_rows = fetch_all(
        "meal_plan_day_recipe_serving",
        "meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated, "
        "kcal_calculated, protein_calculated, carbs_calculated, fat_calculated",
    )
    print(f"  {len(serving_rows)} rows")

    print("Fetching recipe names ...")
    recipe_ids = sorted({r["recipe_id"] for r in mpdr_rows if r.get("recipe_id")})
    names = {}
    for i in range(0, len(recipe_ids), PAGE_SIZE):
        batch = recipe_ids[i : i + PAGE_SIZE]
        resp = supabase.table("recipe").select("id, name").in_("id", batch).execute()
        for r in resp.data or []:
            names[r["id"]] = r["name"]
    print(f"  {len(names)} recipes named")

    # --- reconstruct per-day records -------------------------------------
    mpdr_by_day = defaultdict(list)
    for r in mpdr_rows:
        mpdr_by_day[r["meal_plan_day_id"]].append(r)

    served_by_mpdr = defaultdict(list)
    for s in serving_rows:
        served_by_mpdr[s["meal_plan_day_recipe_id"]].append(s)

    day_records = []
    skipped_no_order = 0
    skipped_no_recipes = 0
    skipped_no_target = 0
    for d in days:
        order = orders_by_id.get(d["daily_macro_order_id"])
        if not order:
            skipped_no_order += 1
            continue
        user_id = plan_user_by_id.get(d["meal_plan_id"])
        real_target = active_target_for(user_id, d["date"]) if user_id else None
        if not real_target:
            skipped_no_target += 1
            continue
        mpdrs = mpdr_by_day.get(d["id"], [])
        if not mpdrs:
            skipped_no_recipes += 1
            continue

        recipes_by_meal = {}
        actual_totals = {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
        has_serving_data = False
        for mpdr in mpdrs:
            mt = mpdr.get("meal_type")
            rid = mpdr.get("recipe_id")
            if not mt or not rid:
                continue
            recipes_by_meal[mt] = {"recipe_id": rid, "recipe_name": names.get(rid), "meal_type": mt}
            for s in served_by_mpdr.get(mpdr["id"], []):
                has_serving_data = True
                actual_totals["kcal"] += float(s.get("kcal_calculated") or 0.0)
                actual_totals["protein"] += float(s.get("protein_calculated") or 0.0)
                actual_totals["carbs"] += float(s.get("carbs_calculated") or 0.0)
                actual_totals["fat"] += float(s.get("fat_calculated") or 0.0)

        if not recipes_by_meal:
            skipped_no_recipes += 1
            continue

        day_records.append({
            "day_id": d["id"],
            "date": d["date"],
            "target": {
                "protein_g": float(real_target.get("protein_g") or 0.0),
                "carbs_g": float(real_target.get("carbs_g") or 0.0),
                "fat_g": float(real_target.get("fat_g") or 0.0),
                "kcal": float(real_target.get("kcal_target") or 0.0),
            },
            "target_diet_type": real_target.get("diet_type"),
            "target_id": real_target.get("id"),
            "daily_macro_order": {  # kept for reference only -- NOT used as the solve target
                "protein_g": float(order.get("protein_ordered") or 0.0),
                "carbs_g": float(order.get("carbs_ordered") or 0.0),
                "fat_g": float(order.get("fat_ordered") or 0.0),
                "kcal": float(order.get("kcal_ordered") or 0.0),
            },
            "recipes_by_meal": recipes_by_meal,
            "production_actual": actual_totals if has_serving_data else None,
        })

    print(f"\nReconstructed {len(day_records)} usable days "
          f"(skipped {skipped_no_order} w/o order, {skipped_no_target} w/o a matching real diet target, "
          f"{skipped_no_recipes} w/o recipes)")

    # --- pull real recipe_subrecipe fixture for every recipe referenced --
    print(f"\nFetching recipe_subrecipe fixtures for {len(recipe_ids)} distinct recipes ...")
    subs_by_recipe = {}
    zero_sub_recipes = []
    for i, rid in enumerate(recipe_ids, 1):
        resp = (
            supabase.table("recipe_subrecipe")
            .select("max_serving, is_main, subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
            .eq("recipe_id", rid)
            .execute()
        )
        subs = []
        for rs in resp.data or []:
            sub = rs.get("subrecipe") or {}
            if not sub.get("id"):
                continue
            override = rs.get("max_serving")
            base = sub.get("max_serving")
            resolved_max = override if override is not None else base
            subs.append({
                "id": sub["id"], "name": sub.get("name"),
                "is_main": bool(rs.get("is_main")),
                "max_serving": resolved_max,
                "macros": {
                    "kcal":    float(sub.get("kcal")    or 0.0),
                    "protein": float(sub.get("protein") or 0.0),
                    "carbs":   float(sub.get("carbs")   or 0.0),
                    "fat":     float(sub.get("fat")     or 0.0),
                },
            })
        subs_by_recipe[rid] = subs
        if not subs:
            zero_sub_recipes.append(rid)
        if i % 20 == 0 or i == len(recipe_ids):
            print(f"  {i}/{len(recipe_ids)}")

    if zero_sub_recipes:
        print(f"\n  WARNING: {len(zero_sub_recipes)} recipes have zero subrecipes: {zero_sub_recipes}")

    # drop day records that reference a zero-sub recipe -- can't be solved
    before = len(day_records)
    zero_set = set(zero_sub_recipes)
    day_records = [
        d for d in day_records
        if not any(info["recipe_id"] in zero_set for info in d["recipes_by_meal"].values())
    ]
    dropped = before - len(day_records)
    if dropped:
        print(f"  Dropped {dropped} days referencing a zero-sub recipe (now {len(day_records)} usable days)")

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DAYS_OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"source": "LIVE Supabase read, real historical client orders", "days": day_records}, f, indent=2)
    with open(FIXTURE_OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "source": "LIVE Supabase read, real recipe_subrecipe data for every recipe referenced by real_orders_days.json",
            "recipe_names": names,
            "subs_by_recipe": subs_by_recipe,
        }, f, indent=2)

    print(f"\nWrote {DAYS_OUT_PATH} ({len(day_records)} days)")
    print(f"Wrote {FIXTURE_OUT_PATH} ({len(subs_by_recipe)} recipes)")


if __name__ == "__main__":
    main()
