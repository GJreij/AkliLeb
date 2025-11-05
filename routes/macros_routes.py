from flask import Blueprint, request, jsonify
from config.constants import DIET_MACROS, KCAL_PER_G

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
