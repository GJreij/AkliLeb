# services/weekly_menu_service.py

from utils.supabase_client import supabase
from datetime import datetime


class WeeklyMenuService:
    def __init__(self):
        self.sb = supabase

    def get_available_recipes_for_date(self, date_str: str, tenant_id=None):
        """
        Returns full recipe rows available for a given date.
        Date filtering is done ONLY on weekly_menu.
        """

        # 1) Validate date
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return {
                "error": "Invalid date format. Use YYYY-MM-DD.",
                "date_received": date_str
            }, 400

        # 2) Weekly menus covering this date
        q = (
            self.sb.table("weekly_menu")
            .select("id")
            .lte("week_start_date", date_str)
            .gte("week_end_date", date_str)
        )

        if tenant_id is not None:
            q = q.eq("tenant_id", tenant_id)

        menu_res = q.execute()
        menus = menu_res.data or []

        if not menus:
            return {
                "date": date_str,
                "tenant_id": tenant_id,
                "recipes": [],
                "count": 0
            }, 200

        weekly_menu_ids = [m["id"] for m in menus]

        # 3) Get recipe_ids from weekly_menu_recipe
        wmr_res = (
            self.sb.table("weekly_menu_recipe")
            .select("recipe_id")
            .in_("weekly_menu_id", weekly_menu_ids)
            .execute()
        )

        recipe_ids = list({
            r["recipe_id"]
            for r in (wmr_res.data or [])
            if r.get("recipe_id") is not None
        })

        if not recipe_ids:
            return {
                "date": date_str,
                "tenant_id": tenant_id,
                "recipes": [],
                "count": 0
            }, 200

        # 4) Fetch FULL recipe rows
        recipes_res = (
            self.sb.table("recipe")
            .select("*")
            .in_("id", recipe_ids)
            .execute()
        )

        recipes = recipes_res.data or []

        return {
            "date": date_str,
            "tenant_id": tenant_id,
            "recipes": recipes,
            "count": len(recipes)
        }, 200
