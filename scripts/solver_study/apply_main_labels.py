"""
apply_main_labels.py — simulates a future 'is_main' column on
recipe_subrecipe (doesn't exist in the real DB yet) by overlaying it onto
a COPY of the frozen recipe fixture. The real recipe_fixture.json is never
touched, so this is purely a sandbox exercise -- no schema, frontend, or
backend change required to test the idea.

Main assignment is a product judgment call, not a data fact -- flagged
here explicitly so it's easy to correct:
  breakfast (Greek Yogurt and Nuts) -> Greek Yogurt (Plain) [id 24]
  lunch     (Shawarma Meat Plate)   -> Shawarma Meat        [id 49]
  snack     (Energy Bomb)           -> Halawa Date Mixed Nuts [id 85] (only subrecipe -- must be main)
  dinner    (Siyadiye)              -> Siyadiye Main        [id 75]

Validates the "every recipe has >=1 main" invariant before writing.

Run with:
    venv/Scripts/python.exe scripts/solver_study/apply_main_labels.py
"""

import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SRC_PATH = os.path.join(DATA_DIR, "recipe_fixture.json")
OUT_PATH = os.path.join(DATA_DIR, "recipe_fixture_with_mains.json")

MAIN_SUBRECIPE_IDS = {24, 49, 85, 75}


def main():
    with open(SRC_PATH, encoding="utf-8") as f:
        fixture = json.load(f)

    for meal_type, info in fixture["recipes_by_meal_type"].items():
        n_main = 0
        for s in info["subrecipes"]:
            s["is_main"] = s["id"] in MAIN_SUBRECIPE_IDS
            if s["is_main"]:
                n_main += 1
        if n_main == 0:
            raise ValueError(f"{meal_type} ({info['recipe_name']}) has zero mains -- invalid, fix MAIN_SUBRECIPE_IDS")
        print(f"{meal_type:<10} {info['recipe_name']:<35} "
              f"mains: {[s['name'] for s in info['subrecipes'] if s['is_main']]}")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2)
    print(f"\nWrote {OUT_PATH} (simulated -- real recipe_fixture.json unchanged)")


if __name__ == "__main__":
    main()
