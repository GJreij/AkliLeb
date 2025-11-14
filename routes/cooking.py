@cooking_bp.route("/cooking/overview", methods=["POST"])
def cooking_overview():
    data = request.json or {}

    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    def clean(v):
        if v == "" or v == " ":
            return None
        return v

    filters = {
        "client_id":     clean(data.get("client_id")),
        "delivery_slot_id": clean(data.get("delivery_slot_id")),
        "recipe_id":     clean(data.get("recipe_id")),
        "subrecipe_id":  clean(data.get("subrecipe_id")),
        "ingredient_id": clean(data.get("ingredient_id")),
        "status":        clean(data.get("status")),
    }

    result = get_cooking_overview(start_date, end_date, filters)
    return jsonify(result)
