# routes/confirm_order.py

from flask import Blueprint, request, jsonify
from services.order_service import OrderService

confirm_order_bp = Blueprint("confirm_order", __name__)
order_service = OrderService()

@confirm_order_bp.route("/confirm_order", methods=["POST"])
def confirm_order():
    try:
        data = request.get_json() or {}

        user_id = data.get("user_id")
        meal_plan = data.get("meal_plan") or {}
        checkout_summary = data.get("checkout_summary") or {}
        delivery_slot_id = data.get("delivery_slot_id")

        # basic validation
        if not user_id or not meal_plan or not checkout_summary or not delivery_slot_id:
            return jsonify({"error": "Missing required fields"}), 400

        result, status = order_service.confirm_order(
            user_id=user_id,
            meal_plan=meal_plan,
            checkout_summary=checkout_summary,
            delivery_slot_id=delivery_slot_id,
        )
        return jsonify(result), status

    except Exception as e:
        return jsonify({"error": str(e)}), 500
