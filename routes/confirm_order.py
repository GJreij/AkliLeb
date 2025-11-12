# routes/confirm_order.py

from flask import Blueprint, request, jsonify
from services.order_service import OrderService

confirm_order_bp = Blueprint("confirm_order", __name__)
order_service = OrderService()

@confirm_order_bp.route("/confirm_order", methods=["POST"])
def confirm_order():
    try:
        data = request.get_json()
        user_id = data.get("user_id")
        meal_plan = data.get("meal_plan", {})
        checkout_summary = data.get("checkout_summary", {})
        delivery_prefs = data.get("user_delivery_preferences", {})

        result, status = order_service.confirm_order(
            user_id, meal_plan, checkout_summary, delivery_prefs
        )
        return jsonify(result), status

    except Exception as e:
        return jsonify({"error": str(e)}), 500
