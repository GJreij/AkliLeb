# Define macro splits by diet type (percentages of kcal)
DIET_MACROS = {
    "high_protein": {"protein": 0.3, "carbs": 0.40, "fat": 0.3},
    "balanced": {"protein": 0.2, "carbs": 0.5, "fat": 0.3},
    "low_fat": {"protein": 0.25, "carbs": 0.55, "fat": 0.20},
    "high_carbs": {"protein": 0.20, "carbs": 0.60, "fat": 0.20},
}

# Conversion factors (kcal per g)
KCAL_PER_G = {
    "protein": 4,
    "carbs": 4,
    "fat": 9,
}
