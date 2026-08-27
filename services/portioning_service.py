# services/portioning_service.py

from utils.supabase_client import supabase

ALLERGEN_KEYS = [
    "celery", "cereals_containing_gluten", "crustaceans", "eggs", "fish",
    "lupin", "milk", "molluscs", "sulphites", "mustard", "peanuts",
    "sesame", "soybeans", "tree_nuts",
]


def normalize_filter_value(value):
    if value is None:
        return None
    if isinstance(value, str):
        if value.strip() in ("", "null", "Null", "NULL"):
            return None
    return value


def parse_int_list(raw_value, field_name):
    if raw_value is None:
        raise ValueError(f"{field_name} is required")

    if isinstance(raw_value, str):
        parts = [p.strip() for p in raw_value.split(",") if p.strip() != ""]
        try:
            return [int(p) for p in parts]
        except ValueError:
            raise ValueError(f"{field_name} must be a comma-separated list of integers")

    if isinstance(raw_value, int):
        return [raw_value]

    if isinstance(raw_value, (list, tuple)):
        out = []
        for x in raw_value:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                raise ValueError(f"{field_name} contains non-integer")
        return out

    raise ValueError(f"Unsupported format for {field_name}")


def get_portioning_summary(subrecipe_id, meal_plan_day_recipe_ids):

    # --- 1. Fetch servings ---
    servings_res = (
        supabase.table("meal_plan_day_recipe_serving")
        .select(
            "id, meal_plan_day_recipe_id, subrecipe_id, "
            "recipe_subrecipe_serving_calculated, weight_after_cooking, portioning_status "
        )
        .eq("subrecipe_id", subrecipe_id)
        .in_("meal_plan_day_recipe_id", meal_plan_day_recipe_ids)
        .execute()
    )
    servings = servings_res.data or []

    if not servings:
        return None, "No servings found"

    # Make sure subrecipe appears in *all* input meal_plan_day_recipe_ids
    found_ids = {row["meal_plan_day_recipe_id"] for row in servings}
    expected_ids = set(meal_plan_day_recipe_ids)

    if found_ids != expected_ids:
        missing = list(expected_ids - found_ids)
        extra = list(found_ids - expected_ids)
        return None, {
            "error": "Subrecipe missing in some MPDRs",
            "missing": missing,
            "extra_found": extra
        }

    total_subrecipe_servings = sum(
        row.get("recipe_subrecipe_serving_calculated") or 0 for row in servings
    )

    # --- 2. meal_plan_day_recipe → meal_plan_day_id ---
    # Also pulls recipe_id/meal_type so each client line can show which meal
    # this portion belongs to — the same subrecipe can appear in two
    # different meals for the same client on the same day (e.g. a salad in
    # both their lunch and dinner recipe), and those must stay two separate
    # physical portions, not get folded into one combined line.
    mpdr_res = (
        supabase.table("meal_plan_day_recipe")
        .select("id, meal_plan_day_id, recipe_id, meal_type")
        .in_("id", list(found_ids))
        .execute()
    )
    mpdr_rows = mpdr_res.data or []
    mpdr_by_id = {row["id"]: row for row in mpdr_rows}

    mpd_ids = {
        row["meal_plan_day_id"]
        for row in mpdr_rows
        if row.get("meal_plan_day_id") is not None
    }

    recipe_ids_for_mpdr = {
        row["recipe_id"] for row in mpdr_rows if row.get("recipe_id") is not None
    }
    recipes_by_id = {}
    if recipe_ids_for_mpdr:
        recipes_res = (
            supabase.table("recipe")
            .select("id, name")
            .in_("id", list(recipe_ids_for_mpdr))
            .execute()
        )
        recipes_by_id = {r["id"]: r for r in (recipes_res.data or [])}

    # --- 3. meal_plan_day ---
    mpd_res = (
        supabase.table("meal_plan_day")
        .select("id, date, delivery_id")
        .in_("id", list(mpd_ids))
        .execute()
    )
    mpd_by_id = {row["id"]: row for row in (mpd_res.data or [])}

    # --- 4. deliveries ---
    delivery_ids = {
        row["delivery_id"]
        for row in mpd_by_id.values()
        if row.get("delivery_id")
    }
    deliveries_by_id = {}
    if delivery_ids:
        deliv_res = (
            supabase.table("deliveries")
            .select("id, delivery_date, delivery_slot_id, user_id")
            .in_("id", list(delivery_ids))
            .execute()
        )
        deliveries_by_id = {r["id"]: r for r in (deliv_res.data or [])}

    # --- 5. users ---
    user_ids = {
        d["user_id"] for d in deliveries_by_id.values() if d.get("user_id")
    }
    users_by_id = {}
    if user_ids:
        users_res = (
            supabase.table("user")
            .select("id, name, last_name, " + ", ".join(ALLERGEN_KEYS))
            .in_("id", list(user_ids))
            .execute()
        )
        users_by_id = {u["id"]: u for u in (users_res.data or [])}

    # --- 6. delivery slots ---
    slot_ids = {
        d["delivery_slot_id"] for d in deliveries_by_id.values() if d.get("delivery_slot_id")
    }
    slots_by_id = {}
    if slot_ids:
        slots_res = (
            supabase.table("delivery_slots")
            .select("id, start_time, end_time")
            .in_("id", list(slot_ids))
            .execute()
        )
        slots_by_id = {s["id"]: s for s in (slots_res.data or [])}

    # --- 7. Subrecipe info ---
    subrecipe_res = (
        supabase.table("subrecipe")
        .select("*")
        .eq("id", subrecipe_id)
        .execute()
    )
    if not subrecipe_res.data:
        return None, f"Subrecipe {subrecipe_id} not found"

    subrecipe_info = subrecipe_res.data[0]

    # --- 7b. Subrecipe allergen rollup — same subrecipe_allergen view the
    # customer-facing recipe_allergen view is built on, so a "this client is
    # allergic" alert here can never disagree with what the client saw when
    # ordering.
    subrecipe_allergen_res = (
        supabase.table("subrecipe_allergen")
        .select("*")
        .eq("subrecipe_id", subrecipe_id)
        .execute()
    )
    subrecipe_allergens = subrecipe_allergen_res.data[0] if subrecipe_allergen_res.data else {}
    subrecipe_allergen_keys = {k for k in ALLERGEN_KEYS if subrecipe_allergens.get(k)}

    # --- 8. Subrecipe ingredients ---
    sub_ingred_res = (
        supabase.table("subrec_ingred")
        .select("id, subrecipe_id, ingredient_id, quantity, optional")
        .eq("subrecipe_id", subrecipe_id)
        .execute()
    )
    sub_ingred = sub_ingred_res.data or []

    ingredient_ids = [r["ingredient_id"] for r in sub_ingred if r.get("ingredient_id")]
    ingredients_by_id = {}
    if ingredient_ids:
        ing_res = (
            supabase.table("ingredient")
            .select("id, name, unit, serving_per_unit")
            .in_("id", ingredient_ids)
            .execute()
        )
        ingredients_by_id = {i["id"]: i for i in (ing_res.data or [])}

    # --- Build result lines per client ---
    clients = []
    for r in servings:
        mpdr = mpdr_by_id.get(r["meal_plan_day_recipe_id"])
        mpd = mpd_by_id.get(mpdr["meal_plan_day_id"])
        deliv = deliveries_by_id.get(mpd.get("delivery_id"))
        user = users_by_id.get(deliv.get("user_id")) if deliv else None
        slot = slots_by_id.get(deliv.get("delivery_slot_id")) if deliv else None
        recipe = recipes_by_id.get(mpdr.get("recipe_id")) if mpdr else None

        # Alert-only, never filters/reorders this list — just which of the
        # client's own declared allergens this specific subrecipe contains.
        client_allergens = []
        if user and subrecipe_allergen_keys:
            client_allergens = [k for k in subrecipe_allergen_keys if user.get(k)]

        clients.append({
            "meal_plan_day_recipe_serving_id": r["id"],
            "meal_plan_day_recipe_id": r["meal_plan_day_recipe_id"],
            "recipe_name": recipe.get("name") if recipe else None,
            "meal_type": mpdr.get("meal_type") if mpdr else None,
            "delivery_date": deliv.get("delivery_date") if deliv else None,
            "delivery_slot": slot,
            # Trimmed back down to id/name — the 14 allergen booleans were
            # only pulled in to compute client_allergens above, not to leak
            # raw flags through this response.
            "client": {"id": user["id"], "name": user.get("name"), "last_name": user.get("last_name")} if user else None,
            "client_allergens": client_allergens,

            # servings
            "servings_for_client": r.get("recipe_subrecipe_serving_calculated"),

            # portioning status
            "portioning_status": r.get("portioning_status"),

            # weight handling
            "weight_after_cooking": r.get("weight_after_cooking") or 0,
            "has_weight_after_cooking": r.get("weight_after_cooking") is not None
        })


    # --- Ingredient summary ---
    ingredients_summary = []
    for rel in sub_ingred:
        ing = ingredients_by_id.get(rel["ingredient_id"])
        if not ing:
            continue

        qty = rel.get("quantity") or 0
        spu = ing.get("serving_per_unit") or 0

        total_servings_equivalent = total_subrecipe_servings * qty * spu
        total_units = total_subrecipe_servings * qty

        ingredients_summary.append({
            "ingredient_id": ing["id"],
            "name": ing.get("name"),
            "unit": ing.get("unit"),
            "quantity_per_subrecipe": qty,
            "serving_per_unit": spu,
            "total_units_for_batch": total_units,
            "total_servings_equivalent": total_servings_equivalent,
            "optional": rel.get("optional")
        })

    # Final response dictionary
    return {
        "subrecipe": subrecipe_info,
        "summary": {
            "total_subrecipe_servings_for_batch": total_subrecipe_servings,
            "ingredients": ingredients_summary
        },
        "clients": clients
    }, None
