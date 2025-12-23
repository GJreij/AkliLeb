from flask import Blueprint, request, jsonify
from config.constants import DIET_MACROS, KCAL_PER_G, MACRO_RANGES

macros_bp = Blueprint("macros", __name__)

@macros_bp.route("/macros", methods=["GET"])
def get_macros():
    kcal = request.args.get("kcal", type=float)
    diet_type = request.args.get("diet", "").lower()

    if not kcal or kcal <= 0:
        return jsonify({"error": "Please provide a positive kcal value"}), 400
    if diet_type not in DIET_MACROS:
        return jsonify(
            {"error": f"Diet type must be one of {list(DIET_MACROS.keys())}"}
        ), 400

    macros_pct = DIET_MACROS[diet_type]
    macros_grams = {
        macro: round((kcal * pct) / KCAL_PER_G[macro], 1)
        for macro, pct in macros_pct.items()
    }

    return jsonify(
        {
            "diet_type": diet_type,
            "kcal": kcal,
            "macros_percentage": {
                m: int(pct * 100) for m, pct in macros_pct.items()
            },
            "macros_grams": macros_grams,
        }
    )

def parse_float(value, field_name):
    """
    Safely parse float and return clear error messages
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a number. "
            "Use a dot (.) for decimals, not a comma (,)."
        )

    if value <= 0:
        raise ValueError(f"{field_name} must be greater than 0.")

    return value


@macros_bp.route("/macros/from-grams", methods=["POST"])
def macros_from_grams():
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    try:
        protein_g = parse_float(data.get("protein"), "Protein (g)")
        carbs_g = parse_float(data.get("carbs"), "Carbohydrates (g)")
        fat_g = parse_float(data.get("fat"), "Fat (g)")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # --- kcal calculation ---
    kcal_protein = protein_g * KCAL_PER_G["protein"]
    kcal_carbs = carbs_g * KCAL_PER_G["carbs"]
    kcal_fat = fat_g * KCAL_PER_G["fat"]

    total_kcal = kcal_protein + kcal_carbs + kcal_fat

    if total_kcal <= 0:
        return jsonify({"error": "Total calories must be greater than 0"}), 400

    # --- percentages ---
    pct_protein = kcal_protein / total_kcal
    pct_carbs = kcal_carbs / total_kcal
    pct_fat = kcal_fat / total_kcal

    # --- sanity checks (macro balance) ---
    errors = []

    for macro, pct in {
        "protein": pct_protein,
        "carbs": pct_carbs,
        "fat": pct_fat,
    }.items():
        min_pct, max_pct = MACRO_RANGES[macro]
        if not (min_pct <= pct <= max_pct):
            errors.append(
                f"{macro.capitalize()} percentage ({int(pct*100)}%) "
                f"is outside the recommended range "
                f"({int(min_pct*100)}–{int(max_pct*100)}%)."
            )

    if errors:
        return jsonify(
            {
                "error": "Macro distribution is unrealistic.",
                "details": errors,
            }
        ), 400

    # --- success response ---
    return jsonify(
        {
            "total_kcal": round(total_kcal),
            "macros_grams": {
                "protein": protein_g,
                "carbs": carbs_g,
                "fat": fat_g,
            },
            "macros_percentage": {
                "protein": round(pct_protein * 100, 1),
                "carbs": round(pct_carbs * 100, 1),
                "fat": round(pct_fat * 100, 1),
            },
            "kcal_breakdown": {
                "protein": round(kcal_protein),
                "carbs": round(kcal_carbs),
                "fat": round(kcal_fat),
            },
        }
    ), 200
