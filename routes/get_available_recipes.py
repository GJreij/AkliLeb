# routes/available_recipes.py

from flask import Blueprint, request, jsonify
from services.menu_service import MenuService

available_recipes_bp = Blueprint("available_recipes", __name__)
menu_service = MenuService()


@available_recipes_bp.route("/available_recipes_for_date", methods=["POST"])
def available_recipes_for_date():
    """
    Return recipe_ids available for a given date (for meal change UI).

    Required JSON body:
      - date: "YYYY-MM-DD"

    Optional:
      - tenant_id: int
    """
    try:
        payload = request.get_json(silent=True) or {}

        date_str = payload.get("date")
        tenant_id = payload.get("tenant_id")  # optional

        if not date_str:
            return jsonify({
                "error": "Missing required fields",
                "missing_fields": ["date"]
            }), 400

        result, status_code = menu_service.get_available_recipe_ids_for_date(
            date_str=date_str,
            tenant_id=tenant_id,
        )

        return jsonify(result), status_code

    except Exception as e:
        return jsonify({
            "error": "An unexpected error occurred while fetching available recipes.",
            "details": str(e)
        }), 500
