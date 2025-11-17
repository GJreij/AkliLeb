# services/order_service.py
from utils.supabase_client import supabase
from datetime import datetime

DEFAULT_MAX_DELIVERIES = 20

class OrderService:
    def __init__(self):
        self.sb = supabase

    # -------------- PUBLIC ENTRY --------------
    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id):

        selected_days = [d["date"] for d in (meal_plan.get("days") or []) if "date" in d]
        if not selected_days:
            return {"error": "No delivery days found in meal plan."}, 400

        # 1) capacity checks + ensure rows exist
        full_days = self._check_and_prepare_slot_days_bulk(selected_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please change your slot.",
                "full_days": full_days,
            }, 400

        # 2) upsert delivery preference
        self._upsert_user_delivery_preference(user_id, delivery_slot_id)

        # 3) user + partner
        user_info = self._fetch_user_delivery_and_partner(user_id)
        if not user_info or not user_info.get("delivery_address"):
            return {"error": "User delivery address not found."}, 400

        delivery_address = user_info["delivery_address"]
        partner_id = user_info.get("partner_id")

        # 4) create deliveries + increment counts (optimized)
        deliveries_map = self._create_deliveries_bulk(
            user_id,
            selected_days,
            delivery_slot_id,
            delivery_address
        )

        # 5) store meal plan + days + recipes + subrecipes in optimized batches
        self._store_meal_plan_bundle_optimized(
            user_id=user_id,
            meal_plan=meal_plan,
            deliveries_map=deliveries_map,
        )

        # 6) payment
        self._create_payment_record_bulk(
            ordered_user_id=user_id,
            partner_id=partner_id,
            checkout_summary=checkout_summary,
        )

        return {"message": "Order successfully confirmed."}, 200

    # ------------ OPTIMIZED HELPERS ------------

    def _check_and_prepare_slot_days_bulk(self, selected_days, delivery_slot_id):
        """
        Fetch ALL delivery_slots_daily rows at once.
        Insert missing rows in one batch.
        """
        res = (
            self.sb.table("delivery_slots_daily")
            .select("*")
            .eq("delivery_slot_id", delivery_slot_id)
            .in_("delivery_date", selected_days)
            .execute()
        )
        existing = {r["delivery_date"]: r for r in res.data}

        missing_days = [d for d in selected_days if d not in existing]

        # batch insert missing rows
        if missing_days:
            payload = [{
                "delivery_slot_id": delivery_slot_id,
                "delivery_date": d,
                "current_count": 0,
                "max_deliveries": DEFAULT_MAX_DELIVERIES,
                "created_at": datetime.utcnow().isoformat()
            } for d in missing_days]
            self.sb.table("delivery_slots_daily").insert(payload).execute()

        # detect full days
        full = []
        for d, r in existing.items():
            cur = r.get("current_count") or 0
            mx = r.get("max_deliveries") or DEFAULT_MAX_DELIVERIES
            if cur >= mx:
                full.append(d)

        return full

    def _upsert_user_delivery_preference(self, user_id, delivery_slot_id):
        self.sb.table("user_delivery_preference").upsert({
            "user_id": user_id,
            "delivery_slot_id": delivery_slot_id,
            "updated_at": datetime.utcnow().isoformat()
        }).execute()

    def _fetch_user_delivery_and_partner(self, user_id):

        user_res = (
            self.sb.table("user")
            .select("delivery_address")
            .eq("id", user_id)
            .execute()
        )
        if not user_res.data:
            return None

        delivery_address = user_res.data[0].get("delivery_address")

        partner_res = (
            self.sb.table("partner_client_link")
            .select("partner_id, start_date")
            .eq("client_id", user_id)
            .order("start_date", desc=True)
            .limit(1)
            .execute()
        )
        partner_id = partner_res.data[0]["partner_id"] if partner_res.data else None

        return {"delivery_address": delivery_address, "partner_id": partner_id}

    def _create_deliveries_bulk(self, user_id, selected_days, delivery_slot_id, delivery_address):
        """
        Much faster: batch update slot counters + batch insert deliveries.
        """
        # fetch counts
        rows = (
            self.sb.table("delivery_slots_daily")
            .select("id, delivery_date, current_count, max_deliveries")
            .eq("delivery_slot_id", delivery_slot_id)
            .in_("delivery_date", selected_days)
            .execute()
        )
        slot_map = {r["delivery_date"]: r for r in rows.data}

        # batch update slot counts
        updates = []
        for d in selected_days:
            r = slot_map[d]
            new_count = min((r["current_count"] or 0) + 1, r.get("max_deliveries") or DEFAULT_MAX_DELIVERIES)
            updates.append({
                "id": r["id"],
                "current_count": new_count,
                "updated_at": datetime.utcnow().isoformat()
            })

        # Upsert current_count efficiently
        for u in updates:
            self.sb.table("delivery_slots_daily").update(u).eq("id", u["id"]).execute()

        # batch insert deliveries
        payload = [{
            "user_id": user_id,
            "delivery_date": d,
            "delivery_slot_id": delivery_slot_id,
            "delivery_address": delivery_address,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        } for d in selected_days]

        inserted = self.sb.table("deliveries").insert(payload).execute()

        return {row["delivery_date"]: row["id"] for row in inserted.data}

    def _store_meal_plan_bundle_optimized(self, user_id, meal_plan, deliveries_map):

        # insert meal_plan
        plan_ins = self.sb.table("meal_plan").insert({
            "user_id": user_id,
            "start_date": meal_plan["start_date"],
            "end_date": meal_plan["end_date"],
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        plan_id = plan_ins.data[0]["id"]

        all_day_payload = []
        recipe_payload = []
        subrecipe_payload = []
        macro_payload = []

        # ------- Build ALL payloads in memory -------
        for day in (meal_plan.get("days") or []):
            date_str = day["date"]
            delivery_id = deliveries_map.get(date_str)
            totals = day.get("totals") or {}

            # meal_plan_day
            mpd = {
                "meal_plan_id": plan_id,
                "date": date_str,
                "delivery_id": delivery_id,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }
            all_day_payload.append(mpd)

        # Insert all meal_plan_day at once
        mpd_inserted = self.sb.table("meal_plan_day").insert(all_day_payload).execute()
        mpd_map = {row["date"]: row["id"] for row in mpd_inserted.data}

        # Now build macros and recipes
        for day in (meal_plan.get("days") or []):
            date_str = day["date"]
            mpd_id = mpd_map[date_str]
            totals = day.get("totals") or {}

            # daily_macro_order
            macro_payload.append({
                "user_id": user_id,
                "meal_plan_day_id": mpd_id,
                "for_date": date_str,
                "protein_ordered": totals.get("protein"),
                "carbs_ordered": totals.get("carbs"),
                "fat_ordered": totals.get("fat"),
                "kcal_ordered": totals.get("kcal"),
                "saturated_fat_ordered": totals.get("saturated"),
                "fiber_ordered": totals.get("fiber"),
                "sugar_ordered": totals.get("sugar"),
                "created_at": datetime.utcnow().isoformat()
            })

            for meal in (day.get("meals") or []):
                recipe_payload.append({
                    "meal_plan_day_id": mpd_id,
                    "recipe_id": meal["recipe_id"],
                    "meal_type": meal.get("meal_type"),
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat()
                })

        # Insert all macros
        macro_inserted = self.sb.table("daily_macro_order").insert(macro_payload).execute()

        # Insert recipes
        recipes_inserted = self.sb.table("meal_plan_day_recipe").insert(recipe_payload).execute()

        # Build subrecipes
        for inserted_row, day in zip(recipes_inserted.data, (meal_plan.get("days") or [])):
            mpdr_id = inserted_row["id"]
            for meal in (day.get("meals") or []):
                for sub in (meal.get("subrecipes") or []):
                    subrecipe_payload.append({
                        "meal_plan_day_recipe_id": mpdr_id,
                        "subrecipe_id": sub["subrecipe_id"],
                        "recipe_subrecipe_serving_calculated": sub.get("servings"),
                        "kcal_calculated": (sub.get("macros") or {}).get("kcal"),
                        "protein_calculated": (sub.get("macros") or {}).get("protein"),
                        "carbs_calculated": (sub.get("macros") or {}).get("carbs"),
                        "fat_calculated": (sub.get("macros") or {}).get("fat"),
                        "cooking_status": "pending",
                        "portioning_status": "pending",
                        "created_at": datetime.utcnow().isoformat()
                    })

        if subrecipe_payload:
            self.sb.table("meal_plan_day_recipe_serving").insert(subrecipe_payload).execute()

    def _create_payment_record_bulk(self, ordered_user_id, partner_id, checkout_summary):
        price_breakdown = checkout_summary.get("price_breakdown") or {}
        daily_breakdown = price_breakdown.get("daily_breakdown") or []

        payload = []
        for day_data in daily_breakdown:
            payload.append({
                "ordered_user_id": ordered_user_id,
                "partner_at_order": partner_id,
                "amount": day_data.get("total_price", 0),
                "status": "pending",
                "provider": None,
                "provider_payment_id": None,
                "currency": "EUR",
                "meal_plan_day_id": None,
                "created_at": datetime.utcnow().isoformat()
            })

        if payload:
            self.sb.table("payment").insert(payload).execute()
