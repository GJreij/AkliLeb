"""
population.py — builds the 88-diet study population and freezes it to
data/population.json so every later run (baseline or any candidate version)
solves against the exact same targets.

10 real diets ("Person 1"..."Person 10"): random individual rows pulled
as-is from daily_macro_target, label mess included — see the study's Word
document, Part B §1.

78 synthetic diets: 6 kcal tiers x 13 macro archetypes, built with the same
byWeight() mechanism the live diet_wizard uses (FrontEnd/akli-web/src/lib/
macros.ts) — protein g/kg + fat g/kg, carbs = kcal remainder, floored at
0.75 g/kg. One balanced center (identical to the real "balanced" diet
type) plus three independent axes stepped out to high/extremely-high and
low/extremely-low. See the Word doc for the full table and the "why" of
each number.

Run with:
    venv/Scripts/python.exe scripts/solver_study/population.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from utils.supabase_client import supabase

RANDOM_SEED = 42
N_REAL_DIETS = 10
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "population.json")

CARB_FLOOR_G_PER_KG = 0.75

# kcal tier -> reference bodyweight (kg). Fixed lookup, not derived from a
# real per-person profile — documented assumption, see Word doc "Open
# Assumptions & Decisions".
KCAL_TIERS = {
    1000: 45, 1500: 60, 2000: 75, 2500: 85, 3000: 95, 3500: 110,
}

# 13 archetypes: one balanced center + 3 axes (protein / fat / carbs) x
# {extremely-low, low, high, extremely-high}. Carbs move indirectly: the
# axis raises/lowers protein+fat TOGETHER, since byWeight() always fills
# carbs as the kcal remainder. Values match the Word doc's archetype table
# exactly.
ARCHETYPES = {
    "balanced":                 {"pk": 1.6, "fk": 1.0},
    "low_protein":               {"pk": 1.1, "fk": 1.0},
    "extremely_low_protein":     {"pk": 0.6, "fk": 1.0},
    "high_protein":              {"pk": 2.0, "fk": 1.0},
    "extremely_high_protein":    {"pk": 2.6, "fk": 1.0},
    "low_fat":                   {"pk": 1.6, "fk": 0.5},
    "extremely_low_fat":         {"pk": 1.6, "fk": 0.25},
    "high_fat":                  {"pk": 1.6, "fk": 1.6},
    "extremely_high_fat":        {"pk": 1.6, "fk": 2.2},
    "low_carbs":                 {"pk": 2.0, "fk": 1.4},
    "extremely_low_carbs":       {"pk": 2.4, "fk": 1.8},
    "high_carbs":                {"pk": 1.2, "fk": 0.6},
    "extremely_high_carbs":      {"pk": 0.9, "fk": 0.35},
}


def by_weight(kcal, weight, pk, fk):
    """Ported 1:1 from FrontEnd/akli-web/src/lib/macros.ts byWeight()."""
    p = weight * pk
    f = weight * fk
    carb_floor_g = weight * CARB_FLOOR_G_PER_KG
    max_pf_kcal = kcal - carb_floor_g * 4
    pf_kcal = p * 4 + f * 9
    if max_pf_kcal <= 0:
        p, f = 0.0, 0.0
    elif pf_kcal > max_pf_kcal:
        scale = max_pf_kcal / pf_kcal
        p *= scale
        f *= scale
    carbs = max(carb_floor_g, (kcal - p * 4 - f * 9) / 4)
    return {
        "protein_g": round(p, 1),
        "carbs_g":   round(carbs, 1),
        "fat_g":     round(f, 1),
        "kcal":      float(kcal),
    }


def fetch_real_diets(n=N_REAL_DIETS, seed=RANDOM_SEED):
    rows = (
        supabase.table("daily_macro_target")
        .select("id, diet_type, goal, protein_g, carbs_g, fat_g, kcal_target")
        .execute()
        .data or []
    )
    rows = [r for r in rows if (r.get("kcal_target") or 0) > 0]
    rng = random.Random(seed)
    sample = rng.sample(rows, min(n, len(rows)))

    diets = []
    for i, r in enumerate(sample, start=1):
        diets.append({
            "diet_id":    f"person_{i}",
            "label":      f"Person {i}",
            "group":      "real",
            "source_row_id": r.get("id"),
            "diet_type":  r.get("diet_type"),
            "goal":       r.get("goal"),
            "protein_g":  float(r.get("protein_g") or 0),
            "carbs_g":    float(r.get("carbs_g") or 0),
            "fat_g":      float(r.get("fat_g") or 0),
            "kcal":       float(r.get("kcal_target") or 0),
        })
    return diets


def build_synthetic_diets():
    diets = []
    for kcal, weight in KCAL_TIERS.items():
        for archetype, gk in ARCHETYPES.items():
            macros = by_weight(kcal, weight, gk["pk"], gk["fk"])
            diets.append({
                "diet_id":   f"synth_{kcal}_{archetype}",
                "label":     f"{kcal}kcal / {archetype.replace('_', ' ')}",
                "group":     "synthetic",
                "archetype": archetype,
                "kcal_tier": kcal,
                "ref_weight_kg": weight,
                "protein_g": macros["protein_g"],
                "carbs_g":   macros["carbs_g"],
                "fat_g":     macros["fat_g"],
                "kcal":      macros["kcal"],
            })
    return diets


def main():
    print("Fetching 10 random real diets from daily_macro_target...")
    real = fetch_real_diets()
    for d in real:
        print(f"  {d['label']:<10} diet_type={d['diet_type']!r:<18} goal={d['goal']!r:<10} "
              f"P={d['protein_g']}g C={d['carbs_g']}g F={d['fat_g']}g kcal={d['kcal']}")

    print(f"\nBuilding {len(KCAL_TIERS)} x {len(ARCHETYPES)} = {len(KCAL_TIERS) * len(ARCHETYPES)} synthetic diets...")
    synthetic = build_synthetic_diets()

    population = real + synthetic
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "random_seed": RANDOM_SEED,
            "n_real": len(real),
            "n_synthetic": len(synthetic),
            "n_total": len(population),
            "diets": population,
        }, f, indent=2)

    print(f"\nWrote {len(population)} diets ({len(real)} real + {len(synthetic)} synthetic) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
