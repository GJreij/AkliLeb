# routes/cancellation_routes.py

import os
import traceback
import logging
from flask import Blueprint, request, jsonify
from services.cancellation_service import CancellationService
from utils.event_logger import log_event

logger = logging.getLogger(__name__)

cancellation_bp = Blueprint("cancellation", __name__)
cancellation_service = CancellationService()

INTERNAL_ADMIN_SECRET = os.getenv("INTERNAL_ADMIN_SECRET", "")


@cancellation_bp.route("/request_cancellation", methods=["POST"])
def request_cancellation():
    try:
        payload = request.get_json(silent=True) or {}
        user_id = payload.get("user_id")
        meal_plan_id = payload.get("meal_plan_id")
        meal_plan_day_ids = payload.get("meal_plan_day_ids")  # optional: specific days only

        missing = [f for f, v in (("user_id", user_id), ("meal_plan_id", meal_plan_id)) if not v]
        if missing:
            log_event(user_id, "api_error", {"route": "/request_cancellation", "status_code": 400, "reason": "missing_fields", "missing_fields": missing})
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = cancellation_service.request_cancellation(user_id, meal_plan_id, meal_plan_day_ids)

        if status_code == 200:
            log_event(user_id, "cancellation_requested", {"meal_plan_id": meal_plan_id})
        else:
            log_event(user_id, "api_error", {"route": "/request_cancellation", "status_code": status_code, "error": result.get("error")})

        return jsonify(result), status_code

    except Exception as e:
        logger.error("request_cancellation failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/request_cancellation", "status_code": 500, "error": str(e)})
        return jsonify({"error": "An unexpected error occurred while requesting cancellation.", "details": str(e)}), 500


@cancellation_bp.route("/preview_cancellation_discount_impact", methods=["POST"])
def preview_cancellation_discount_impact():
    try:
        payload = request.get_json(silent=True) or {}
        user_id = payload.get("user_id")
        meal_plan_id = payload.get("meal_plan_id")
        meal_plan_day_ids = payload.get("meal_plan_day_ids")

        missing = [f for f, v in (
            ("user_id", user_id), ("meal_plan_id", meal_plan_id), ("meal_plan_day_ids", meal_plan_day_ids),
        ) if not v]
        if missing:
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = cancellation_service.preview_discount_impact(user_id, meal_plan_id, meal_plan_day_ids)
        return jsonify(result), status_code

    except Exception as e:
        logger.error("preview_cancellation_discount_impact failed: %s\n%s", str(e), traceback.format_exc())
        return jsonify({"error": "An unexpected error occurred while checking this cancellation.", "details": str(e)}), 500


@cancellation_bp.route("/admin/decide_cancellation", methods=["POST"])
def decide_cancellation():
    # This is a privileged endpoint. Flask has no general request auth today,
    # so this shared secret (set on both the Next.js server and here) is the
    # only thing standing between "any request" and "an admin decision" —
    # scoped to just this one new endpoint, not a fix for Flask auth broadly.
    provided_secret = request.headers.get("X-Internal-Admin-Secret", "")
    if not INTERNAL_ADMIN_SECRET or provided_secret != INTERNAL_ADMIN_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        payload = request.get_json(silent=True) or {}
        cancellation_request_id = payload.get("cancellation_request_id")
        decision = payload.get("decision")
        decided_by = payload.get("decided_by")
        note = payload.get("note")
        refund_amount = payload.get("refund_amount")

        missing = [f for f, v in (
            ("cancellation_request_id", cancellation_request_id),
            ("decision", decision),
            ("decided_by", decided_by),
        ) if not v]
        if missing:
            return jsonify({"error": "Missing required fields", "missing_fields": missing}), 400

        result, status_code = cancellation_service.decide(
            cancellation_request_id=cancellation_request_id,
            decision=decision,
            decided_by=decided_by,
            note=note,
            refund_amount=refund_amount,
        )

        log_event(decided_by, "cancellation_decided", {
            "cancellation_request_id": cancellation_request_id,
            "decision": decision,
            "status_code": status_code,
        })

        return jsonify(result), status_code

    except Exception as e:
        logger.error("decide_cancellation failed: %s\n%s", str(e), traceback.format_exc())
        log_event(None, "api_error", {"route": "/admin/decide_cancellation", "status_code": 500, "error": str(e)})
        return jsonify({"error": "An unexpected error occurred while deciding the cancellation.", "details": str(e)}), 500
