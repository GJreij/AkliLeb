# routes/meal_swap_routes.py

import traceback
import logging
from flask import Blueprint, request, jsonify
from services.meal_swap_service import MealSwapService
from utils.event_logger import log_event

logger = logging.getLogger(__name__)

meal_swap_bp = Blueprint("meal_swap", __name__)
meal_swap_service = MealSwapService()


def _parse_payload():
    payload = request.get_json(silent=True) or {}
    user_id = payload.get("user_id")
    meal_plan_day_id = payload.get("meal_plan_day_id")
    meal_plan_day_recipe_id = payload.get("meal_plan_day_recipe_id")
    new_recipe_id = payload.get("new_recipe_id")

    missing = [f for f, v in (
        ("user_id", user_id),
        ("meal_plan_day_id", meal_plan_day_id),
        ("meal_plan_day_recipe_id", meal_plan_day_recipe_id),
        ("new_recipe_id", new_recipe_id),
    ) if not v]
    return payload, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id, missing


@meal_swap_bp.route("/modify_meal/preview", methods=["POST"])
def preview_meal_swap():
    try:
        _payload, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id, missing = _parse_payload()
        if missing:
            log_event(user_id, "api_error", {"route": "/modify_meal/preview", "status_code": 400, "reason": "missing_fields", "missing_fields": missing})
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = meal_swap_service.preview(
            user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id
        )

        if status_code != 200:
            log_event(user_id, "api_error", {"route": "/modify_meal/preview", "status_code": status_code, "error": result.get("error")})

        return jsonify(result), status_code

    except Exception as e:
        logger.error("preview_meal_swap failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/modify_meal/preview", "status_code": 500, "error": str(e)})
        return jsonify({"error": "An unexpected error occurred while previewing the swap.", "details": str(e)}), 500


@meal_swap_bp.route("/modify_meal/confirm", methods=["POST"])
def confirm_meal_swap():
    try:
        _payload, user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id, missing = _parse_payload()
        mode = _payload.get("mode")
        if not mode:
            missing = missing + ["mode"]
        if missing:
            log_event(user_id, "api_error", {"route": "/modify_meal/confirm", "status_code": 400, "reason": "missing_fields", "missing_fields": missing})
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = meal_swap_service.confirm(
            user_id, meal_plan_day_id, meal_plan_day_recipe_id, new_recipe_id, mode
        )

        if status_code == 200:
            log_event(user_id, "meal_swapped", {
                "meal_plan_day_id": meal_plan_day_id,
                "meal_plan_day_recipe_id": meal_plan_day_recipe_id,
                "new_recipe_id": new_recipe_id,
                "price_delta": result.get("price_delta"),
            })
        else:
            log_event(user_id, "api_error", {"route": "/modify_meal/confirm", "status_code": status_code, "error": result.get("error")})

        return jsonify(result), status_code

    except Exception as e:
        logger.error("confirm_meal_swap failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/modify_meal/confirm", "status_code": 500, "error": str(e)})
        return jsonify({"error": "An unexpected error occurred while confirming the swap.", "details": str(e)}), 500
