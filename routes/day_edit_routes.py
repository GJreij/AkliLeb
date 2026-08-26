# routes/day_edit_routes.py

import traceback
import logging
from flask import Blueprint, request, jsonify
from services.day_edit_service import DayEditService
from utils.event_logger import log_event

logger = logging.getLogger(__name__)

day_edit_bp = Blueprint("day_edit", __name__)
day_edit_service = DayEditService()


def _parse_payload():
    payload = request.get_json(silent=True) or {}
    user_id = payload.get("user_id")
    meal_plan_day_id = payload.get("meal_plan_day_id")
    changes = payload.get("changes")

    missing = [f for f, v in (
        ("user_id", user_id),
        ("meal_plan_day_id", meal_plan_day_id),
        ("changes", changes),
    ) if not v]
    return payload, user_id, meal_plan_day_id, changes, missing


@day_edit_bp.route("/edit_day/preview", methods=["POST"])
def preview_day_edit():
    try:
        _payload, user_id, meal_plan_day_id, changes, missing = _parse_payload()
        if missing:
            log_event(user_id, "api_error", {"route": "/edit_day/preview", "status_code": 400, "reason": "missing_fields", "missing_fields": missing})
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = day_edit_service.preview(user_id, meal_plan_day_id, changes)

        if status_code != 200:
            log_event(user_id, "api_error", {"route": "/edit_day/preview", "status_code": status_code, "error": result.get("error")})

        return jsonify(result), status_code

    except Exception as e:
        logger.error("preview_day_edit failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/edit_day/preview", "status_code": 500, "error": str(e)})
        return jsonify({"error": "An unexpected error occurred while previewing the edit.", "details": str(e)}), 500


@day_edit_bp.route("/edit_day/confirm", methods=["POST"])
def confirm_day_edit():
    try:
        _payload, user_id, meal_plan_day_id, changes, missing = _parse_payload()
        if missing:
            log_event(user_id, "api_error", {"route": "/edit_day/confirm", "status_code": 400, "reason": "missing_fields", "missing_fields": missing})
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = day_edit_service.confirm(user_id, meal_plan_day_id, changes)

        if status_code == 200:
            log_event(user_id, "day_edited", {
                "meal_plan_day_id": meal_plan_day_id,
                "price_delta": result.get("price_delta"),
            })
        else:
            log_event(user_id, "api_error", {"route": "/edit_day/confirm", "status_code": status_code, "error": result.get("error")})

        return jsonify(result), status_code

    except Exception as e:
        logger.error("confirm_day_edit failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/edit_day/confirm", "status_code": 500, "error": str(e)})
        return jsonify({"error": "An unexpected error occurred while confirming the edit.", "details": str(e)}), 500
