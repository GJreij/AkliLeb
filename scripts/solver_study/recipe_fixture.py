"""
recipe_fixture.py — freezes the study's fixed recipe combination to
data/recipe_fixture.json so the harness never depends on live Supabase
state (or network) once built. See the study's Word document, Part B §2.

Fixed recipes, chosen from weekly_menu id 28 (2026-08-17 to 2026-08-31) for
subrecipe count / solver headroom:
  breakfast -> Greek Yogurt and Nuts        (recipe id 41, 3 subrecipes)
  lunch     -> Shawarma Meat Plate          (recipe id 34, 5 subrecipes)
  snack     -> Energy Bomb: Halawa, Dates & Nuts (recipe id 59, 1 subrecipe)
  dinner    -> Siyadiye                     (recipe id 50, 4 subrecipes)

The six meal-combo variants are subsets of this same fixed set.

Run with:
    venv/Scripts/python.exe scripts/solver_study/recipe_fixture.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.supabase_client import supabase

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recipe_fixture.json")

FIXED_RECIPES = {
    "breakfast": 41,
    "lunch":     34,
    "snack":     59,
    "dinner":    50,
}

MEAL_COMBOS = {
    "full_day":              ["breakfast", "lunch", "snack", "dinner"],
    "lunch_snack_dinner":    ["lunch", "snack", "dinner"],
    "lunch_dinner":          ["lunch", "dinner"],
    "lunch_only":            ["lunch"],
    "dinner_only":           ["dinner"],
    "lunch_snack":           ["lunch", "snack"],
}


def fetch_recipe_names(recipe_ids):
    resp = supabase.table("recipe").select("id, name").in_("id", recipe_ids).execute()
    return {r["id"]: r["name"] for r in (resp.data or [])}


def fetch_subrecipes(recipe_id):
    resp = (
        supabase.table("recipe_subrecipe")
        .select("subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .eq("recipe_id", recipe_id)
        .execute()
    )
    subs = []
    for row in resp.data or []:
        sub = row.get("subrecipe") or {}
        if not sub.get("id"):
            continue
        subs.append({
            "id":          sub["id"],
            "name":        sub.get("name"),
            "max_serving": sub.get("max_serving") or 3,
            "macros": {
                "kcal":    float(sub.get("kcal") or 0.0),
                "protein": float(sub.get("protein") or 0.0),
                "carbs":   float(sub.get("carbs") or 0.0),
                "fat":     float(sub.get("fat") or 0.0),
            },
        })
    return subs


def fetch_rules(recipe_id):
    resp = (
        supabase.table("recipe_subrecipe_rule")
        .select("subrecipe_a_id, subrecipe_b_id, rule_type, ratio, fixed_servings")
        .eq("recipe_id", recipe_id)
        .execute()
    )
    return resp.data or []


def main():
    recipe_ids = list(FIXED_RECIPES.values())
    names = fetch_recipe_names(recipe_ids)

    recipes_by_meal_type = {}
    for meal_type, recipe_id in FIXED_RECIPES.items():
        subs = fetch_subrecipes(recipe_id)
        rules = fetch_rules(recipe_id)
        recipes_by_meal_type[meal_type] = {
            "recipe_id":   recipe_id,
            "recipe_name": names.get(recipe_id),
            "meal_type":   meal_type,
            "subrecipes":  subs,
            "rules":       rules,
        }
        print(f"  {meal_type:<10} recipe_id={recipe_id:<4} {names.get(recipe_id):<35} "
              f"{len(subs)} subrecipe(s), {len(rules)} rule(s)")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "source_weekly_menu_id": 28,
            "fixed_recipes": FIXED_RECIPES,
            "recipes_by_meal_type": recipes_by_meal_type,
            "meal_combos": MEAL_COMBOS,
        }, f, indent=2)

    print(f"\nWrote fixture -> {OUT_PATH}")


if __name__ == "__main__":
    main()
