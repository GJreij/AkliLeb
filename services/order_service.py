# services/order_service.py

from utils.supabase_client import supabase
from datetime import datetime

class OrderService:
    def __init__(self):
        self.supabase = supabase

    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id):
        """
        Main orchestration for confirming a user's meal plan order.
        Steps:
          1. Fetch delivery slot info
          2. Validate capacity and update counts
          3. Upsert user delivery preference
          4. Fetch user info (address, partner)
          5. Create deliveries
          6. Store daily macros and meal plan structure
          7. Create payment record
        """

        # 1️⃣ Fetch slot info
        slot_res = self.supabase.table("delivery_slots").select("*").eq("id", delivery_slot_id).execute()
        slot = slot_res.data[0] if slot_res.data else None
        if not slot:
            return {"error": "Invalid delivery slot"}, 400

        # Extract delivery days from slot (or could come from another table)
        selected_days = slot.get("delivery_days", [])
        if not selected_days:
            return {"error": "This delivery slot has no delivery days defined."}, 400

        # 2️⃣ Validate delivery slots capacity
        full_days = self._check_delivery_slots(selected_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please choose different days.",
                "full_days": full_days,
            }, 400

        # 3️⃣ Upsert user delivery preference
        self._upsert_user_delivery_preference(user_id, slot)

        # 4️⃣ Fetch user info (address, partner)
        user_info = self._fetch_user_info(user_id)
        if not user_info or not user_info.get("delivery_address"):
            return {"error": "User delivery address not found"}, 400

        delivery_address = user_info["delivery_address"]
        partner_id = user_info.get("partner_id")

        # 5️⃣ Create deliveries and update slot counts
        self._create_deliveries_and_update_slots(user_id, selected_days, delivery_slot_id, delivery_address)

        # 6️⃣ Store daily macros and meal plan structure
        self._store_meal_plan_data(user_id, meal_plan)

        # 7️⃣ Create payment record
        self._create_payment(user_id, partner_id, checkout_summary)

        return {"message": "Order successfully confirmed"}, 200

    # ---------------------------------------------------------------------
    # 🔹 HELPER FUNCTIONS
    # ---------------------------------------------------------------------

    def _check_delivery_slots(self, selected_days, delivery_slot_id):
        """Check if selected delivery days still have capacity."""
        full_days = []
        for day in selected_days:
            res = self.supabase.table("delivery_slots_daily") \
                .select("*") \
                .eq("delivery_date", day) \
                .eq("slot_id", delivery_slot_id) \
                .execute()
            slot_day = res.data[0] if res.data else None

            if slot_day:
                if slot_day["current_count"] >= slot_day["max_deliveries"]:
                    full_days.append(day)
            else:
                # Create if not exists
                self.supabase.table("delivery_slots_daily").insert({
                    "delivery_date": day,
                    "slot_id": delivery_slot_id,
                    "current_count": 0,
                    "max_deliveries": 20
                }).execute()

        return full_days

    def _upsert_user_delivery_preference(self, user_id, slot):
        """Insert or update user delivery preference with the selected slot."""
        existing = self.supabase.table("user_delivery_preference") \
            .select("*") \
            .eq("user_id", user_id) \
            .execute()

        slot_id = slot["id"]

        if existing.data:
            pref = existing.data[0]
            if pref["delivery_slot_id"] != slot_id:
                self.supabase.table("user_delivery_preference").update({
                    "delivery_slot_id": slot_id,
                    "last_updated": datetime.utcnow().isoformat()
                }).eq("user_id", user_id).execute()
        else:
            self.supabase.table("user_delivery_preference").insert({
                "user_id": user_id,
                "delivery_slot_id": slot_id,
                "created_at": datetime.utcnow().isoformat()
            }).execute()

    def _fetch_user_info(self, user_id):
        """Fetch user delivery address and partner info."""
        res = self.supabase.table("user").select("delivery_address, partner_id").eq("id", user_id).execute()
        return res.data[0] if res.data else None

    def _create_deliveries_and_update_slots(self, user_id, selected_days, delivery_slot_id, delivery_address):
        """Increment slot counts and create deliveries."""
        for day in selected_days:
            # Increment slot count if exists
            res = self.supabase.table("delivery_slots_daily").select("*").eq("delivery_date", day).eq("slot_id", delivery_slot_id).execute()
            slot_day = res.data[0] if res.data else None

            if slot_day:
                new_count = min(slot_day["current_count"] + 1, slot_day["max_deliveries"])
                self.supabase.table("delivery_slots_daily").update({
                    "current_count": new_count
                }).eq("id", slot_day["id"]).execute()
            else:
                # Create the slot for the day
                self.supabase.table("delivery_slots_daily").insert({
                    "delivery_date": day,
                    "slot_id": delivery_slot_id,
                    "current_count": 1,
                    "max_deliveries": 20
                }).execute()

            # Create delivery record
            self.supabase.table("deliveries").insert({
                "user_id": user_id,
                "delivery_date": day,
                "delivery_address": delivery_address,
                "delivery_slot_id": delivery_slot_id,
                "status": "pending"
            }).execute()

    def _store_meal_plan_data(self, user_id, meal_plan):
        """Save daily macros and full meal plan structure."""
        plan_insert = self.supabase.table("meal_plan").insert({
            "user_id": user_id,
            "start_date": meal_plan["start_date"],
            "end_date": meal_plan["end_date"],
            "status": "confirmed"
        }).execute()

        plan_id = plan_insert.data[0]["id"]

        for day in meal_plan.get("days", []):
            totals = day["totals"]

            # Save daily macros
            self.supabase.table("daily_macro_order").insert({
                "user_id": user_id,
                "date": day["date"],
                "protein_g": totals["protein"],
                "carbs_g": totals["carbs"],
                "fat_g": totals["fat"],
                "kcal": totals["kcal"]
            }).execute()

            # Create meal plan day
            day_insert = self.supabase.table("meal_plan_day").insert({
                "meal_plan_id": plan_id,
                "date": day["date"]
            }).execute()

            day_id = day_insert.data[0]["id"]

            # Add recipes and subrecipes
            for meal in day["meals"]:
                recipe_insert = self.supabase.table("meal_plan_day_recipe").insert({
                    "meal_plan_day_id": day_id,
                    "recipe_id": meal["recipe_id"],
                    "meal_type": meal["meal_type"]
                }).execute()

                recipe_id = recipe_insert.data[0]["id"]

                for sub in meal["subrecipes"]:
                    self.supabase.table("meal_plan_day_recipe_serving").insert({
                        "meal_plan_day_recipe_id": recipe_id,
                        "subrecipe_id": sub["subrecipe_id"],
                        "servings": sub["servings"]
                    }).execute()

    def _create_payment(self, user_id, partner_id, checkout_summary):
        """Insert payment row."""
        total_price = checkout_summary.get("price_breakdown", {}).get("total_price", 0)
        self.supabase.table("payment").insert({
            "ordered_user_id": user_id,
            "partner_at_order": partner_id,
            "amount": total_price,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }).execute()
