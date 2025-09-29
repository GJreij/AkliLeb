from flask import Flask, request, jsonify

app = Flask(__name__)

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

@app.route("/macros", methods=["GET"])
def get_macros():
    kcal = request.args.get("kcal", type=float)
    diet_type = request.args.get("diet", "").lower()

    if not kcal or kcal <= 0:
        return jsonify({"error": "Please provide a positive kcal value"}), 400
    if diet_type not in DIET_MACROS:
        return jsonify({"error": f"Diet type must be one of {list(DIET_MACROS.keys())}"}), 400

    macros_pct = DIET_MACROS[diet_type]
    macros_grams = {
        macro: round((kcal * pct) / KCAL_PER_G[macro], 1)
        for macro, pct in macros_pct.items()
    }

    return jsonify({
        "diet_type": diet_type,
        "kcal": kcal,
        "macros_percentage": {m: int(pct * 100) for m, pct in macros_pct.items()},
        "macros_grams": macros_grams,
    })

@app.route("/")
def home():
    return "Hello from Flask API on Heroku!"

if __name__ == "__main__":
    app.run(debug=True)
