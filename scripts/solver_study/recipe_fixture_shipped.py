"""
recipe_fixture_shipped.py — freezes the REAL, currently-live recipe_subrecipe
data (is_main + max_serving resolution) for the study's 4 fixed recipes, so
verify_shipped.py can re-run the 528-point harness against the actual
production services/mealplan_service.py without hitting Supabase per point.

Unlike recipe_fixture.json (subrecipe-level max_serving only) and
recipe_fixture_with_mains.json (simulated is_main, 4 guessed labels), this
pulls the REAL is_main values and the REAL max_serving resolution
(recipe_subrecipe.max_serving override, falling back to subrecipe.max_serving,
else None) exactly as services/mealplan_service.py's get_recipe_subrecipes()
resolves it. See the Word doc for what turned out to differ from the
sandbox's guesses (multi-main meals, a zero-main recipe, manual overrides).

Run with:
    venv/Scripts/python.exe scripts/solver_study/recipe_fixture_shipped.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.supabase_client import supabase

OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recipe_fixture_shipped.json")

FIXED_RECIPES = {"breakfast": 41, "lunch": 34, "snack": 59, "dinner": 50}
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


def fetch_subrecipes_real(recipe_id):
    """Mirrors services/mealplan_service.py's get_recipe_subrecipes() exactly:
    max_serving = recipe_subrecipe.max_serving override, else subrecipe.max_serving, else None."""
    resp = (
        supabase.table("recipe_subrecipe")
        .select("max_serving, is_main, subrecipe(id, name, max_serving, kcal, protein, carbs, fat)")
        .eq("recipe_id", recipe_id)
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
            "max_serving": resolved_max,  # None means unbounded -- preserved as null in JSON
            "macros": {
                "kcal":    float(sub.get("kcal")    or 0.0),
                "protein": float(sub.get("protein") or 0.0),
                "carbs":   float(sub.get("carbs")   or 0.0),
                "fat":     float(sub.get("fat")     or 0.0),
            },
        })
    return subs


def main():
    recipe_ids = list(FIXED_RECIPES.values())
    names = fetch_recipe_names(recipe_ids)

    recipes_by_meal_type = {}
    for meal_type, recipe_id in FIXED_RECIPES.items():
        subs = fetch_subrecipes_real(recipe_id)
        n_main = sum(1 for s in subs if s["is_main"])
        recipes_by_meal_type[meal_type] = {
            "recipe_id": recipe_id, "recipe_name": names.get(recipe_id),
            "meal_type": meal_type, "subrecipes": subs,
        }
        flag = "  [!! ZERO MAINS]" if n_main == 0 else ""
        print(f"  {meal_type:<10} recipe_id={recipe_id:<4} {names.get(recipe_id):<35} "
              f"{len(subs)} subrecipe(s), {n_main} main(s){flag}")
        for s in subs:
            tag = "MAIN" if s["is_main"] else "    "
            cap = s["max_serving"] if s["max_serving"] is not None else "unbounded"
            print(f"      [{tag}] {s['name']:<25} max_serving={cap}")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "source": "LIVE Supabase read, real production data (not simulated)",
            "fixed_recipes": FIXED_RECIPES,
            "recipes_by_meal_type": recipes_by_meal_type,
            "meal_combos": MEAL_COMBOS,
        }, f, indent=2)

    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
