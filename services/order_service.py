# services/order_service.py

from utils.supabase_client import supabase
from datetime import datetime

class OrderService:
    def __init__(self):
        self.supabase = supabase

    def confirm_order(self, user_id, meal_plan, checkout_summary, delivery_slot_id):
        """
        1. Validate delivery slot capacity per day
        2. Upsert user delivery preference
        3. Create deliveries for each ordered day
        4. Save macros + meal plan
        5. Create payment record
        """

        # 1️⃣ Extract ordered days directly from meal_plan
        selected_days = [d["date"] for d in meal_plan.get("days", [])]
        if not selected_days:
            return {"error": "No delivery days found in meal plan."}, 400

        # 2️⃣ Check capacity in delivery_slots_daily
        full_days = self._check_slot_capacity(selected_days, delivery_slot_id)
        if len(full_days) > 2:
            return {
                "error": "Too many selected delivery days are fully booked. Please change your slot.",
                "full_days": full_days,
            }, 400

        # 3️⃣ Upsert user preference
        self._upsert_user_preference(user_id, delivery_slot_id)

        # 4️⃣ Fetch user info (address, partner)
        user_info = self._fetch_user_info(user_id)
        if not user_info or not user_info.get("delivery_address"):
            return {"error": "User delivery address not found."}, 400

        delivery_address = user_info["delivery_address"]
        partner_id = user_info.get("partner_id")

        # 5️⃣ Create deliveries + update counts
        self._create_deliveries(user_id, selected_days, delivery_slot_id, delivery_address)

        # 6️⃣ Store daily macros + meal plan
        self._store_meal_plan_data(user_id, meal_plan)

        # 7️⃣ Create payment record
        self._create_payment(user_id, partner_id, checkout_summary)

        return {"message": "Order successfully confirmed."}, 200

    # -----------------------------------------------------------------
    # Helper functions
    # -----------------------------------------------------------------

    def _check_slot_capacity(self, selected_days, delivery_slot_id):
        """Check and prepare daily slots."""
        full_days = []
        for day in selected_days:
            res = (
                self.supabase.table("delivery_slots_daily")
                .select("*")
                .eq("delivery_slot_id", delivery_slot_id)
                .eq("delivery_date", day)
                .execute()
            )
            slot_day = res.data[0] if res.data else None

            if slot_day:
                if slot_day["current_count"] >= slot_day["max_deliveries"]:
                    full_days.append(day)
            else:
                # Create the slot record for that date
                self.supabase.table("delivery_slots_daily").insert({
                    "delivery_slot_id": delivery_slot_id,
                    "delivery_date": day,
                    "current_count": 0,
                    "max_deliveries": 20
                }).execute()

        return full_days

    def _upsert_user_preference(self, user_id, delivery_slot_id):
        """Insert or update user preference based on slot."""
        res = (
            self.supabase.table("user_delivery_preference")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )

        if res.data:
            pref = res.data[0]
            if pref["delivery_slot_id"] != delivery_slot_id:
                self.supabase.table("user_delivery_preference").update({
                    "delivery_slot_id": delivery_slot_id,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("user_id", user_id).execute()
        else:
            self.supabase.table("user_delivery_preference").insert({
                "user_id": user_id,
                "delivery_slot_id": delivery_slot_id,
                "created_at": datetime.utcnow().isoformat()
            }).execute()

    def _fetch_user_info(self, user_id):
        """Get address and partner info."""
        res = (
            self.supabase.table("user")
            .select("delivery_address, partner_id")
            .eq("id", user_id)
            .execute()
        )
        return res.data[0] if res.data else None

    def _create_deliveries(self, user_id, selected_days, delivery_slot_id, delivery_address):
        """Create deliveries for each ordered day and increment slot count."""
        for day in selected_days:
            # Fetch daily slot record
            res = (
                self.supabase.table("delivery_slots_daily")
                .select("*")
                .eq("delivery_slot_id", delivery_slot_id)
                .eq("delivery_date", day)
                .execute()
            )
            slot_day = res.data[0] if res.data else None

            if slot_day:
                new_count = (slot_day["current_count"] or 0) + 1
                self.supabase.table("delivery_slots_daily").update({
                    "current_count": new_count,
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", slot_day["id"]).execute()

            # Create delivery record
            self.supabase.table("deliveries").insert({
                "user_id": user_id,
                "delivery_date": day,
                "delivery_slot_id": delivery_slot_id,
                "delivery_address": delivery_address,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat()
            }).execute()

    def _store_meal_plan_data(self, user_id, meal_plan):
        """Save daily macros and plan structure."""
        plan_insert = self.supabase.table("meal_plan").insert({
            "user_id": user_id,
            "start_date": meal_plan["start_date"],
            "end_date": meal_plan["end_date"],
            "status": "confirmed"
        }).execute()

        plan_id = plan_insert.data[0]["id"]

        for day in meal_plan.get("days", []):
            totals = day["totals"]

            # Save macros
            self.supabase.table("daily_macro_order").insert({
                "user_id": user_id,
                "date": day["date"],
                "protein_g": totals["protein"],
                "carbs_g": totals["carbs"],
                "fat_g": totals["fat"],
                "kcal": totals["kcal"]
            }).execute()

            # Create day
            day_insert = self.supabase.table("meal_plan_day").insert({
                "meal_plan_id": plan_id,
                "date": day["date"]
            }).execute()

            day_id = day_insert.data[0]["id"]

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
        """Insert payment record."""
        total_price = checkout_summary.get("price_breakdown", {}).get("total_price", 0)
        self.supabase.table("payment").insert({
            "ordered_user_id": user_id,
            "partner_at_order": partner_id,
            "amount": total_price,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat()
        }).execute()
