# routes/get_available_recipes.py

from flask import Blueprint, request, jsonify
from services.weekly_menu_service import WeeklyMenuService

get_available_recipes_bp = Blueprint("get_available_recipes", __name__)
weekly_menu_service = WeeklyMenuService()


@get_available_recipes_bp.route("/available_recipes_for_date", methods=["POST"])
def available_recipes_for_date():
    """
    Returns recipe_ids available to cook on a given date.

    Required JSON body:
      - date: "YYYY-MM-DD"

    Optional:
      - tenant_id: int
    """
    try:
        payload = request.get_json(silent=True) or {}

        date_str = payload.get("date")
        tenant_id = payload.get("tenant_id")  # optional

        missing = []
        if not date_str:
            missing.append("date")

        if missing:
            return jsonify({
                "error": "Missing required fields",
                "missing_fields": missing
            }), 400

        result, status_code = weekly_menu_service.get_available_recipes_for_date(
            date_str=date_str,
            tenant_id=tenant_id
        )


        return jsonify(result), status_code

    except Exception as e:
        return jsonify({
            "error": "An unexpected error occurred while fetching available recipes.",
            "details": str(e)
        }), 500
