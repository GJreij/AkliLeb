from utils.supabase_client import supabase
from collections import defaultdict


# ---------------------------------------------------------
#   Helper: Apply NULL, NOT NULL, or normal filter
# ---------------------------------------------------------
def apply_null_filter(query, column, value):
    """
    value can be:
      - None → no filter
      - "null" → WHERE column IS NULL
      - "not_null" → WHERE column IS NOT NULL
      - other → WHERE column = value
    """
    if value is None:
        return query

    if value == "null":
        return query.is_(column, None)

    if value == "not_null":
        return query.not_.is_(column, None)

    return query.eq(column, value)


def _serving_progress(s):
    """
    Returns progress for a single meal_plan_day_recipe_serving as a float in [0, 1].

    Rules:
      - if portioning_status is completed → 100% (1.0)
      - elif cooking_status is completed and portioning_status is pending → 50% (0.5)
      - else → 0%
    """
    cooking = (s.get("cooking_status") or "").lower()
    portioning = (s.get("portioning_status") or "").lower()

    completed_values = {"completed", "complete", "done"}
    pending_values = {"pending", "in_progress", ""}

    if portioning in completed_values:
        return 1.0

    if cooking in completed_values and (portioning in pending_values or portioning is None):
        return 0.5

    return 0.0


# ---------------------------------------------------------
#   Main service: full cooking overview
# ---------------------------------------------------------
def get_cooking_overview(start_date, end_date, filters):
    """
    Returns aggregated cooking tasks grouped by recipe with:
      - recipe info (+ instructions)
      - earliest cooking date
      - progress (recipe-level)
      - required ingredients (recipe-level, with quantity_per_unit)
      - required subrecipes (aggregated) + ingredients (+ instructions, progress)
    """

    # =====================================================
    # 1️⃣ Fetch meal_plan_days inside date range
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
    mpd_ids = list(mpd_map.keys())

    # =====================================================
    # 2️⃣ Fetch deliveries if filtering on client or slot
    # =====================================================
    deliveries_map = {}
    if filters["client_id"] or filters["delivery_slot_id"]:

        deliveries = (
            supabase.table("deliveries")
            .select("id, meal_plan_day_id, user_id, delivery_slot_id")
            .in_("meal_plan_day_id", mpd_ids)
            .execute()
            .data
        )

        deliveries_map = {d["id"]: d for d in deliveries}

        # Filter by client
        client_filter = filters["client_id"]
        if client_filter:
            if client_filter == "null":
                mpd_ids = [
                d["meal_plan_day_id"] for d in deliveries
                if d["user_id"] is None
                ]
            elif client_filter == "not_null":
                mpd_ids = [
                d["meal_plan_day_id"] for d in deliveries
                if d["user_id"] is not None
                ]
            else:
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if str(d["user_id"]) == str(client_filter)
                ]

        # Filter by slot
        slot_filter = filters["delivery_slot_id"]
        if slot_filter:
            if slot_filter == "null":
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if d["delivery_slot_id"] is None
                ]
            elif slot_filter == "not_null":
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if d["delivery_slot_id"] is not None
                ]
            else:
                mpd_ids = [
                    d["meal_plan_day_id"]
                    for d in deliveries
                    if str(d["delivery_slot_id"]) == str(slot_filter)
                ]

        if not mpd_ids:
            return []

    # =====================================================
    # 3️⃣ Fetch meal_plan_day_recipe records
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
    # 4️⃣ Get recipe definitions
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
    # 5️⃣ Get servings (subrecipes)
    # =====================================================
    servings_query = (
        supabase.table("meal_plan_day_recipe_serving")
        .select("*")
        .in_("meal_plan_day_recipe_id", mpdr_ids)
    )

    servings_query = apply_null_filter(
        servings_query, "subrecipe_id", filters["subrecipe_id"]
    )
    servings_query = apply_null_filter(
        servings_query, "cooking_status", filters["status"]
    )
    servings_query = apply_null_filter(
        servings_query, "portioning_status", filters["status"]
    )

    servings = servings_query.execute().data
    if not servings:
        return []

    subrecipe_ids = list({s["subrecipe_id"] for s in servings if s["subrecipe_id"] is not None})

    # =====================================================
    # 6️⃣ Fetch subrecipe definitions
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
    # 7️⃣ Fetch ingredients for each subrecipe
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

    # Index ingredients per subrecipe
    subrec_ing_map = defaultdict(list)
    for ing in subrec_ingred:
        subrec_ing_map[ing["subrecipe_id"]].append(ing)

    # =====================================================
    # 8️⃣ Build final aggregated output
    # =====================================================
    output = []

    for recipe_id in recipe_ids:
        recipe = recipe_map.get(recipe_id)
        if not recipe:
            continue

        # All mpdr entries for this recipe
        mpdr_for_recipe = [r for r in mpdr if r["recipe_id"] == recipe_id]
        mpdr_for_recipe_ids = [r["id"] for r in mpdr_for_recipe]

        # All servings belonging to those mpdrs
        recipe_servings = [
            s for s in servings
            if s["meal_plan_day_recipe_id"] in mpdr_for_recipe_ids
        ]
        if not recipe_servings:
            continue

        # Earliest cook date
        dates = [mpd_map[r["meal_plan_day_id"]]["date"] for r in mpdr_for_recipe]
        earliest_date = min(dates)

        # ----------------------------------
        # Recipe-level progress (based on cooking_status "done")
        # (unchanged, but still ok to keep)
        # ----------------------------------
        total_sub = len(recipe_servings)
        done_sub = len([s for s in recipe_servings if (s.get("cooking_status") or "").lower() in {"done", "completed", "complete"}])
        recipe_progress = int(done_sub / total_sub * 100) if total_sub else 0

        # ==================================
        # Recipe-level ingredients
        # ==================================
        recipe_ing_totals = defaultdict(float)

        for s in recipe_servings:
            sub_id = s["subrecipe_id"]
            if sub_id is None:
                continue

            multiplier = s["recipe_subrecipe_serving_calculated"] or 0

            for ing in subrec_ing_map[sub_id]:
                ing_id = ing["ingredient_id"]
                base_qty = ing["quantity"] or 0

                ingredient_def = ingredient_map.get(ing_id, {})
                # 🔢 USE serving_per_unit (or 1.0 if missing)
                serving_per_unit = ingredient_def.get("serving_per_unit") or 1.0


                recipe_ing_totals[ing_id] += base_qty * multiplier * serving_per_unit

        ingredient_list = []
        for ing_id, qty in recipe_ing_totals.items():
            ing_def = ingredient_map.get(ing_id)
            if not ing_def:
                continue
            ingredient_list.append(
                {
                    "ingredient_id": ing_id,
                    "name": ing_def.get("name"),
                    "unit": ing_def.get("unit"),
                    "total_quantity": qty,
                }
            )

        # ==================================
        # Build aggregated subrecipe details
        # ==================================
        # Group servings by subrecipe_id
        servings_by_sub = defaultdict(list)
        for s in recipe_servings:
            sub_id = s["subrecipe_id"]
            if sub_id is None:
                continue
            servings_by_sub[sub_id].append(s)

        subrecipe_list = []

        for sub_id, servings_for_sub in servings_by_sub.items():
            sub = subrecipe_map.get(sub_id)
            if not sub:
                continue

            # Total servings for this subrecipe in this recipe
            total_servings = sum(
                (s["recipe_subrecipe_serving_calculated"] or 0)
                for s in servings_for_sub
            )

            # Progress for this subrecipe (avg over its servings)
            progress_values = [_serving_progress(s) for s in servings_for_sub]
            sub_progress = int(
                (sum(progress_values) / len(progress_values)) * 100
            ) if progress_values else 0

            # Derive an aggregated status from progress (optional, but useful)
            if sub_progress == 100:
                sub_status = "completed"
            elif sub_progress == 0:
                sub_status = "pending"
            else:
                sub_status = "in_progress"

            # Ingredients needed for this subrecipe (aggregated)
            sub_ing_totals = defaultdict(float)
            for ing in subrec_ing_map[sub_id]:
                ing_id = ing["ingredient_id"]
                base_qty = ing["quantity"] or 0

                ing_def = ingredient_map.get(ing_id, {})
                serving_per_unit = ing_def.get("serving_per_unit") or 1.0

                # base_qty is per 1 serving of subrecipe
                sub_ing_totals[ing_id] += base_qty * total_servings * serving_per_unit

            sub_ing_list = []
            for ing_id, qty in sub_ing_totals.items():
                ing_def = ingredient_map.get(ing_id)
                if not ing_def:
                    continue

                sub_ing_list.append(
                    {
                        "ingredient_id": ing_id,
                        "name": ing_def.get("name"),
                        "unit": ing_def.get("unit"),
                        # keep key name "quantity" to avoid breaking consumers
                        "quantity": qty,
                    }
                )

            subrecipe_list.append(
                {
                    "subrecipe_id": sub_id,
                    "name": sub.get("name"),
                    "description": sub.get("description"),
                    # 🆕 add instructions for subrecipes
                    "instructions": sub.get("instructions"),
                    "status": sub_status,
                    # 🆕 total servings aggregated across all mpdr_servings
                    "total_servings": total_servings,
                    # 🆕 combination of all serving IDs that contribute to this subrecipe
                    "selected_meal_plan_day_recipe_serving_id": [
                        s["id"] for s in servings_for_sub
                    ],
                    # 🆕 progress for this subrecipe (0–100)
                    "progress": sub_progress,
                    "ingredients_needed": sub_ing_list,
                }
            )

        # ==================================
        # Final recipe object
        # ==================================
        output.append(
            {
                "recipe_id": recipe_id,
                "name": recipe.get("name"),
                "description": recipe.get("description"),
                # 🆕 add instructions for recipes
                "instructions": recipe.get("instructions"),
                "meal_plan_day_recipe_ids": mpdr_for_recipe_ids,
                "earliest_date": earliest_date,
                "status": mpdr_for_recipe[0]["status"],
                "progress": recipe_progress,
                "ingredients_needed": ingredient_list,
                "subrecipes": subrecipe_list,
            }
        )

    return output
