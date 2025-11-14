from flask import Blueprint, request, jsonify
from services.ingredients_service import get_ingredients_to_buy

ingredients_bp = Blueprint("ingredients", __name__)


@ingredients_bp.route("/ingredients-to-buy", methods=["GET"])
def ingredients_to_buy():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    recipe = request.args.get("recipe")
    client = request.args.get("client")
    delivery_slot = request.args.get("delivery_slot")
    # Treat "", "null", "None" as no filter
    def normalize(value):
        if value is None:
            return None
        if value.strip() == "" or value.lower() in ["null", "none"]:
            return None
        return value

    recipe = normalize(recipe)
    client = normalize(client)
    delivery_slot = normalize(delivery_slot)

    try:
        result = get_ingredients_to_buy(
            start_date=start_date,
            end_date=end_date,
            recipe=recipe,
            client=client,
            delivery_slot=delivery_slot,
        )
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500
