# services/order_service.py

from utils.supabase_client import supabase
from datetime import datetime

DEFAULT_MAX_DELIVERIES = 20

class OrderService:
    def __init__(self):
        self.sb = supabase

    # ---------- PUBLIC ORCHESTRATOR ----------
    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id):
        """
        Flow:
          1) Extract ordered days from meal_plan
          2) Capacity checks & prep delivery_slots_daily rows
          3) Upsert user_delivery_preference
          4) Fetch user delivery address + partner via partner_client_link
          5) Create deliveries FIRST and collect {date: delivery_id}
          6) Create meal_plan + meal_plan_day (status, delivery_id) and update deliveries.meal_plan_day_id
          7) Create payment
        """
        # 1) days from meal_plan
        selected_days = [d["date"] for d in (meal_plan.get("days") or []) if "date" in d]
        if not selected_days:
            return {"error": "No delivery days found in meal plan."}, 400

        # 2) capacity checks
        full_days = self._check_and_prepare_slot_days(selected_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please change your slot.",
                "full_days": full_days,
            }, 400

        # 3) upsert preference
        self._upsert_user_delivery_preference(user_id, delivery_slot_id)

        # 4) user info + partner
        user_info = self._fetch_user_delivery_and_partner(user_id)
        if not user_info or not user_info.get("delivery_address"):
            return {"error": "User delivery address not found."}, 400

        delivery_address = user_info["delivery_address"]
        partner_id = user_info.get("partner_id")

        # 5) create deliveries first -> map
        deliveries_map = self._create_deliveries_and_increment_counts(
            user_id=user_id,
            selected_days=selected_days,
            delivery_slot_id=delivery_slot_id,
            delivery_address=delivery_address,
        )

        # 6) persist meal plan, days, recipes, servings; back-link deliveries
        self._store_meal_plan_bundle(
            user_id=user_id,
            meal_plan=meal_plan,
            deliveries_map=deliveries_map,
        )

        # 7) payment
        self._create_payment_record(
            ordered_user_id=user_id,
            partner_id=partner_id,
            checkout_summary=checkout_summary,
        )

        return {"message": "Order successfully confirmed."}, 200

    # ---------- HELPERS ----------

    def _check_and_prepare_slot_days(self, selected_days, delivery_slot_id):
        """
        Ensure a row exists in delivery_slots_daily for each (slot, date).
        Collect days where current_count >= max_deliveries.
        Create missing rows with current_count=0, max_deliveries=DEFAULT_MAX_DELIVERIES.
        """
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
        """
        delivery_address from user.
        partner via partner_client_link (latest/active row if multiple).
        """
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
            .order("start_date", desc=True)  # latest if multiple
            .limit(1)
            .execute()
        )
        partner_id = partner_res.data[0]["partner_id"] if partner_res.data else None

        return {
            "delivery_address": delivery_address,
            "partner_id": partner_id
        }

    def _create_deliveries_and_increment_counts(self, user_id, selected_days, delivery_slot_id, delivery_address):
        """
        For each ordered day:
          - increment current_count in delivery_slots_daily
          - insert deliveries row
        Return {date: delivery_id}
        """
        deliveries_map = {}

        for day in selected_days:
            # read slot-day row
            res = (
                self.sb.table("delivery_slots_daily")
                .select("id, current_count, max_deliveries")
                .eq("delivery_slot_id", delivery_slot_id)
                .eq("delivery_date", day)
                .execute()
            )
            slot_day = res.data[0] if res.data else None

            if slot_day:
                cur = (slot_day.get("current_count") or 0) + 1
                mx = (slot_day.get("max_deliveries") or DEFAULT_MAX_DELIVERIES)
                # still safe to update (we already passed overbook check earlier)
                if cur > mx:
                    cur = mx  # clamp
                self.sb.table("delivery_slots_daily").update({
                    "current_count": cur,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", slot_day["id"]).execute()
            else:
                # extremely rare because we created missing rows earlier, but handle anyway
                self.sb.table("delivery_slots_daily").insert({
                    "delivery_slot_id": delivery_slot_id,
                    "delivery_date": day,
                    "current_count": 1,
                    "max_deliveries": DEFAULT_MAX_DELIVERIES,
                    "created_at": datetime.utcnow().isoformat()
                }).execute()

            # insert delivery
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

    def _store_meal_plan_bundle(self, user_id, meal_plan, deliveries_map):
        """
        Insert meal_plan, per-day rows (with status + delivery_id),
        update deliveries.meal_plan_day_id, then recipes & subrecipes.
        Assumes you've added:
          - meal_plan_day.status TEXT DEFAULT 'pending'
          - meal_plan_day.delivery_id BIGINT REFERENCES deliveries(id)
          - deliveries.meal_plan_day_id BIGINT REFERENCES meal_plan_day(id)
        """
        # meal_plan (no 'status' column here)
        plan_ins = self.sb.table("meal_plan").insert({
            "user_id": user_id,
            "start_date": meal_plan["start_date"],
            "end_date": meal_plan["end_date"],
            "created_at": datetime.utcnow().isoformat()
        }).execute()
        plan_id = plan_ins.data[0]["id"]

        for day in (meal_plan.get("days") or []):
            date_str = day["date"]
            totals = day.get("totals") or {}
            delivery_id = deliveries_map.get(date_str)

            # 1️⃣ Create meal_plan_day first
            day_ins = self.sb.table("meal_plan_day").insert({
                "meal_plan_id": plan_id,
                "date": date_str,
                "delivery_id": delivery_id,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            meal_plan_day_id = day_ins.data[0]["id"]

            # 2️⃣ Create daily_macro_order (link to meal_plan_day_id + kcal_ordered)
            daily_macro_ins = self.sb.table("daily_macro_order").insert({
                "user_id": user_id,
                "meal_plan_day_id": meal_plan_day_id,
                "for_date": date_str,
                "protein_ordered": totals.get("protein"),
                "carbs_ordered": totals.get("carbs"),
                "fat_ordered": totals.get("fat"),
                "kcal_ordered": totals.get("kcal"),  # NEW FIELD
                "saturated_fat_ordered": totals.get("saturated") if "saturated" in totals else None,
                "fiber_ordered": totals.get("fiber"),
                "sugar_ordered": totals.get("sugar"),
                "created_at": datetime.utcnow().isoformat()
            }).execute()
            daily_macro_order_id = daily_macro_ins.data[0]["id"]

            # 3️⃣ Update meal_plan_day to include daily_macro_order_id
            self.sb.table("meal_plan_day").update({
                "daily_macro_order_id": daily_macro_order_id,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", meal_plan_day_id).execute()

            # 4️⃣ Back-link on deliveries
            if delivery_id:
                self.sb.table("deliveries").update({
                    "meal_plan_day_id": meal_plan_day_id,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", delivery_id).execute()

            # 5️⃣ Recipes + subrecipes
            for meal in (day.get("meals") or []):
                rec_ins = self.sb.table("meal_plan_day_recipe").insert({
                    "meal_plan_day_id": meal_plan_day_id,
                    "recipe_id": meal["recipe_id"],
                    "meal_type": meal.get("meal_type"),
                    "status": "pending",
                    "created_at": datetime.utcnow().isoformat()
                }).execute()
                mpdr_id = rec_ins.data[0]["id"]

                for sub in (meal.get("subrecipes") or []):
                    self.sb.table("meal_plan_day_recipe_serving").insert({
                        "meal_plan_day_recipe_id": mpdr_id,
                        "subrecipe_id": sub["subrecipe_id"],
                        "recipe_subrecipe_serving_calculated": sub.get("servings"),
                        "kcal_calculated": (sub.get("macros") or {}).get("kcal"),
                        "protein_calculated": (sub.get("macros") or {}).get("protein"),
                        "carbs_calculated": (sub.get("macros") or {}).get("carbs"),
                        "fat_calculated": (sub.get("macros") or {}).get("fat"),
                        "status": "pending",
                        "created_at": datetime.utcnow().isoformat()
                    }).execute()


    def _create_payment_record(self, ordered_user_id, partner_id, checkout_summary):
        """
        Create one payment per delivery day, linked to meal_plan_day.
        """
        price_breakdown = checkout_summary.get("price_breakdown") or {}
        daily_breakdown = price_breakdown.get("daily_breakdown") or []

        for day_data in daily_breakdown:
            date_str = day_data.get("date")
            day_total = day_data.get("total_price", 0)

            # find corresponding meal_plan_day_id
            meal_plan_day_res = (
                self.sb.table("meal_plan_day")
                .select("id")
                .eq("date", date_str)  # if you have this column
                .execute()
            )
            meal_plan_day_id = (
                meal_plan_day_res.data[0]["id"]
                if meal_plan_day_res.data else None
            )

            self.sb.table("payment").insert({
                "ordered_user_id": ordered_user_id,
                "partner_at_order": partner_id,
                "amount": day_total,
                "status": "pending",
                "provider": None,
                "provider_payment_id": None,
                "currency": "EUR",
                "meal_plan_day_id": meal_plan_day_id,  # NEW LINK
                "created_at": datetime.utcnow().isoformat()
            }).execute()

