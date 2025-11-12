from flask import Blueprint, request, jsonify
from services.partner_service import PartnerService

partner_bp = Blueprint("partner", __name__)
partner_service = PartnerService()

@partner_bp.route("/partner_shares", methods=["GET"])
def get_partner_shares():
    partner_id = request.args.get("partner_id")
    this_month = request.args.get("this_month", "false").lower() == "true"

    if not partner_id:
        return jsonify({"error": "Missing partner_id"}), 400

    result = partner_service.get_partner_shares(partner_id, this_month)
    return jsonify(result), 200
