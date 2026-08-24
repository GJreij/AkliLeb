# Akli Meal Planner API

Flask backend for the Akli meal-prep app: pricing, the LP meal-plan solver, checkout, order
confirmation, and kitchen-ops views (cooking, portioning, packaging, deliveries).

- **Base URL (prod):** `https://aklilebapp-72376dbe3cc8.herokuapp.com`
- **Auth:** none. No route checks a token, API key, or session — every endpoint trusts whatever
  `user_id` is passed in the request body/query string. Authorization happens entirely on the
  Supabase (RLS) side of the stack that calls in; this API itself has no gate.
- **CORS:** wide open — `app.py` registers `CORS(app, resources={r"/*": {"origins": "*"}})`.
- **Content type:** JSON in, JSON out, except the four `GET` endpoints that take query params.
- **Errors:** no consistent envelope. Most routes return `{"error": "..."}` with a 4xx/5xx status;
  a few (`/confirm_order`, `/available_recipes_for_date`) add `"missing_fields"` or `"details"`.
  Treat any non-2xx body as "has an `error` key" rather than a fixed shape.
- **Frontend contract:** [`FrontEnd/akli-web/src/lib/flask.ts`](../../FrontEnd/akli-web/src/lib/flask.ts)
  has hand-written TypeScript types for most of these. See **Known discrepancies** below for where
  it's drifted from what the API actually does.

## Contents

