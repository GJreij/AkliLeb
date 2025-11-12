# services/order_service.py

from utils.supabase_client import supabase
from datetime import datetime

class OrderService:
    def __init__(self):
        self.supabase = supabase

    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_prefs):
        """
        Main orchestration for confirming a user's meal plan order.
        Steps:
          1. Validate delivery slots
          2. Create/update user delivery preferences
          3. Create deliveries + increment slot counts
          4. Save daily macros
          5. Create meal plan structure
          6. Save payment record
        """

        selected_days = delivery_prefs.get("selected_days", [])
        delivery_slot_id = delivery_prefs.get("delivery_slot_id")

        # --- 1️⃣ Validate inputs ---
        if not user_id or not selected_days or not delivery_slot_id:
            return {"error": "Missing required fields"}, 400

        # --- 2️⃣ Validate delivery slots ---
        full_days = self._check_delivery_slots(selected_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please change your preferences.",
                "full_days": full_days,
            }, 400

        # --- 3️⃣ Handle user delivery preferences ---
        self._upsert_user_delivery_preference(user_id, delivery_prefs)

        # --- 4️⃣ Get user info (address + partner) ---
        user_info = self._fetch_user_info(user_id)
        if not user_info or not user_info.get("delivery_address"):
            return {"error": "User delivery address not found"}, 400

        delivery_address = user_info["delivery_address"]
        partner_id = user_info.get("partner_id")

        # --- 5️⃣ Create deliveries and update slot counts ---
        self._create_deliveries_and_update_slots(user_id, selected_days, delivery_slot_id, delivery_address)

        # --- 6️⃣ Store daily macros + meal plan ---
        self._store_meal_plan_data(user_id, meal_plan)

        # --- 7️⃣ Create payment record ---
        self._create_payment(user_id, partner_id, checkout_summary)

        return {"message": "Order successfully confirmed"}, 200

    # ---------------------------------------
    # Helper functions
    # ---------------------------------------

    def _check_delivery_slots(self, selected_days, delivery_slot_id):
        """Check if selected delivery days still have capacity."""
        full_days = []
        for day in selected_days:
            res = self.supabase.table("delivery_slots_daily").select("*").eq("delivery_date", day).eq("slot_id", delivery_slot_id).execute()
            slot = res.data[0] if res.data else None

            if slot:
                if slot["current_count"] >= slot["max_deliveries"]:
                    full_days.append(day)
            else:
                # Create slot if it doesn't exist
                self.supabase.table("delivery_slots_daily").insert({
                    "delivery_date": day,
                    "slot_id": delivery_slot_id,
                    "current_count": 0,
                    "max_deliveries": 20
                }).execute()
        return full_days

    def _upsert_user_delivery_preference(self, user_id, delivery_prefs):
        """Insert or update user delivery preference."""
        existing = self.supabase.table("user_delivery_preference").select("*").eq("user_id", user_id).execute()
        if existing.data:
            self.supabase.table("user_delivery_preference").update(delivery_prefs).eq("user_id", user_id).execute()
        else:
            self.supabase.table("user_delivery_preference").insert({
                "user_id": user_id,
                **delivery_prefs
            }).execute()

    def _fetch_user_info(self, user_id):
        """Fetch delivery address and partner ID."""
        res = self.supabase.table("user").select("delivery_address, partner_id").eq("id", user_id).execute()
        return res.data[0] if res.data else None

    def _create_deliveries_and_update_slots(self, user_id, selected_days, delivery_slot_id, delivery_address):
        """Create deliveries and increment slot counts for selected days."""
        for day in selected_days:
            # Increment slot count
            self.supabase.table("delivery_slots_daily").update({
                "current_count": self.supabase.rpc("increment_slot_count", {"slot_date": day, "slot_id": delivery_slot_id})
            }).eq("delivery_date", day).eq("slot_id", delivery_slot_id).execute()

            # Create delivery
            self.supabase.table("deliveries").insert({
                "user_id": user_id,
                "delivery_date": day,
                "delivery_address": delivery_address,
                "delivery_slot_id": delivery_slot_id,
                "status": "pending"
            }).execute()

    def _store_meal_plan_data(self, user_id, meal_plan):
        """Store daily macros and meal plan structure."""
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
        """Create payment record."""
        self.supabase.table("payment").insert({
            "ordered_user_id": user_id,
            "partner_at_order": partner_id,
            "amount": checkout_summary["price_breakdown"]["total_price"],
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }).execute()
