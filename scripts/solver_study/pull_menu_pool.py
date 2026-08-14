"""
pull_menu_pool.py — freezes a real weekly_menu's full recipe pool (all
meal-type-eligible recipes, their subrecipe/macro/is_main data) plus real
historical weekday popularity, to data/menu_pool_fixture.json so the
daily_menu template-selection study never depends on live Supabase state.

Defaults to weekly_menu id=28 (2026-08-17 to 2026-08-31, the current/
upcoming week at time of writing) — representative of every other recent
week checked (22-27): pool size is consistently ~18-19 recipes total,
tight for breakfast/snack (4-6 recipes for 5 weekday slots) and
comfortable for lunch/dinner (10-12). This tightness is the core input
to the Friday-collapse study, not a quirk of this one week.

Also fixes the broken production popularity query in the process: prod's
prefetch_weekday_popularity() (routes/mealplan_routes.py) selects
meal_plan_day.recipe_id/.weekday, but those columns don't exist on that
table (recipe_id lives on meal_plan_day_recipe; weekday isn't stored
anywhere and must be derived from meal_plan_day.date). Prod's try/except
silently swallows this and falls back to a flat 0.5 for every recipe —
the "historical popularity" scoring signal has never actually run. This
script does the correct join so the study can test with real signal.

Run with:
    venv/Scripts/python.exe scripts/solver_study/pull_menu_pool.py [--weekly-menu-id N]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.supabase_client import supabase

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "menu_pool_fixture.json")
DEFAULT_WEEKLY_MENU_ID = 28
MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")


def fetch_recipes(weekly_menu_id: int) -> dict:
    resp = (
        supabase.table("weekly_menu_recipe")
        .select("recipe(id, name, could_be_breakfast, could_be_lunch, could_be_dinner, could_be_snack)")
        .eq("weekly_menu_id", weekly_menu_id)
        .execute()
    )
    recipes = {}
    for row in resp.data or []:
        r = row.get("recipe") or {}
        if r.get("id"):
            recipes[r["id"]] = r
    return recipes


def fetch_subrecipes(recipe_ids: list) -> dict:
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("recipe_subrecipe")
        .select(
            "recipe_id, is_main, max_serving, optional, "
            "subrecipe(id, name, max_serving, kcal, protein, carbs, fat)"
        )
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    by_recipe = defaultdict(list)
    for row in resp.data or []:
        sub = row.get("subrecipe") or {}
        if not sub.get("id"):
            continue
        override = row.get("max_serving")
        base = sub.get("max_serving")
        by_recipe[row["recipe_id"]].append({
            "subrecipe_id": sub["id"],
            "name": sub.get("name"),
            "is_main": bool(row.get("is_main")),
            "optional": bool(row.get("optional")),
            "max_serving": override if override is not None else base,
            "macros": {
                "kcal":    float(sub.get("kcal") or 0.0),
                "protein": float(sub.get("protein") or 0.0),
                "carbs":   float(sub.get("carbs") or 0.0),
                "fat":     float(sub.get("fat") or 0.0),
            },
        })
    return dict(by_recipe)


def fetch_weekday_popularity(recipe_ids: list) -> dict:
    """
    { (recipe_id, weekday): order_count } — real signal, correctly joined
    (see module docstring for why prod's version silently returns nothing).
    """
    if not recipe_ids:
        return {}
    resp = (
        supabase.table("meal_plan_day_recipe")
        .select("recipe_id, meal_plan_day(date)")
        .in_("recipe_id", recipe_ids)
        .execute()
    )
    counts: dict = defaultdict(int)
    for row in resp.data or []:
        rid = row.get("recipe_id")
        day = row.get("meal_plan_day") or {}
        date_str = day.get("date")
        if rid is None or not date_str:
            continue
        weekday = datetime.strptime(date_str[:10], "%Y-%m-%d").weekday()
        counts[(rid, weekday)] += 1
    return dict(counts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weekly-menu-id", type=int, default=DEFAULT_WEEKLY_MENU_ID)
    args = parser.parse_args()

    recipes = fetch_recipes(args.weekly_menu_id)
    recipe_ids = list(recipes.keys())
    subs_by_recipe = fetch_subrecipes(recipe_ids)
    popularity_counts = fetch_weekday_popularity(recipe_ids)

    eligible = {mt: 0 for mt in MEAL_TYPES}
    for r in recipes.values():
        for mt in MEAL_TYPES:
            if r.get(f"could_be_{mt}"):
                eligible[mt] += 1

    fixture = {
        "source_weekly_menu_id": args.weekly_menu_id,
        "pool_size":             len(recipes),
        "eligible_counts":       eligible,
        "recipes": {
            str(rid): {**r, "subrecipes": subs_by_recipe.get(rid, [])}
            for rid, r in recipes.items()
        },
        "popularity_counts": {
            f"{rid}:{wd}": c for (rid, wd), c in popularity_counts.items()
        },
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2)

    print(f"weekly_menu_id={args.weekly_menu_id}")
    print(f"Pool size: {len(recipes)} recipes")
    print(f"Eligible per meal type: {eligible}")
    print(f"Popularity data points (recipe,weekday pairs with history): {len(popularity_counts)}")
    print(f"Wrote fixture -> {OUT_PATH}")


if __name__ == "__main__":
    main()
