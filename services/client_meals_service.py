from utils.supabase_client import supabase

class ClientMealsService:

    def get_upcoming_recipes(self, user_id, from_date, to_date):
        # --------------------------------------------------
        # 1) Fetch meal_plan_days
        # --------------------------------------------------
        mpd_res = (
    supabase.table("meal_plan_day")
    .select("""
        id,
        date,

        meal_plan!inner(
            id,
            user_id
        ),

        daily_macro_order!meal_plan_day_daily_macro_order_id_fkey(
            protein_ordered,
            carbs_ordered,
            fat_ordered,
            kcal_ordered
        ),

        deliveries!meal_plan_day_delivery_id_fkey(
            delivery_date,
            status,
            delivery_slot_id,
            delivery_slots(
                start_time,
                end_time
            )
        )
    """)
    .eq("meal_plan.user_id", user_id)
    .gte("date", from_date)
    .lte("date", to_date)
    .execute()
)



        days = []

        for day in mpd_res.data or []:
            mpd_id = day["id"]

            # --------------------------------------------------
            # 2) Recipes for the day
            # --------------------------------------------------
            recipes_res = (
                supabase.table("meal_plan_day_recipe")
                .select("""
                    recipe_id,
                    meal_type,
                    recipe(name),
                    meal_plan_day_recipe_serving(
                        kcal_calculated,
                        protein_calculated,
                        carbs_calculated,
                        fat_calculated
                    )
                """)
                .eq("meal_plan_day_id", mpd_id)
                .execute()
            )

            recipes_payload = []
            for r in recipes_res.data or []:
                servings = r.get("meal_plan_day_recipe_serving") or []

                totals = {
                    "kcal": sum(s["kcal_calculated"] or 0 for s in servings),
                    "protein": sum(s["protein_calculated"] or 0 for s in servings),
                    "carbs": sum(s["carbs_calculated"] or 0 for s in servings),
                    "fat": sum(s["fat_calculated"] or 0 for s in servings),
                }

                recipes_payload.append({
                    "meal_type": r["meal_type"],
                    "recipe_id": r["recipe_id"],
                    "recipe_name": r["recipe"]["name"],
                    **totals
                })

            # --------------------------------------------------
            # 3) Price (payment)
            # --------------------------------------------------
            payment_res = (
                supabase.table("payment")
                .select("amount")
                .eq("meal_plan_day_id", mpd_id)
                .execute()
            )
            price = payment_res.data[0]["amount"] if payment_res.data else 0

            # --------------------------------------------------
            # 4) Delivery info
            # --------------------------------------------------
            delivery = day.get("deliveries")
            slot = delivery.get("delivery_slots") if delivery else {}

            days.append({
                "date": day["date"],
                "delivery": {
                    "delivery_date": delivery.get("delivery_date") if delivery else None,
                    "delivery_time": (
                        f"{slot.get('start_time')}-{slot.get('end_time')}"
                        if slot else None
                    ),
                    "status": delivery.get("status") if delivery else None
                },
                "totals": {
                    "kcal": day["daily_macro_order"]["kcal_ordered"],
                    "protein": day["daily_macro_order"]["protein_ordered"],
                    "carbs": day["daily_macro_order"]["carbs_ordered"],
                    "fat": day["daily_macro_order"]["fat_ordered"]
                },
                "price": price,
                "recipes": recipes_payload
            })
        has_orders = len(days) > 0


        return {
            "user_id": user_id,
            "from": from_date,
            "to": to_date,
            "has_orders": has_orders,
            "days": days
        }
