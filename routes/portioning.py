# routes/portioning.py

from flask import Blueprint, request, jsonify
from services.portioning_service import (
    get_portioning_view_for_serving_ids,
    get_portioning_view_by_filters,
)



portioning_bp = Blueprint("portioning", __name__)


@portioning_bp.route("/portioning/by-ids", methods=["POST"])
def portioning_by_ids():
    data = request.get_json(silent=True) or {}
    serving_ids = data.get("serving_ids", [])

    if not serving_ids:
        return jsonify({"error": "serving_ids is required"}), 400

    try:
        result = get_portioning_view_for_serving_ids(serving_ids)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@portioning_bp.route("/portioning/by-filters", methods=["POST"])
def portioning_by_filters():
    data = request.get_json(silent=True) or {}

    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    try:
        result = get_portioning_view_by_filters(
            start_date=start_date,
            end_date=end_date,
            recipe_id=data.get("recipe_id"),
            delivery_slot_id=data.get("delivery_slot_id"),
            subrecipe_id=data.get("subrecipe_id"),
            cooking_status=data.get("cooking_status"),
            portioning_status=data.get("portioning_status"),
        )
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
