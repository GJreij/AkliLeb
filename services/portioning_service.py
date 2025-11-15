# services/portioning_service.py

from typing import Any, Dict, List, Optional
from sqlalchemy import text
from utils.supabase_client import engine  # adapt if needed


def _sanitize_filter(value: Optional[Any]) -> Optional[Any]:
    """Convert "", " ", "null", "Null", "NULL" -> None."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if v == "" or v.lower() == "null":
            return None
        return v
    return value


def _build_in_clause(column_name: str, values: List[Any], param_prefix: str = "p"):
    if not values:
        raise ValueError("Values list for IN clause cannot be empty")

    placeholders = []
    params = {}
    for idx, val in enumerate(values):
        key = f"{param_prefix}{idx}"
        placeholders.append(f":{key}")
        params[key] = val

    clause = f"{column_name} IN ({', '.join(placeholders)})"
    return clause, params


def get_portioning_view_for_serving_ids(serving_ids: List[int]) -> Dict[str, Any]:
    """
    Core function used by both pages (cooking + portioning).
    """
    if not serving_ids:
        return {
            "clients": [],
            "selection_stats": {"total_servings": 0, "ingredients": []},
        }

    in_clause, in_params = _build_in_clause("mprs.id", serving_ids, param_prefix="sid")

    # --- Per-client info ---
    client_sql = f"""
        SELECT
            mprs.id AS meal_plan_day_recipe_serving_id,
            mprs.recipe_subrecipe_serving_calculated AS servings,
            mprs.weight_after_cooking,
            COALESCE(d.delivery_date, mpd.date) AS delivery_date,
            ds.id AS delivery_slot_id,
            ds.start_time,
            ds.end_time,
            u.first_name,
            u.last_name
        FROM meal_plan_day_recipe_serving mprs
        JOIN meal_plan_day_recipe mpr
            ON mprs.meal_plan_day_recipe_id = mpr.id
        JOIN meal_plan_day mpd
            ON mpr.meal_plan_day_id = mpd.id
        LEFT JOIN deliveries d
            ON mpd.delivery_id = d.id
        LEFT JOIN delivery_slots ds
            ON d.delivery_slot_id = ds.id
        JOIN meal_plan mp
            ON mpd.meal_plan_id = mp.id
        JOIN "user" u
            ON mp.user_id = u.id
        WHERE {in_clause}
        ORDER BY delivery_date, ds.start_time, u.last_name, u.first_name
    """

    # --- Ingredient aggregation ---
    ingredients_sql = f"""
        SELECT
            i.id AS ingredient_id,
            i.name,
            i.unit,
            SUM(
                COALESCE(mprs.recipe_subrecipe_serving_calculated, 0)
                * COALESCE(si.quantity, 0)
                * COALESCE(i.serving_per_unit, 1)
            ) AS total_quantity
        FROM meal_plan_day_recipe_serving mprs
        JOIN subrec_ingred si
            ON mprs.subrecipe_id = si.subrecipe_id
        JOIN ingredient i
            ON si.ingredient_id = i.id
        WHERE {in_clause}
        GROUP BY i.id, i.name, i.unit
        ORDER BY i.name
    """

    # --- Total servings ---
    total_servings_sql = f"""
        SELECT SUM(COALESCE(recipe_subrecipe_serving_calculated, 0)) AS total_servings
        FROM meal_plan_day_recipe_serving
        WHERE {in_clause}
    """

    with engine.connect() as conn:
        client_rows = conn.execute(text(client_sql), in_params).mappings().all()
        ingredient_rows = conn.execute(text(ingredients_sql), in_params).mappings().all()
        total_row = conn.execute(text(total_servings_sql), in_params).mappings().first()

    total_servings = float(total_row["total_servings"] or 0)

    clients = []
    for r in client_rows:
        clients.append({
            "meal_plan_day_recipe_serving_id": r["meal_plan_day_recipe_serving_id"],
            "delivery_date": r["delivery_date"].isoformat() if r["delivery_date"] else None,
            "delivery_slot": {
                "id": r["delivery_slot_id"],
                "start_time": str(r["start_time"]) if r["start_time"] else None,
                "end_time": str(r["end_time"]) if r["end_time"] else None,
            },
            "client_first_name": r["first_name"],
            "client_last_name": r["last_name"],
            "servings": float(r["servings"] or 0),
            "has_weight_after_cooking": r["weight_after_cooking"] is not None,
            "weight_after_cooking": (
                float(r["weight_after_cooking"])
                if r["weight_after_cooking"] is not None
                else None
            ),
        })

    ingredients = [
        {
            "ingredient_id": r["ingredient_id"],
            "name": r["name"],
            "unit": r["unit"],
            "total_quantity": float(r["total_quantity"] or 0),
        }
        for r in ingredient_rows
    ]

    return {
        "clients": clients,
        "selection_stats": {
            "total_servings": total_servings,
            "ingredients": ingredients,
        },
    }


def get_portioning_view_by_filters(
    *,
    start_date: str,
    end_date: str,
    recipe_id: Optional[int] = None,
    delivery_slot_id: Optional[int] = None,
    subrecipe_id: Optional[int] = None,
    cooking_status: Optional[str] = None,
    portioning_status: Optional[str] = None,
) -> Dict[str, Any]:

    recipe_id = _sanitize_filter(recipe_id)
    delivery_slot_id = _sanitize_filter(delivery_slot_id)
    subrecipe_id = _sanitize_filter(subrecipe_id)
    cooking_status = _sanitize_filter(cooking_status)
    portioning_status = _sanitize_filter(portioning_status)

    # Default cooking_status
    if cooking_status is None:
        cooking_status = "completed"

    clauses = [
        "mpd.date >= :start_date",
        "mpd.date <= :end_date",
        "COALESCE(mprs.cooking_status, '') = :cooking_status",
    ]
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "cooking_status": cooking_status,
    }

    if recipe_id is not None:
        clauses.append("mpr.recipe_id = :recipe_id")
        params["recipe_id"] = recipe_id

    if delivery_slot_id is not None:
        clauses.append("d.delivery_slot_id = :delivery_slot_id")
        params["delivery_slot_id"] = delivery_slot_id

    if subrecipe_id is not None:
        clauses.append("mprs.subrecipe_id = :subrecipe_id")
        params["subrecipe_id"] = subrecipe_id

    if portioning_status is not None:
        clauses.append("COALESCE(mprs.portioning_status, '') = :portioning_status")
        params["portioning_status"] = portioning_status

    where_sql = " AND ".join(clauses)

    serving_ids_sql = f"""
        SELECT mprs.id AS id
        FROM meal_plan_day_recipe_serving mprs
        JOIN meal_plan_day_recipe mpr
            ON mprs.meal_plan_day_recipe_id = mpr.id
        JOIN meal_plan_day mpd
            ON mpr.meal_plan_day_id = mpd.id
        LEFT JOIN deliveries d
            ON mpd.delivery_id = d.id
        WHERE {where_sql}
        ORDER BY mpd.date
    """

    with engine.connect() as conn:
        rows = conn.execute(text(serving_ids_sql), params).mappings().all()

    serving_ids = [r["id"] for r in rows]

    if not serving_ids:
        return {"clients": [], "selection_stats": {"total_servings": 0, "ingredients": []}}

    return get_portioning_view_for_serving_ids(serving_ids)
