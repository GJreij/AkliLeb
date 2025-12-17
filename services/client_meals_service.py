from utils.supabase_client import supabase


def _round(value):
    return int(round(value or 0))


class ClientMealsService:

    def get_upcoming_recipes(self, user_id, from_date, to_date):
        # --------------------------------------------------
        # Single optimized query
        # --------------------------------------------------
        res = (
            supabase.table("meal_plan_day")
            .select("""
                id,
                date,

                daily_macro_order!meal_plan_day_daily_macro_order_id_fkey(
                    kcal_ordered,
                    protein_ordered,
                    carbs_ordered,
                    fat_ordered
                ),

                payment(amount),

                deliveries!meal_plan_day_delivery_id_fkey(
                    delivery_date,
                    status,
                    delivery_slots(
                        start_time,
                        end_time
                    )
                ),

                meal_plan!inner(user_id),

                meal_plan_day_recipe(
                    meal_type,
                    recipe_id,
                    recipe(name),
                    meal_plan_day_recipe_serving(
                        kcal_calculated,
                        protein_calculated,
                        carbs_calculated,
                        fat_calculated
                    )
                )
            """)
            .eq("meal_plan.user_id", user_id)
            .gte("date", from_date)
            .lte("date", to_date)
            .order("date")
            .execute()
        )

        days = []

        for day in res.data or []:
            # -----------------------------
            # Recipes
            # -----------------------------
            recipes_payload = []

            for r in day.get("meal_plan_day_recipe", []):
                servings = r.get("meal_plan_day_recipe_serving") or []

                totals = {
                    "kcal": _round(sum(s["kcal_calculated"] for s in servings)),
                    "protein": _round(sum(s["protein_calculated"] for s in servings)),
                    "carbs": _round(sum(s["carbs_calculated"] for s in servings)),
                    "fat": _round(sum(s["fat_calculated"] for s in servings)),
                }

                recipes_payload.append({
                    "meal_type": r["meal_type"],
                    "recipe_id": r["recipe_id"],
                    "recipe_name": r["recipe"]["name"],
                    **totals
                })

            # -----------------------------
            # Price
            # -----------------------------
            payment = day.get("payment") or []
            price = _round(payment[0]["amount"]) if payment else 0

            # -----------------------------
            # Delivery
            # -----------------------------
            delivery = day.get("deliveries")
            slot = delivery.get("delivery_slots") if delivery else None

            # -----------------------------
            # Day payload
            # -----------------------------
            days.append({
                "date": day["date"],
                "delivery": {
                    "delivery_date": delivery.get("delivery_date") if delivery else None,
                    "delivery_time": (
                        f"{slot['start_time']}-{slot['end_time']}"
                        if slot else None
                    ),
                    "status": delivery.get("status") if delivery else None
                },
                "totals": {
                    "kcal": _round(day["daily_macro_order"]["kcal_ordered"]),
                    "protein": _round(day["daily_macro_order"]["protein_ordered"]),
                    "carbs": _round(day["daily_macro_order"]["carbs_ordered"]),
                    "fat": _round(day["daily_macro_order"]["fat_ordered"]),
                },
                "price": price,
                "recipes": recipes_payload
            })

        return {
            "user_id": user_id,
            "from": from_date,
            "to": to_date,
            "has_orders": len(days) > 0,
            "days": days
        }
