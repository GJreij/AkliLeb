from utils.supabase_client import supabase
from collections import defaultdict


# ---------------------------------------------------------
#   Helper: Apply NULL, NOT NULL, or normal filter
# ---------------------------------------------------------
def apply_null_filter(query, column, value):
    if value is None:
        return query

    if value == "null":
        return query.is_(column, None)

    if value == "not_null":
        return query.not_.is_(column, None)

    return query.eq(column, value)


# ---------------------------------------------------------
#   Helper: compute per-serving progress
# ---------------------------------------------------------
def _serving_progress(s):
    cooking = (s.get("cooking_status") or "").lower()
    portioning = (s.get("portioning_status") or "").lower()

    completed = {"completed", "complete", "done"}
    pending = {"pending", "in_progress", ""}

    if portioning in completed:
        return 1.0
    if cooking in completed and portioning in pending:
        return 0.5
    return 0.0


# ---------------------------------------------------------
#   Main service: full cooking overview
# ---------------------------------------------------------
def get_cooking_overview(start_date, end_date, filters):

    # =====================================================
    # 1️⃣ Fetch meal_plan_days
    # =====================================================
    mpd = (
        supabase.table("meal_plan_day")
        .select("id, date, delivery_id")
        .gte("date", start_date)
        .lte("date", end_date)
        .execute()
        .data
    )

    if not mpd:
        return []

    mpd_map = {x["id"]: x for x in mpd}
    mpd_ids = [x["id"] for x in mpd]

    # =====================================================
    # 2️⃣ Deliveries filtering
    # =====================================================
    if filters["client_id"] or filters["delivery_slot_id"]:

        deliveries = (
            supabase.table("deliveries")
            .select("id, meal_plan_day_id, user_id, delivery_slot_id")
            .in_("meal_plan_day_id", mpd_ids)
            .execute()
            .data
        )

        client_filter = filters["client_id"]
        if client_filter:
            if client_filter == "null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["user_id"] is None]
            elif client_filter == "not_null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["user_id"] is not None]
            else:
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if str(d["user_id"]) == str(client_filter)
                ]

        slot_filter = filters["delivery_slot_id"]
        if slot_filter:
            if slot_filter == "null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["delivery_slot_id"] is None]
            elif slot_filter == "not_null":
                mpd_ids = [d["meal_plan_day_id"] for d in deliveries if d["delivery_slot_id"] is not None]
            else:
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if str(d["delivery_slot_id"]) == str(slot_filter)
                ]

        if not mpd_ids:
            return []

    # =====================================================
    # 3️⃣ Fetch meal_plan_day_recipe
    # =====================================================
    mpdr_query = (
        supabase.table("meal_plan_day_recipe")
        .select("id, meal_plan_day_id, recipe_id, status")
        .in_("meal_plan_day_id", mpd_ids)
    )

    mpdr_query = apply_null_filter(mpdr_query, "recipe_id", filters["recipe_id"])
    mpdr_query = apply_null_filter(mpdr_query, "status", filters["status"])

    mpdr = mpdr_query.execute().data
    if not mpdr:
        return []

    mpdr_ids = [x["id"] for x in mpdr]
    recipe_ids = list({x["recipe_id"] for x in mpdr})

    # =====================================================
    # 4️⃣ Recipes
    # =====================================================
    recipes = (
        supabase.table("recipe")
        .select("*")
        .in_("id", recipe_ids)
        .execute()
        .data
    )
    recipe_map = {r["id"]: r for r in recipes}

    # =====================================================
    # 5️⃣ Servings (meal_plan_day_recipe_serving)
    # =====================================================
    servings_query = (
        supabase.table("meal_plan_day_recipe_serving")
        .select("*")
        .in_("meal_plan_day_recipe_id", mpdr_ids)
    )

    servings_query = apply_null_filter(servings_query, "subrecipe_id", filters["subrecipe_id"])
    servings_query = apply_null_filter(servings_query, "cooking_status", filters["status"])
    servings_query = apply_null_filter(servings_query, "portioning_status", filters["status"])

    servings = servings_query.execute().data
    if not servings:
        return []

    # SORT SERVINGS TO ENSURE DETERMINISTIC ORDER
    servings.sort(key=lambda s: (s.get("subrecipe_id") or 0, s.get("id")))

    subrecipe_ids = list({s["subrecipe_id"] for s in servings if s["subrecipe_id"]})

    # =====================================================
    # 6️⃣ Subrecipes
    # =====================================================
    subrecipes = (
        supabase.table("subrecipe")
        .select("*")
        .in_("id", subrecipe_ids)
        .execute()
        .data
    )
    subrecipe_map = {s["id"]: s for s in subrecipes}

    # =====================================================
    # 7️⃣ Subrecipe ingredients
    # =====================================================
    subrec_ingred = (
        supabase.table("subrec_ingred")
        .select("*")
        .in_("subrecipe_id", subrecipe_ids)
        .execute()
        .data
    )

    ingredient_ids = list({i["ingredient_id"] for i in subrec_ingred})

    ingredients = (
        supabase.table("ingredient")
        .select("*")
        .in_("id", ingredient_ids)
        .execute()
        .data
    )
    ingredient_map = {i["id"]: i for i in ingredients}

    subrec_ing_map = defaultdict(list)
    for ing in subrec_ingred:
        subrec_ing_map[ing["subrecipe_id"]].append(ing)

    # =====================================================
    # 8️⃣ Build final output
    # =====================================================
    output = []

    # SORT RECIPES BY EARLIEST DATE
    recipe_ids_sorted = sorted(
        recipe_ids,
        key=lambda rid: min(
            mpd_map[r["meal_plan_day_id"]]["date"]
            for r in mpdr
            if r["recipe_id"] == rid
        )
    )

    for recipe_id in recipe_ids_sorted:
        recipe = recipe_map.get(recipe_id)
        if not recipe:
            continue

        mpdr_for_recipe = [r for r in mpdr if r["recipe_id"] == recipe_id]
        mpdr_ids_for_recipe = [r["id"] for r in mpdr_for_recipe]

        recipe_servings = [
            s for s in servings if s["meal_plan_day_recipe_id"] in mpdr_ids_for_recipe
        ]
        if not recipe_servings:
            continue

        dates = [mpd_map[r["meal_plan_day_id"]]["date"] for r in mpdr_for_recipe]
        earliest_date = min(dates)

        # ------------------------------------------
        # 🟦 RECIPE-LEVEL INGREDIENTS (sorted alphabetically)
        # ------------------------------------------
        recipe_ing_totals = defaultdict(float)

        for s in recipe_servings:
            sub_id = s["subrecipe_id"]
            if not sub_id:
                continue

            multiplier = s["recipe_subrecipe_serving_calculated"] or 0

            for ing in subrec_ing_map[sub_id]:
                ing_id = ing["ingredient_id"]
                base_qty = ing["quantity"] or 0

                ing_def = ingredient_map.get(ing_id, {})
                serving_per_unit = ing_def.get("serving_per_unit") or 1.0

                recipe_ing_totals[ing_id] += base_qty * multiplier * serving_per_unit

        ingredient_list = sorted(
            [
                {
                    "ingredient_id": ing_id,
                    "name": ingredient_map[ing_id]["name"],
                    "unit": ingredient_map[ing_id]["unit"],
                    "total_quantity": round(qty, 1),
                }
                for ing_id, qty in recipe_ing_totals.items()
            ],
            key=lambda x: x["name"].lower(),
        )

        # ------------------------------------------
        # 🟧 SUBRECIPES (sorted alphabetically)
        # ------------------------------------------
        servings_by_sub = defaultdict(list)
        for s in recipe_servings:
            if s["subrecipe_id"]:
                servings_by_sub[s["subrecipe_id"]].append(s)

        subrecipe_list = []

        for sub_id, sub_servings in servings_by_sub.items():
            sub = subrecipe_map.get(sub_id)
            if not sub:
                continue

            total_servings = sum(
                (s["recipe_subrecipe_serving_calculated"] or 0) for s in sub_servings
            )

            # progress (avg of serving progresses)
            progress_values = [_serving_progress(s) for s in sub_servings]
            sub_progress = int((sum(progress_values) / len(progress_values)) * 100)

            if sub_progress == 100:
                sub_status = "completed"
            elif sub_progress == 0:
                sub_status = "pending"
            else:
                sub_status = "in_progress"

            # SUBRECIPE INGREDIENTS (sorted alphabetically)
            sub_ing_totals = defaultdict(float)
            for ing in subrec_ing_map[sub_id]:
                ing_id = ing["ingredient_id"]
                base_qty = ing["quantity"] or 0

                ing_def = ingredient_map.get(ing_id, {})
                serving_per_unit = ing_def.get("serving_per_unit") or 1.0

                sub_ing_totals[ing_id] += base_qty * total_servings * serving_per_unit

            sub_ing_list = sorted(
                [
                    {
                        "ingredient_id": ing_id,
                        "name": ingredient_map[ing_id]["name"],
                        "unit": ingredient_map[ing_id]["unit"],
                        "quantity": round(qty, 1),
                    }
                    for ing_id, qty in sub_ing_totals.items()
                ],
                key=lambda x: x["name"].lower(),
            )

            subrecipe_list.append(
                {
                    "subrecipe_id": sub_id,
                    "name": sub["name"],
                    "description": sub["description"],
                    "instructions": sub["instructions"],
                    "status": sub_status,
                    "progress": sub_progress,
                    "total_servings": total_servings,
                    "selected_meal_plan_day_recipe_serving_id": [s["id"] for s in sub_servings],
                    "ingredients_needed": sub_ing_list,
                }
            )

        # SORT SUBRECIPES ALPHABETICALLY
        subrecipe_list = sorted(subrecipe_list, key=lambda x: x["name"].lower())

        # ------------------------------------------------
        # 🟥 RECIPE PROGRESS = average of subrecipe progress
        # ------------------------------------------------
        recipe_progress = int(
            sum(sub["progress"] for sub in subrecipe_list) / len(subrecipe_list)
        ) if subrecipe_list else 0

        # final assembled recipe item
        output.append(
            {
                "recipe_id": recipe_id,
                "name": recipe["name"],
                "description": recipe["description"],
                "instructions": recipe["instructions"],
                "meal_plan_day_recipe_ids": mpdr_ids_for_recipe,
                "earliest_date": earliest_date,
                "status": mpdr_for_recipe[0]["status"],
                "progress": recipe_progress,
                "ingredients_needed": ingredient_list,
                "subrecipes": subrecipe_list,
            }
        )

    # FINAL SORT: recipes by earliest date
    output = sorted(output, key=lambda r: r["earliest_date"])

    return output
