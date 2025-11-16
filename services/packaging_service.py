# services/packaging_service.py

from utils.supabase_client import supabase
from datetime import datetime


def get_packaging_view(start_date, end_date):
    """
    Packaging process:
    For each client and delivery between start_date and end_date, return:
    - client name / last name
    - delivery_date
    - delivery slot
    - recipes
      - meal_type
      - recipe name
      - subrecipes + serving size
    """

    # --- 1. Get deliveries in range ---
    deliveries_res = (
        supabase.table("deliveries")
        .select("id, delivery_date, delivery_slot_id, user_id")
        .gte("delivery_date", start_date)
        .lte("delivery_date", end_date)
        .execute()
    )

    deliveries = deliveries_res.data or []
    if not deliveries:
        return []

    delivery_ids = [d["id"] for d in deliveries]
    deliveries_by_id = {d["id"]: d for d in deliveries}

    # --- 2. Get delivery slots ---
    slot_ids = list({d["delivery_slot_id"] for d in deliveries if d.get("delivery_slot_id")})
    slots_by_id = {}

    if slot_ids:
        slots_res = (
            supabase.table("delivery_slots")
            .select("id, start_time, end_time")
            .in_("id", slot_ids)
            .execute()
        )
        slots_by_id = {s["id"]: s for s in (slots_res.data or [])}

    # --- 3. Get users ---
    user_ids = list({d["user_id"] for d in deliveries if d.get("user_id")})
    users_by_id = {}

    if user_ids:
        users_res = (
            supabase.table("user")
            .select("id, name, last_name")
            .in_("id", user_ids)
            .execute()
        )
        users_by_id = {u["id"]: u for u in (users_res.data or [])}

    # --- 4. meal_plan_day linked to these deliveries ---
    mpd_res = (
        supabase.table("meal_plan_day")
        .select("id, meal_plan_id, date, delivery_id")
        .in_("delivery_id", delivery_ids)
        .execute()
    )
    mpd = mpd_res.data or []
    mpd_by_id = {m["id"]: m for m in mpd}
    mpd_ids = [m["id"] for m in mpd]

    # --- 5. meal_plan_day_recipe (recipe of the day) ---
    mpdr_res = (
        supabase.table("meal_plan_day_recipe")
        .select("id, meal_plan_day_id, recipe_id, meal_type")
        .in_("meal_plan_day_id", mpd_ids)
        .execute()
    )
    mpdr = mpdr_res.data or []
    mpdr_by_day = {}

    for r in mpdr:
        mpdr_by_day.setdefault(r["meal_plan_day_id"], []).append(r)

    recipe_ids = [r["recipe_id"] for r in mpdr if r.get("recipe_id")]

    # --- 6. recipes info ---
    recipes_by_id = {}
    if recipe_ids:
        recipes_res = (
            supabase.table("recipe")
            .select("id, name")
            .in_("id", recipe_ids)
            .execute()
        )
        recipes_by_id = {r["id"]: r for r in (recipes_res.data or [])}

    # --- 7. subrecipe serving info (meal_plan_day_recipe_serving) ---
    mpdr_ids = [r["id"] for r in mpdr]
    servings_res = (
        supabase.table("meal_plan_day_recipe_serving")
        .select("id, meal_plan_day_recipe_id, subrecipe_id, recipe_subrecipe_serving_calculated")
        .in_("meal_plan_day_recipe_id", mpdr_ids)
        .execute()
    )
    servings = servings_res.data or []

    servings_by_mpdr = {}
    for s in servings:
        servings_by_mpdr.setdefault(s["meal_plan_day_recipe_id"], []).append(s)

    # --- 8. fetch subrecipes info ---
    subrecipe_ids = list({s["subrecipe_id"] for s in servings if s.get("subrecipe_id")})
    subrecipes_by_id = {}
    if subrecipe_ids:
        sub_res = (
            supabase.table("subrecipe")
            .select("id, name")
            .in_("id", subrecipe_ids)
            .execute()
        )
        subrecipes_by_id = {s["id"]: s for s in (sub_res.data or [])}

    # --- 9. Build structured packaging list ---

    packaging = []

    for delivery in deliveries:
        user = users_by_id.get(delivery["user_id"])
        slot = slots_by_id.get(delivery["delivery_slot_id"])

        # which meal_plan_day belongs to this delivery?
        for mpd_entry in mpd:
            if mpd_entry["delivery_id"] != delivery["id"]:
                continue

            mpdr_list = mpdr_by_day.get(mpd_entry["id"], [])

            recipes_output = []

            for r in mpdr_list:
                recipe_info = recipes_by_id.get(r["recipe_id"])
                servings_list = servings_by_mpdr.get(r["id"], [])

                subrecipes_output = []
                for serv in servings_list:
                    subrecipes_output.append({
                        "subrecipe_id": serv["subrecipe_id"],
                        "subrecipe_name": subrecipes_by_id.get(serv["subrecipe_id"], {}).get("name"),
                        "serving_size": serv["recipe_subrecipe_serving_calculated"]
                    })

                recipes_output.append({
                    "meal_type": r.get("meal_type"),
                    "recipe_id": r.get("recipe_id"),
                    "recipe_name": recipe_info.get("name") if recipe_info else None,
                    "subrecipes": subrecipes_output
                })

            packaging.append({
                "client": {
                    "name": user.get("name") if user else None,
                    "last_name": user.get("last_name") if user else None
                },
                "delivery_date": delivery.get("delivery_date"),
                "delivery_slot": slot,
                "recipes": recipes_output
            })

    # --- 10. Sorting by date and slot ---

    def sort_key(entry):
        date = entry["delivery_date"]
        slot = entry["delivery_slot"]["start_time"] if entry["delivery_slot"] else "00:00:00"
        name = entry["client"]["name"] or ""
        return (date, slot, name)

    packaging.sort(key=sort_key)

    return packaging