- [Known discrepancies vs. the frontend client](#known-discrepancies-vs-the-frontend-client)
- [Pricing & macros](#pricing--macros)
  - [`GET /macros`](#get-macros)
  - [`GET /macros/ui-price`](#get-macrosui-price)
  - [`POST /macros/from-grams`](#post-macrosfrom-grams)
  - [`POST /simple_price_simulator`](#post-simple_price_simulator)
- [Meal planning](#meal-planning)
  - [`POST /generate_meal_plan`](#post-generate_meal_plan)
  - [`POST /check_meal_plan_conflict`](#post-check_meal_plan_conflict)
  - [`POST /update_meal_plan`](#post-update_meal_plan)
  - [`POST /available_recipes_for_date`](#post-available_recipes_for_date)
- [Checkout & orders](#checkout--orders)
  - [`POST /checkout_summary`](#post-checkout_summary)
  - [`POST /confirm_order`](#post-confirm_order)
- [Client-facing](#client-facing)
  - [`GET /client/upcoming_recipes`](#get-clientupcoming_recipes)
- [Kitchen operations](#kitchen-operations)
  - [`GET /ingredients-to-buy`](#get-ingredients-to-buy)
  - [`POST /cooking/overview`](#post-cookingoverview)
  - [`POST /portioning/summary`](#post-portioningsummary)
  - [`POST /packaging`](#post-packaging)
  - [`POST /deliveries/overview`](#post-deliveriesoverview)
- [`GET /`](#get-)

---

## Known discrepancies vs. the frontend client

`flask.ts` is the closest thing this API had to documentation before this file — it's mostly
accurate, but it drifted in a few places. Worth fixing on whichever side is wrong:

| Discrepancy | Where | Impact |
|---|---|---|
| **`/simple_price_simulator` returns more than its type declares.** The real response also includes `avg_week_price` and an `inputs` echo block; `PriceSimulatorResponse` in `flask.ts` only types `avg_day_price` and `breakdown`. | [`routes/price_simulator.py`](routes/price_simulator.py) vs. `flask.ts:14-24` | Harmless (extra fields are just untyped), but `avg_week_price` is unusable from the frontend without a cast. |
| **`/checkout_summary` returns much more than `CheckoutSummaryResponse` types.** Missing from the TS type: top-level `weekly_accuracy`, `price_breakdown.weekly_price`, `price_breakdown.affiliate_id`, `price_breakdown.commission_rate`, and the raw per-gram prices (`protein_price_per_g` etc.) in `price_breakdown`. | [`routes/checkout_summary.py`](routes/checkout_summary.py) vs. `flask.ts:143-180` | `weekly_price` is the field meant to be shown to the customer as *the* price per the code comments — it's currently only reachable from the frontend via an unsafe cast. |
| **`macros_bp` (3 routes) has no frontend client at all.** `/macros`, `/macros/ui-price`, `/macros/from-grams` are never called from `flask.ts`. | [`routes/macros_routes.py`](routes/macros_routes.py) | Either dead code, or called from somewhere outside `akli-web` (worth confirming which). |
| **`/client/upcoming_recipes` and `/available_recipes_for_date` have no frontend client.** | [`routes/client_meals.py`](routes/client_meals.py), [`routes/get_available_recipes.py`](routes/get_available_recipes.py) | Same as above — confirm whether these are used by something else (admin tooling? a future screen?) or can be removed. |
| **`ingredient_id` filter on `/cooking/overview` is accepted but silently ignored.** The route builds it into the `filters` dict, but `get_cooking_overview()` in the service never reads `filters["ingredient_id"]` — every other filter key is applied via `apply_null_filter`, this one isn't. | [`routes/cooking.py:41`](routes/cooking.py) vs. [`services/cooking_service.py`](services/cooking_service.py) | Passing `ingredient_id` from a caller silently does nothing — no error, no effect. |
| **`/cooking/overview` shifts dates by +1 day before querying.** `start_date`/`end_date` are documented nowhere as meaning "cooking date," but the route converts them to "eating date" (`+ timedelta(days=1)`) before the query runs. | [`routes/cooking.py:18-20`](routes/cooking.py) | Callers must pass the *cooking* date range, not the delivery/eating range — easy to get backwards without this doc. |

---

## Pricing & macros

### `GET /macros`

Diet-based macro split + price estimate for a given calorie target. No `user_id` — pure calculator.

**Query params**

| Param | Type | Required | Notes |
|---|---|---|---|
| `kcal` | float | yes | Must be > 0 |
| `diet` | string | yes | One of `high_protein`, `balanced`, `low_fat`, `high_carbs` |
| `meals_per_day` | int | no | Default 3 |
| `avg_subrecipes_per_meal` | float | no | Default 3 |
| `apply_kcal_discount` | bool | no | Default `true` |

**200 response**

```json
{
  "diet_type": "balanced",
  "kcal": 2200.0,
  "macros_percentage": { "protein": 25, "carbs": 45, "fat": 30 },
  "macros_grams": { "protein": 137.5, "carbs": 247.5, "fat": 73.3 },
  "price_estimate": {
    "estimated_day_price": 23.14,
    "assumptions": { "meals_per_day": 3, "avg_subrecipes_per_meal": 3, "apply_kcal_discount": true },
    "breakdown": {
      "base_macro_cost": 18.02,
      "kcal_discount_pct": 0.11,
      "macro_cost_after_discount": 16.04,
      "day_packaging_cost": 1.5,
      "recipes_packaging_cost": 3.0,
      "subrecipes_packaging_cost": 2.6
    },
    "prices_used": { "protein_price_per_g": 0.04, "carbs_price_per_g": 0.02, "...": "..." }
  }
}
```

**Errors:** `400` bad/missing `kcal`, unknown `diet`, bad numeric knobs. `500` if pricing can't be
fetched from `macro_price`.

The kcal discount (`get_kcal_discount` in `pricing_service.py`) ramps linearly from **0% at 1200
kcal to 22% at 3000 kcal** — the same curve is shared by every pricing endpoint below.

---

### `GET /macros/ui-price`

Same macro-split calculation as `/macros`, but returns UI-friendly rounded **ranges** for a
3-meals-+-1-snack day instead of one exact figure — this is what a pricing screen should render.

**Query params:** `kcal` (500–3500, required), `diet` (required), `avg_subrecipes_per_meal`,
`snack_kcal_share` (default 0.20), `snack_subrecipes` (default 1.0), `apply_kcal_discount`.

**200 response**

```json
{
  "diet_type": "balanced",
  "kcal": 2200.0,
  "macros_percentage": { "protein": 25, "carbs": 45, "fat": 30 },
  "macros_grams": { "protein": 137.5, "carbs": 247.5, "fat": 73.3 },
  "ui_price": {
    "scenario": { "meals": 3, "snacks": 1, "containers": 4 },
    "ranges": {
      "day": { "low": 22, "high": 25 },
      "week": { "low": 155, "high": 175 },
      "per_meal_avg": { "low": 6, "high": 7 }
    },
    "exact": { "day": 23.5, "week": 164.5, "avg_per_container": 5.88 },
    "ui_copy": {
      "headline": "For a day of 3 meals and 1 snack:",
      "day": "~ $22–25 / day",
      "per_meal": "(≈ $6–7 per meal on average)",
      "week": "~ $155–175 / week",
      "note": "Meals vary in size and macros. Pricing is based on total daily nutrition, not individual dishes."
    },
    "assumptions": { "avg_subrecipes_per_meal": 3, "snack_kcal_share": 0.2, "snack_subrecipes": 1.0, "apply_kcal_discount": true },
    "day_estimate_debug": { "...": "full estimate_day_price() output, kept for debugging" }
  }
}
```

Ranges are computed by `_band()`: `±6%` (min width `$3`) for day/week, `±8%` (min width `$1`) for
per-meal — not a fixed `±$X`, so the band widens with price.

---

### `POST /macros/from-grams`

Reverse of `/macros` — takes exact grams instead of a diet preset, validates the resulting macro
split falls in a sane range, and returns the same price-estimate shape.

**Body**

```json
{
  "protein": 150, "carbs": 200, "fat": 60,
  "meals_per_day": 3, "avg_subrecipes_per_meal": 1.5, "apply_kcal_discount": true
}
```

`meals_per_day`, `avg_subrecipes_per_meal`, `apply_kcal_discount` are optional (same defaults as
`/macros`).

**200 response**

```json
{
  "total_kcal": 1580,
  "macros_grams": { "protein": 150, "carbs": 200, "fat": 60 },
  "macros_percentage": { "protein": 38.0, "carbs": 50.6, "fat": 34.2 },
  "kcal_breakdown": { "protein": 600, "carbs": 800, "fat": 540 },
  "price_estimate": { "...": "same shape as /macros" }
}
```

**Errors:** `400` if a macro is non-numeric or ≤ 0, or if the resulting percentages fall outside
`MACRO_RANGES` (protein 10–55%, carbs 20–65%, fat 15–40% — see `config/constants.py`) — the error
body lists each offending macro under `"details"`. `500` if pricing lookup fails.

---

### `POST /simple_price_simulator`

Standalone per-day price calculator from raw grams — same math as `/macros/from-grams` but
without the macro-sanity check, and it also returns a weekly figure.

**Body** — all fields required except `apply_kcal_discount`:

```json
{
  "protein_g": 150, "carbs_g": 200, "fat_g": 60,
  "meals_per_day": 3, "avg_subrecipes_per_meal": 1.5,
  "apply_kcal_discount": true
}
```

**200 response**

```json
{
  "inputs": { "protein_g": 150, "carbs_g": 200, "fat_g": 60, "meals_per_day": 3, "avg_subrecipes_per_meal": 1.5, "estimated_kcal": 1580, "apply_kcal_discount": true },
  "avg_day_price": 23.14,
  "avg_week_price": 161.98,
  "breakdown": {
    "prices_used": { "protein_price_per_g": 0.04, "...": "..." },
    "base_macro_cost": 18.02,
    "kcal_discount_pct": 0.0,
    "macro_cost_after_discount": 18.02,
    "day_packaging_cost": 1.5,
    "recipes_packaging_cost": 3.0,
    "subrecipes_packaging_cost": 2.6
  }
}
```

> `avg_week_price` is `avg_day_price * 7` — this endpoint only ever sees one day's macros, so the
> weekly number is an extrapolation (7 identical days), not a solved week. See the "Task 3" comment
> in `checkout_summary.py` for the real weekly price, which sums actual grams across a solved plan.

**Errors:** `400` missing/invalid field types, negative macros, `meals_per_day <= 0`. `500` if
pricing lookup fails.

---

## Meal planning

### `POST /generate_meal_plan`

Runs the LP solver (PuLP) to build a day-by-day meal plan for a user across a date range. This is
the core endpoint — it re-fetches the user's saved macro target and recipe catalog from Supabase
itself using `user_id`; the caller only supplies the date range and a few knobs.

**Body**

| Field | Type | Required | Notes |
|---|---|---|---|
| `user_id` | string | yes | Must have a `daily_macro_target` row already saved |
| `start_date`, `end_date` | `"YYYY-MM-DD"` | yes | `end_date >= start_date` |
| `include_weekends` | bool | no | Default `false` |
| `meals` | object | no | e.g. `{"breakfast": "breakfast"}` — restricts which meal slots are solved. Default: all four (`breakfast`, `lunch`, `snack`, `dinner`) |
| `kcal_override` | number | no | Client-computed reduced daily target, e.g. when the user is "eating out" for an excluded meal |
| `kitchen_id` | number | no | Scopes the kitchen-closure check to one kitchen |

**200 response** — shape matches `GenerateMealPlanResponse` in `flask.ts`:

```json
{
  "user_id": "...",
  "start_date": "2026-08-25",
  "end_date": "2026-08-29",
  "daily_macro_target": { "protein_g": 137.5, "carbs_g": 247.5, "fat_g": 73.3, "kcal": 2200 },
  "excluded_dates": [],
  "days": [
    {
      "date": "2026-08-25",
      "weekday": 1,
      "is_weekend": false,
      "macro_error": 0.031,
      "totals": { "protein": 136, "carbs": 244, "fat": 74, "kcal": 2196 },
      "meals": [
        {
          "meal_key": "breakfast",
          "meal_type": "breakfast",
          "recipe_id": 42,
          "recipe_name": "Shakshuka Bowl",
          "photo": "https://...",
          "macros": { "protein": 30, "carbs": 40, "fat": 15, "kcal": 405 },
          "subrecipes": [ { "subrecipe_id": 7, "name": "Poached Eggs", "servings": 2, "macros": { "...": "..." } } ]
        }
      ],
      "adjusted_target": { "protein_g": 140.1, "carbs_g": 250.2, "fat_g": 72.9, "kcal": 2210 }
    }
  ],
  "plan_summary": [
    { "start_date": "2026-08-25", "end_date": "2026-08-31", "used": [ { "recipe_id": 42, "recipe_name": "Shakshuka Bowl", "times_used": 2 } ], "not_used": [ { "recipe_id": 88, "recipe_name": "Beef Bowl" } ] }
  ]
}
```

`adjusted_target` (not in `flask.ts`'s `PlanDay` type, but present on every day) is the
carryover-adjusted macro target actually solved against for that day — persisted so
`/update_meal_plan` can carry it forward instead of reverting to the flat target.

**Errors:**
- `400` — missing `user_id`, bad dates, invalid `meals` map, all candidate dates fall in a kitchen
  closure (`"error": "kitchen_closed"`), or every recipe is excluded by the user's own
  `dont_include` preferences (`"error": "All recipes were excluded by user preferences"`).
- `404` — no `weekly_menu` covers the range, no recipes inside those menus, no recipes available
  for a specific date, or the solver couldn't find enough unique recipes for a specific day
  (`"error": "Not enough unique recipes for this day", "date": "..."`).

**Solver behavior worth knowing:**
- Users whose daily kcal target exceeds **2800** (`HIGH_KCAL_THRESHOLD`) bypass the shared
  cross-client `daily_menu` template entirely and always get a freshly personalized day — never
  written back to the shared template, so it can't corrupt it for normal-calorie clients.
- Weekly-carryover deviation resets every 7 calendar days from the range's own start date, and is
  measured against the flat base target, never the carryover-adjusted one (both were live bugs,
  fixed 2026-08-18 — see the comments in `mealplan_routes.py` around lines 330–430 if this ever
  regresses).
- Past 7 requested days, only one repair trial type (`dinner`) runs instead of two — a real 10-day
  request with both trial types enabled measured 32–36s against Heroku's 30s hard timeout.

---

### `POST /check_meal_plan_conflict`

Checks whether a user already has a `meal_plan` overlapping a proposed date range. Pure read —
no solving, no writes.

**Body:** `user_id` (required), `start_date`, `end_date` (both `YYYY-MM-DD`, required).

**200 response**

```json
{
  "has_conflict": true,
  "conflicts": [ { "id": 501, "start_date": "2026-08-26", "end_date": "2026-08-30", "created_at": "..." } ],
  "selected": { "start_date": "2026-08-25", "end_date": "2026-08-29" }
}
```

**Errors:** `400` missing `user_id`, bad dates, or `end_date < start_date`.

---

### `POST /update_meal_plan`

Applies targeted edits (swap a recipe, delete a meal, add a meal) to an already-generated plan
without re-solving the whole range.

**Body**

```json
{
  "original_plan": { "...": "a full GenerateMealPlanResponse, as returned by /generate_meal_plan" },
  "change_logs": [
    {
      "date": "2026-08-26",
      "created_at": "2026-08-24T10:00:00Z",
      "meal_key": "dinner",
      "old_recipe_id": 42,
      "new_recipe_id": 88,
      "include_macros_in_rest": true
    }
  ]
}
```

`change_logs[].Delete: true` removes a meal entirely (day-scoped, omit `meal_key` for a full-day
delete). A new meal with no `old_recipe_id` requires `meal_type`.
`include_macros_in_rest: false` means "eating out" — reduces that day's target instead of
redistributing the meal's macros to the rest of the day.

**200 response:** same shape as `/generate_meal_plan`'s 200 response, recomputed for the affected
day(s).

**Errors:** `400` if `original_plan` is missing or `change_logs` isn't a list.

---

### `POST /available_recipes_for_date`

Returns which recipe IDs are available to cook on a given calendar date, based purely on
`weekly_menu` date coverage (no user/preferences involved).

**Body:** `date` (`YYYY-MM-DD`, required), `tenant_id` (int, optional).

**200 response**

```json
{
  "date": "2026-08-25",
  "tenant_id": null,
  "weekly_menu_ids": [12],
  "recipe_ids": [3, 7, 9, 42, 88],
  "count": 5
}
```

If no `weekly_menu` covers the date, this still returns `200` with `"recipe_ids": []` and a
`"message"` field explaining why — not a `404`.

**Errors:** `400` missing `date`. `500` unexpected failure (`"details"` includes the exception
string).

---

## Checkout & orders

### `POST /checkout_summary`

Prices a generated plan: per-gram macro cost, packaging, automatic volume discount, promo code,
delivery fee, and both a per-day (ops) and single weekly (customer-facing) total.

**Body:** `user_id` (required), `final_plan` (required — a `GenerateMealPlanResponse`),
`promo_code` (optional string).

**200 response** (trimmed for length — see `routes/checkout_summary.py` STEP 6 for the exact
builder):

```json
{
  "user_id": "...",
  "total_meals": 20,
  "macro_summary": { "avg_kcal": 2198.4, "avg_protein": 136.2, "avg_carbs": 245.8, "avg_fat": 73.6 },
  "weekly_accuracy": { "protein_pct": 99, "carbs_pct": 101, "fat_pct": 98, "kcal_pct": 100 },
  "price_breakdown": {
    "protein_price_per_g": 0.04,
    "carbs_price_per_g": 0.02,
    "fat_price_per_g": 0.03,
    "day_packaging_price": 1.5,
    "recipe_packaging_price": 1.0,
    "subrecipe_packaging_price": 0.5,

    "total_price_before_discount": 178.4,
    "discount_amount": 13.9,
    "final_price_before_delivery": 164.5,

    "volume_discount": { "amount": 8.9, "rule_name": "5+ days", "min_order_days": 5 },
    "promo_discount_amount": 5.0,

    "delivery": {
      "fee_per_day": 3.5, "minimum_per_day_for_free_delivery": 25,
      "delivery_days": 1, "delivery_fee": 3.5,
      "is_free_delivery": false, "waived_by_promo": false
    },

    "final_price": 168.0,
    "promo_code_status": "valid", "promo_code_used": "WELCOME10",
    "promo_message": "10% off applied", "promo_code_id": 4,
    "affiliate_id": null, "commission_rate": null,

    "weekly_price": {
      "price_before_discount": 178.4, "discount_amount": 13.9,
      "price_before_delivery": 164.5, "delivery_fee": 3.5, "final_price": 168.0
    },

    "daily_breakdown": [
      { "date": "2026-08-25", "total_price": 23.5, "original_total_price": 26.9, "meals": 4, "delivery_applied": false, "delivery_fee": 0, "total_price_with_delivery": 23.5 }
    ]
  }
}
```

**Errors:** `400` missing `user_id`/`final_plan`, or an empty plan (`"error": "Plan is empty"`).
`500` if pricing can't be fetched.

**Pricing rules worth knowing:**
- **Weekly price is the number meant for the customer** (`price_breakdown.weekly_price.final_price`)
  — it's computed once from grams summed across the whole plan, not by adding up daily prices.
  `daily_breakdown` still exists and is accurate, but it's kept for kitchen/ops use.
- **Volume discount and promo code stack multiplicatively, not additively** — the promo percentage
  is computed on the price *after* the volume discount is applied, so two 10% deals compound to
  19% off, not 20%. If a volume rule has `stackable_with_promo: false`, the promo wins exclusively
  and the volume discount is voided entirely (and the promo is then recomputed against the
  original price).
- **Delivery eligibility is based on the pre-discount daily total** (`< $25`/day, `DELIVERY_DAY_MINIMUM`)
  — a promo code can waive delivery outright via `waives_delivery` on the promo record, independent
  of that per-day threshold.

---

### `POST /confirm_order`

Writes the plan to the database: `meal_plan` → `meal_plan_day` → `meal_plan_day_recipe` →
`meal_plan_day_recipe_serving`, plus `deliveries`, `user_delivery_preference`, and one `payment`
row per day. Also fires an admin notification email via a Supabase edge function.

**Body**

| Field | Required | Notes |
|---|---|---|
| `user_id` | yes | |
| `meal_plan` | yes | The plan being ordered (`GenerateMealPlanResponse`, possibly edited via `/update_meal_plan`) |
| `checkout_summary` | yes | The response from `/checkout_summary` for this exact plan — its `daily_breakdown` and `affiliate_id`/`commission_rate` are what get written to `payment` rows |
| `delivery_slot_id` | yes | |
| `delivery_address_id` **or** `delivery_address` | one required | `_id` looks up a saved `user_delivery_address` row (must belong to `user_id`); `delivery_address` is a free-text override. If neither resolves to an address (including no default address on file), the order is rejected |
| `payment_method` | no | `"cash"` \| `"whish"` \| `"neo"` — stored as-is on each `payment` row, not validated against this list server-side |

**200 response**

```json
{ "success": true, "message": "Order successfully confirmed.", "order_id": 501 }
```

`order_id` is the newly created `meal_plan.id`. (Fixed 2026-08-24 — this endpoint used to omit it
entirely, which meant every `order_confirmed` analytics event logged `order_id: null`.)

**Errors:**
- `400` — missing required fields (`"missing_fields"` lists which), delivery slot not found or
  missing `start_time`, more than 2 requested delivery days are fully booked
  (`"full_days"` lists which), or no delivery address could be resolved.
- `500` — unexpected failure, with `"details"` containing the exception string.

**Delivery-day mapping:** a delivery slot's `start_time` determines AM vs. PM. **AM slots deliver
on the same calendar day as the meal; PM slots deliver the evening before.** This is why the
frontend's 48h order-lead-time calculation (`beirutISODate` in `order/new/page.tsx`) exists —
without it, a PM-slot order placed same-day would need a delivery that already happened.

---

## Client-facing

### `GET /client/upcoming_recipes`

A signed-in user's ordered meals in a date window, with per-day pricing and delivery status —
what a "my upcoming meals" screen would render.

**Query params:** `user_id` (required), `from`, `to` (both optional — default window is **3 days
in the past to 7 days in the future** from today).

**200 response**

```json
{
  "user_id": "...",
  "from": "2026-08-21",
  "to": "2026-08-31",
  "has_orders": true,
  "days": [
    {
      "date": "2026-08-25",
      "delivery": { "delivery_date": "2026-08-24", "delivery_time": "18:00-20:00", "status": "pending" },
      "totals": { "kcal": 2196, "protein": 136, "carbs": 244, "fat": 74 },
      "price": 24,
      "recipes": [ { "meal_type": "breakfast", "recipe_id": 42, "recipe_name": "Shakshuka Bowl", "kcal": 405, "protein": 30, "carbs": 40, "fat": 15 } ]
    }
  ]
}
```

**Errors:** `400` missing `user_id`.

---

## Kitchen operations

These five endpoints are ops/kitchen-facing (cooking, portioning, packaging, purchasing,
delivery routing) — no `user_id`-based authorization, scoped only by date range and optional
filters.

### `GET /ingredients-to-buy`

Aggregated shopping list for a date range.

**Query params:** `start_date`, `end_date` (required). Optional filters: `recipe`, `client`,
`delivery_slot` (all treated as unset if empty string, `"null"`, or `"none"`).

**200 response:** array of `{ "ingredient_id": 3, "name": "Chicken breast", "unit": "g", "total_quantity": 4200.0 }`.

**Errors:** `400` missing dates. `500` on failure.

---

### `POST /cooking/overview`

Everything the kitchen needs to cook a date range: recipes, their subrecipes, aggregated
ingredient quantities, progress, and any client comments left on those recipes.

**Body:** `start_date`, `end_date` (required — **see the date-shift note below**). Optional
filters: `client_id`, `delivery_slot_id`, `recipe_id`, `subrecipe_id`, `cooking_status`
(`ingredient_id` is also accepted but currently has no effect — see
[Known discrepancies](#known-discrepancies-vs-the-frontend-client)). Each filter accepts
`"null"` / `"not_null"` as special values, in addition to a literal id.

> **Date shift:** the route adds **+1 day** to both `start_date` and `end_date` before querying,
> to convert a *cooking* date into an *eating* date. Pass the range you want to cook for, not the
> range you want to deliver in.

**200 response:** array of recipes, shape matches `CookingRecipe` in `flask.ts` — each with
`subrecipes[]` (with their own `ingredients_needed[]` and `status`/`progress`) and `comments[]`
pulled from `user_recipe_preferences.comment`.

**Errors:** `400` missing dates.

---

### `POST /portioning/summary`

Per-client portioning breakdown for one subrecipe batch, plus the total ingredient quantities
needed for that batch.

**Body:** `subrecipe_id` (required, int), `meal_plan_day_recipe_ids` (required — int, list of
ints, or comma-separated string), `cooking_status` (default `"completed"`).

**200 response:** matches `PortioningSummary` in `flask.ts` — `subrecipe`, `summary` (batch totals
+ per-ingredient breakdown), `clients[]` (one row per serving, with delivery date/slot/client and
`weight_after_cooking`).

**Errors (all `400`):**
- Plain string error, e.g. `"subrecipe_id is required"`, `"No servings found"`.
- **Partial-batch mismatch** returns a structured error instead of a string —
  `{"error": "Subrecipe missing in some MPDRs", "missing": [...], "extra_found": [...]}` — when
  some but not all of the requested `meal_plan_day_recipe_ids` have a serving row at the given
  `cooking_status`. Matches `PortioningPartialError` in `flask.ts`.
- If servings exist at some *other* status, the message is specifically
  `"This subrecipe hasn't been marked as cooked yet — mark it cooked before portioning"`.

---

### `POST /packaging`

Per-delivery-slot, per-client packaging checklist for a date range.

**Body:** `start_date`, `end_date` (required).

**200 response:** array of `{ "delivery_date": ..., "slots": [ { "slot_id", "start_time", "end_time", "clients": [ { "name", "last_name", "recipes": [ { "meal_plan_day_recipe_id", "meal_type", "recipe_name", "packaging_status", "subrecipes": [...] } ] } ] } ] }` — matches `PackagingDay` in `flask.ts`.

**Errors:** `400` missing dates.

---

### `POST /deliveries/overview`

Per-delivery rows for a date range — client, address, payment status, and a Google Maps link
derived from the client's saved address lat/lng (matched by address text, falling back to their
default address).

**Body:** `start_date`, `end_date` (required).

**200 response:** array matching `DeliveryRow` in `flask.ts` — sorted by
`(delivery_date, slot.start_time, client.name)`. `payment.collect_cash` is `true` only when
`provider == "cash"` and `status != "paid"`.

**Errors:** `400` missing dates.

---

## `GET /`

Liveness check — returns the plain string `"Hello from Flask API on Heroku!!"`, not JSON. Used
to confirm the dyno is up, nothing else.
