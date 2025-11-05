# Define macro splits by diet type (percentages of kcal)
DIET_MACROS = {
    "high_protein": {"protein": 0.35, "carbs": 0.40, "fat": 0.25},
    "balanced": {"protein": 0.30, "carbs": 0.45, "fat": 0.25},
    "low_fat": {"protein": 0.25, "carbs": 0.55, "fat": 0.20},
    "high_carbs": {"protein": 0.20, "carbs": 0.60, "fat": 0.20},
}

# Conversion factors (kcal per g)
KCAL_PER_G = {
    "protein": 4,
    "carbs": 4,
    "fat": 9,
}
