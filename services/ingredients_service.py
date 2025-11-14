from utils.supabase_client import supabase


def get_ingredients_to_buy(start_date, end_date, recipe=None, client=None, delivery_slot=None):

    # ---------------------------------------------------------
    # 1. Fetch deliveries within date range
    # ---------------------------------------------------------
    q = (
        supabase
        .table("deliveries")
        .select("id, meal_plan_day_id")
        .gte("delivery_date", start_date)
        .lte("delivery_date", end_date)
    )

    if client:
        q = q.eq("user_id", client)

    if delivery_slot:
        q = q.eq("delivery_slot_id", delivery_slot)

    deliveries = q.execute().data or []
    if not deliveries:
        return []

    meal_plan_day_ids = [d["meal_plan_day_id"] for d in deliveries if d["meal_plan_day_id"]]

    # ---------------------------------------------------------
    # 2. Fetch meal_plan_day_recipe rows for these days
    # ---------------------------------------------------------
    mprd = (
        supabase
        .table("meal_plan_day_recipe")
        .select("id, recipe_id, meal_plan_day_id")
        .in_("meal_plan_day_id", meal_plan_day_ids)
        .execute()
        .data
    )

    if recipe:
        mprd = [r for r in mprd if str(r["recipe_id"]) == str(recipe)]

    if not mprd:
        return []

    meal_plan_day_recipe_ids = [r["id"] for r in mprd]

    # ---------------------------------------------------------
    # 3. Fetch servings (meal_plan_day_recipe_serving)
    # ---------------------------------------------------------
    servings = (
        supabase
        .table("meal_plan_day_recipe_serving")
        .select("id, subrecipe_id, recipe_subrecipe_serving_calculated, meal_plan_day_recipe_id")
        .in_("meal_plan_day_recipe_id", meal_plan_day_recipe_ids)
        .execute()
        .data
    )

    if not servings:
        return []

    subrecipe_ids = list({s["subrecipe_id"] for s in servings})

    # ---------------------------------------------------------
    # 4. Fetch ingredients for these subrecipes
    # ---------------------------------------------------------
    ingred_rows = (
    supabase
    .table("subrec_ingred")
    .select("""
        subrecipe_id,
        ingredient_id,
        quantity,
        ingredient:ingredient_id (
            name,
            unit,
            serving_per_unit
        )
    """)
    .in_("subrecipe_id", subrecipe_ids)
    .execute()
    .data
    )


    ingred_map = {}
    for row in ingred_rows:
        ingred_map.setdefault(row["subrecipe_id"], []).append(row)

    # ---------------------------------------------------------
    # 5. Multiply servings × ingredient quantities
    # ---------------------------------------------------------
    totals = {}

    for s in servings:
        sub_id = s["subrecipe_id"]
        servings_count = s["recipe_subrecipe_serving_calculated"]

        if sub_id not in ingred_map:
            continue

        for ing in ingred_map[sub_id]:
            ing_id = ing["ingredient_id"]

            # quantity per recipe * number of servings
            base_qty = ing["quantity"] * servings_count

            # convert serving → unit (rice: 1 serving = 100g)
            serving_per_unit = ing["ingredient"]["serving_per_unit"] or 1

            final_qty = base_qty * serving_per_unit

            if ing_id not in totals:
                totals[ing_id] = {
                    "ingredient_id": ing_id,
                    "name": ing["ingredient"]["name"],
                    "unit": ing["ingredient"]["unit"],
                    "total_quantity": 0,
                }

            totals[ing_id]["total_quantity"] += final_qty

    return list(totals.values())
