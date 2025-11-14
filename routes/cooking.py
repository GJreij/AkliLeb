from flask import Blueprint, request, jsonify
from services.cooking_service import get_cooking_overview

cooking_bp = Blueprint('cooking', __name__)

@cooking_bp.route("/cooking/overview", methods=["POST"])
def cooking_overview():
    data = request.json or {}

    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    filters = {
        "client_id": data.get("client_id"),
        "delivery_slot_id": data.get("delivery_slot_id"),
        "recipe_id": data.get("recipe_id"),
        "subrecipe_id": data.get("subrecipe_id"),
        "ingredient_id": data.get("ingredient_id"),
        "status": data.get("status"),
    }

    result = get_cooking_overview(start_date, end_date, filters)
    return jsonify(result)
