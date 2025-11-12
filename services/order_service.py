# services/order_service.py

from utils.supabase_client import supabase
from datetime import datetime

DEFAULT_MAX_DELIVERIES = 20

class OrderService:
    def __init__(self):
        self.sb = supabase

    # ---------------------------------------------------------------------
    # MAIN FLOW
    # ---------------------------------------------------------------------
    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id):
        """
        Confirm an order:
          1. Extract days from meal_plan
          2. Check slot capacity
          3. Upsert user preference
          4. Fetch user delivery address + partner
          5. Create deliveries (returns map {date: delivery_id})
          6. Create meal_plan + days + recipes + payments per day
        """
        selected_days = [d["date"] for d in (meal_plan.get("days") or []) if "date" in d]
        if not selected_days:
            return {"error": "No delivery days found in meal plan."}, 400

        # 2️⃣ Check capacity
        full_days = self._check_and_prepare_slot_days(selected_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please change your slot.",
                "full_days": full_days,
            }, 400

        # 3️⃣ Upsert user preference
        self._upsert_user_delivery_preference(user_id, delivery_slot_id)

        # 4️⃣ Get delivery address + partner
        user_info = self._fetch_user_delivery_and_partner(user_id)
        if not user_info or not user_info.get("delivery_address"):
            return {"error": "User delivery address not found."}, 400

        delivery_address = user_info["delivery_address"]
        partner_id = user_info.get("partner_id")

        # 5️⃣ Create deliveries first
        deliveries_map = self._create_deliveries_and_increment_counts(
            user_id=user_id,
            selected_days=selected_days,
            delivery_slot_id=delivery_slot_id,
            delivery_address=delivery_address,
        )

        # 6️⃣ Store plan + days + per-day payments
        self._store_meal_plan_bundle(
            user_id=user_id,
            meal_plan=meal_plan,
            deliveries_map=deliveries_map,
            checkout_summary=checkout_summary,
            partner_id=partner_id,
        )

        return {"message": "Order successfully confirmed."}, 200

    # ---------------------------------------------------------------------
    # HELPERS
    # ---------------------------------------------------------------------
    def _check_and_prepare_slot_days(self, selected_days, delivery_slot_id):
        """Ensure delivery_slots_daily rows exist; return list of full days."""
        full_days = []

        for day in selected_days:
            res = (
                self.sb.table("delivery_slots_daily")
                .select("*")
                .eq("delivery_slot_id", delivery_slot_id)
                .eq("delivery_date", day)
                .execute()
            )
            row = res.data[0] if res.data else None

            if row:
                cur = (row.get("current_count") or 0)
                mx = (row.get("max_deliveries") or DEFAULT_MAX_DELIVERIES)
                if cur >= mx:
                    full_days.append(day)
            else:
                self.sb.table("delivery_slots_daily").insert({
                    "delivery_slot_id": delivery_slot_id,
                    "delivery_date": day,
                    "current_count": 0,
                    "max_deliveries": DEFAULT_MAX_DELIVERIES,
                    "created_at": datetime.utcnow().isoformat()
                }).execute()

        return full_days

    def _upsert_user_delivery_preference(self, user_id, delivery_slot_id):
        """Insert or update the user's preferred delivery slot."""
        res = (
            self.sb.table("user_delivery_preference")
            .select("id, delivery_slot_id")
            .eq("user_id", user_id)
            .execute()
        )

        if res.data:
            pref = res.data[0]
            if pref.get("delivery_slot_id") != delivery_slot_id:
                self.sb.table("user_delivery_preference").update({
                    "delivery_slot_id": delivery_slot_id,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", pref["id"]).execute()
        else:
            self.sb.table("user_delivery_preference").insert({
                "user_id": user_id,
                "delivery_slot_id": delivery_slot_id,
                "created_at": datetime.utcnow().isoformat()
            }).execute()

    def _fetch_user_delivery_and_partner(self, user_id):
        """Fetch delivery_address from user + partner from partner_client_link."""
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

    def _create_deliveries_and_increment_counts(self, user_id, selected_days, delivery_slot_id, delivery_address):
        """Create deliveries and return {date: delivery_id}."""
        deliveries_map = {}

        for day in selected_days:
            # fetch slot daily record
            res = (
                self.sb.table("delivery_slots_daily")
                .select("id, current_count, max_deliveries")
                .eq("delivery_slot_id", delivery_slot_id)
                .eq("delivery_date", day)
                .execute()
            )
            slot_day = res.data[0] if res.data else None

            if slot_day:
                new_count = (slot_day.get("current_count") or 0) + 1
                max_deliv = slot_day.get("max_deliveries") or DEFAULT_MAX_DELIVERIES
                if new_count > max_deliv:
                    new_count = max_deliv
                self.sb.table("delivery_slots_daily").update({
                    "current_count": new_count,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", slot_day["id"]).execute()
            else:
                self.sb.table("delivery_slots_daily").insert({
                    "delivery_slot_id": delivery_slot_id,
                    "delivery_date": day,
                    "current_count": 1,
                    "max_deliveries": DEFAULT_MAX_DELIVERIES,
                    "created_at": datetime.utcnow().isoformat()
                }).execute()

            # create delivery record
            delivery_ins = self.sb.table("deliveries").insert({
                "user_id": user_id,
                "delivery_date": day,
                "delivery_slot_id": delivery_slot_id,
                "delivery_address": delivery_address,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            delivery_id = delivery_ins.data[0]["id"]
            deliveries_map[day] = delivery_id

        return deliveries_map

    # Helper: extract daily price map
    def _daily_price_map_from_summary(self, checkout_summary):
        daily = ((checkout_summary or {}).get("price_breakdown") or {}).get("daily_breakdown") or []
        return {row["date"]: row.get("total_price", 0) for row in daily if "date" in row}

    def _store_meal_plan_bundle(self, user_id, meal_plan, deliveries_map, checkout_summary, partner_id):
        """Insert meal_plan, days, recipes, and one payment per day."""
        plan_ins = self.sb.table("meal_plan").insert({
            "user_id": user_id,
            "start_date": meal_plan["start_date"],
            "end_date": meal_plan["end_date"],
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        plan_id = plan_ins.data[0]["id"]

        daily_prices = self._daily_price_map_from_summary(checkout_summary)

        for day in (meal_plan.get("days") or []):
            date_str = day["date"]
            totals = day.get("totals") or {}
            delivery_id = deliveries_map.get(date_str)

            # daily macros
            self.sb.table("daily_macro_order").insert({
                "user_id": user_id,
                "for_date": date_str,
                "protein_g": totals.get("protein"),
                "carbs_g": totals.get("carbs"),
                "fat_g": totals.get("fat"),
                "kcal": totals.get("kcal"),
                "created_at": datetime.utcnow().isoformat()
            }).execute()

            # create meal_plan_day
            day_ins = self.sb.table("meal_plan_day").insert({
                "meal_plan_id": plan_id,
                "date": date_str,
                "delivery_id": delivery_id,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            meal_plan_day_id = day_ins.data[0]["id"]

            # back link deliveries
            if delivery_id:
                self.sb.table("deliveries").update({
                    "meal_plan_day_id": meal_plan_day_id,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", delivery_id).execute()

            # create payment for this day
            amount = daily_prices.get(date_str, 0)
            self.sb.table("payment").insert({
                "ordered_user_id": user_id,
                "partner_at_order": partner_id,
                "meal_plan_id": plan_id,
                "meal_plan_day_id": meal_plan_day_id,
                "amount": amount,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()

            # recipes + subrecipes
            for meal in (day.get("meals") or []):
                rec_ins = self.sb.table("meal_plan_day_recipe").insert({
                    "meal_plan_day_id": meal_plan_day_id,
                    "recipe_id": meal["recipe_id"],
                    "meal_type": meal.get("meal_type"),
                    "created_at": datetime.utcnow().isoformat()
                }).execute()
                recipe_id = rec_ins.data[0]["id"]

                for sub in (meal.get("subrecipes") or []):
                    self.sb.table("meal_plan_day_recipe_serving").insert({
                        "meal_plan_day_recipe_id": recipe_id,
                        "subrecipe_id": sub["subrecipe_id"],
                        "recipe_subrecipe_serving_calculated": sub.get("servings"),
                        "kcal_calculated": (sub.get("macros") or {}).get("kcal"),
                        "protein_calculated": (sub.get("macros") or {}).get("protein"),
                        "carbs_calculated": (sub.get("macros") or {}).get("carbs"),
                        "fat_calculated": (sub.get("macros") or {}).get("fat"),
                        "created_at": datetime.utcnow().isoformat()
                    }).execute()
