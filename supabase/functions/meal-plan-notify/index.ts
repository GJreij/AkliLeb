import { serve } from "https://deno.land/std@0.224.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY_MEALPLAN")!;
const ADMIN_EMAIL = Deno.env.get("ADMIN_EMAIL")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET");
const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);

async function sendEmail(to: string[], subject: string, html: string) {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${RESEND_API_KEY}`,
    },
    body: JSON.stringify({
      from: "Akli <noreply@akli-lb.org>",
      to,
      subject,
      html,
    }),
  });

  const text = await res.text();
  console.log("Resend status:", res.status);
  console.log("Resend body:", text);

  if (!res.ok) throw new Error(`Resend error ${res.status}: ${text}`);
}

function formatDate(dateStr: string | null | undefined) {
  if (!dateStr) return "—";
  const d = new Date(`${dateStr}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateStr;
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

function formatTime(timeStr: string | null | undefined) {
  if (!timeStr) return null;
  return timeStr.slice(0, 5); // "HH:MM:SS" -> "HH:MM"
}

const PAYMENT_LABELS: Record<string, string> = {
  cash: "Cash on delivery",
  whish: "Whish Money",
  neo: "Neopay",
};

function clientConfirmationHtml(opts: {
  firstName: string;
  orderId: number | string;
  dayCount: number;
  startDate: string | null;
  endDate: string | null;
  deliveryAddress: string | null;
  slotWindow: string | null;
  paymentMethod: string | null;
  totalAmount: number | null;
}) {
  const {
    firstName, orderId, dayCount, startDate, endDate,
    deliveryAddress, slotWindow, paymentMethod, totalAmount,
  } = opts;

  const paymentLabel = paymentMethod ? (PAYMENT_LABELS[paymentMethod] ?? paymentMethod) : "—";
  const totalLabel = totalAmount != null ? `$${totalAmount.toFixed(2)}` : "—";

  const row = (label: string, value: string) => `
    <tr>
      <td style="padding:10px 0;color:#5c5c5c;font-size:14px;border-top:1px solid #e0dbd5;">${label}</td>
      <td style="padding:10px 0;color:#1a1a1a;font-size:14px;font-weight:600;text-align:right;border-top:1px solid #e0dbd5;">${value}</td>
    </tr>
  `;

  return `
  <div style="background:#eee9e6;padding:32px 16px;font-family:Arial,Helvetica,sans-serif;">
    <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e0dbd5;">
      <div style="background:#063330;padding:28px 24px;text-align:center;">
        <div style="color:#ffffff;font-size:22px;font-weight:700;letter-spacing:0.5px;">AKLI</div>
      </div>
      <div style="padding:28px 24px;">
        <h2 style="margin:0 0 4px;color:#1a1a1a;font-size:20px;">Your order is confirmed 🎉</h2>
        <p style="margin:0 0 20px;color:#5c5c5c;font-size:14px;">Hi ${firstName || "there"}, thanks for ordering with Akli! Here's a summary of your meal plan.</p>

        <table style="width:100%;border-collapse:collapse;">
          ${row("Order #", String(orderId))}
          ${row("Meal plan", `${dayCount} day${dayCount === 1 ? "" : "s"} · ${formatDate(startDate)} – ${formatDate(endDate)}`)}
          ${row("Delivery address", deliveryAddress ?? "—")}
          ${slotWindow ? row("Delivery window", slotWindow) : ""}
          ${row("Payment method", paymentLabel)}
          ${row("Total", totalLabel)}
        </table>

        <div style="margin-top:24px;padding:14px 16px;background:#f2f9f9;border:1px solid #67b1b0;border-radius:8px;">
          <p style="margin:0;color:#063330;font-size:13px;">We'll start prepping your meals fresh and get them to you on schedule. Any questions or changes? Message us on WhatsApp and we'll take care of it.</p>
        </div>

        <div style="text-align:center;margin-top:22px;">
          <a href="https://wa.me/96181567192" style="display:inline-block;background:#063330;color:#ffffff;text-decoration:none;font-size:14px;font-weight:600;padding:12px 28px;border-radius:8px;">Chat with us on WhatsApp</a>
        </div>
      </div>
      <div style="padding:16px 24px;background:#f7f5f3;text-align:center;">
        <p style="margin:0;color:#a09890;font-size:11px;">Akli · Beirut → Jounieh meal delivery</p>
      </div>
    </div>
  </div>
  `;
}

serve(async (req) => {
  // 1) Verify webhook secret
  const gotSecret = req.headers.get("x-webhook-secret");
  if (WEBHOOK_SECRET && gotSecret !== WEBHOOK_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }

  // 2) Parse payload
  const payload = await req.json();
  const { type, schema, table, record } = payload;

  if (schema !== "public" || table !== "meal_plan" || type !== "INSERT") {
    return new Response("ignored", { status: 200 });
  }

  // 3) Fetch user details from the user table — including their declared
  // allergens, so we can flag it below if this order actually contains one.
  const ALLERGEN_KEYS = [
    "celery", "cereals_containing_gluten", "crustaceans", "eggs", "fish",
    "lupin", "milk", "molluscs", "sulphites", "mustard", "peanuts",
    "sesame", "soybeans", "tree_nuts",
  ] as const;
  const ALLERGEN_LABELS: Record<string, string> = {
    celery: "Celery", cereals_containing_gluten: "Gluten", crustaceans: "Crustaceans",
    eggs: "Eggs", fish: "Fish", lupin: "Lupin", milk: "Milk", molluscs: "Molluscs",
    sulphites: "Sulphites", mustard: "Mustard", peanuts: "Peanuts", sesame: "Sesame",
    soybeans: "Soybeans", tree_nuts: "Tree nuts",
  };

  const { data: user, error } = await supabase
    .from("user")
    .select(`name, last_name, email, phone_number, ${ALLERGEN_KEYS.join(", ")}`)
    .eq("id", record?.user_id)
    .single();

  if (error) {
    console.error("Failed to fetch user:", error.message);
  }

  const fullName = user
    ? `${user.name ?? ""} ${user.last_name ?? ""}`.trim()
    : "Unknown"; 

  // 3b) Resolve the actual delivery address used for this order — the user
  // table no longer carries a single delivery_address column; addresses now
  // live in user_delivery_address and get stamped per-order onto deliveries.
  let deliveryAddress: string | null = null;
  let slotWindow: string | null = null;
  const { data: days } = await supabase
    .from("meal_plan_day")
    .select("id, delivery_id, date")
    .eq("meal_plan_id", record?.id);

  const dayDateById = new Map<number, string>(
    (days ?? []).map((d: { id: number; date: string }) => [d.id, d.date])
  );

  const deliveryIds = (days ?? [])
    .map((d: { delivery_id: number | null }) => d.delivery_id)
    .filter((id: number | null): id is number => id !== null);

  if (deliveryIds.length > 0) {
    const { data: deliveryRows } = await supabase
      .from("deliveries")
      .select("delivery_address, delivery_slot_id")
      .in("id", deliveryIds)
      .not("delivery_address", "is", null)
      .limit(1);
    deliveryAddress = deliveryRows?.[0]?.delivery_address ?? null;

    const slotId = deliveryRows?.[0]?.delivery_slot_id;
    if (slotId) {
      const { data: slotRow } = await supabase
        .from("delivery_slots")
        .select("start_time, end_time")
        .eq("id", slotId)
        .single();
      if (slotRow) {
        const start = formatTime(slotRow.start_time);
        const end = formatTime(slotRow.end_time);
        slotWindow = start && end ? `${start} – ${end}` : null;
      }
    }
  }

  // Fallback in case this webhook fires before meal_plan_day/deliveries are
  // written yet — use the user's default saved address instead.
  if (!deliveryAddress) {
    const { data: defaultAddr } = await supabase
      .from("user_delivery_address")
      .select("address_text")
      .eq("user_id", record?.user_id)
      .eq("is_default", true)
      .limit(1)
      .single();
    deliveryAddress = defaultAddr?.address_text ?? null;
  }

  // 3c) Resolve the payment method used for this order, so the admin can
  // reach out to the client about it if needed.
  let paymentProvider: string | null = null;
  let totalAmount: number | null = null;
  const mealPlanDayIds = (days ?? []).map((d: { delivery_id: number | null; id?: number }) => d.id);
  if (mealPlanDayIds.length > 0) {
    const { data: paymentRows } = await supabase
      .from("payment")
      .select("provider, amount")
      .in("meal_plan_day_id", mealPlanDayIds);
    paymentProvider = paymentRows?.find(p => p.provider)?.provider ?? null;
    if (paymentRows && paymentRows.length > 0) {
      totalAmount = paymentRows.reduce((sum, p) => sum + (Number(p.amount) || 0), 0);
    }
  }

  // 3d) Check this order's dishes against the client's declared allergens —
  // alert-only, nothing here blocks or delays the order; it just makes sure
  // a human sees it immediately if it happens. Same recipe_allergen view
  // used by every customer-facing surface (My Tastes, menu, order review),
  // so this can never disagree with what the client themselves was shown.
  let allergenConflictHtml = "";
  const userAllergenFlags = (user ?? {}) as Record<string, boolean | null>;
  const declaredAllergens = ALLERGEN_KEYS.filter(k => userAllergenFlags[k]);
  if (declaredAllergens.length > 0 && mealPlanDayIds.length > 0) {
    const { data: dayRecipes, error: dayRecipesError } = await supabase
      .from("meal_plan_day_recipe")
      .select("recipe_id, meal_plan_day_id, label, recipe:recipe_id(name)")
      .in("meal_plan_day_id", mealPlanDayIds);
    // Errors here previously vanished silently: dayRecipes would fall back
    // to [] below and the allergen section would just be omitted from the
    // email with nothing to say the check didn't actually run.
    if (dayRecipesError) console.error("Failed to fetch day recipes for allergen check:", dayRecipesError.message);

    type DayRecipeRow = {
      recipe_id: number;
      meal_plan_day_id: number;
      label: string | null;
      recipe: { name: string | null } | { name: string | null }[] | null;
    };
    const dayRecipeRows = (dayRecipes ?? []) as DayRecipeRow[];
    const recipeIds = Array.from(new Set(dayRecipeRows.map(r => r.recipe_id)));
    if (recipeIds.length > 0) {
      const { data: allergenRows, error: allergenRowsError } = await supabase
        .from("recipe_allergen")
        .select("*")
        .in("recipe_id", recipeIds);
      if (allergenRowsError) console.error("Failed to fetch recipe_allergen rows for allergen check:", allergenRowsError.message);

      const conflictKeysByRecipe = new Map<number, string[]>();
      for (const row of allergenRows ?? []) {
        const flags = row as Record<string, boolean | null> & { recipe_id: number };
        const hit = declaredAllergens.filter(k => flags[k]);
        if (hit.length > 0) conflictKeysByRecipe.set(flags.recipe_id, hit);
      }

      if (conflictKeysByRecipe.size > 0) {
        const dishName = (r: DayRecipeRow) => {
          const embedded = Array.isArray(r.recipe) ? r.recipe[0] : r.recipe;
          return r.label || embedded?.name || `Recipe #${r.recipe_id}`;
        };
        const lines = dayRecipeRows
          .filter(r => conflictKeysByRecipe.has(r.recipe_id))
          .map(r => {
            const keys = conflictKeysByRecipe.get(r.recipe_id)!;
            const day = dayDateById.get(r.meal_plan_day_id) ?? "—";
            const labels = keys.map(k => ALLERGEN_LABELS[k] ?? k).join(", ");
            return { day, html: `<li><b>${dishName(r)}</b> (${day}) — contains ${labels}</li>` };
          })
          .sort((a, b) => a.day.localeCompare(b.day))
          .map(l => l.html);
        allergenConflictHtml = `
          <div style="background:#fff3e8;border:1px solid #f0b87a;border-radius:8px;padding:12px 16px;margin-bottom:16px;">
            <h3 style="margin:0 0 6px;color:#c45f00;">⚠️ Allergen conflict</h3>
            <p style="margin:0 0 8px;">${fullName} has declared: <b>${declaredAllergens.map(k => ALLERGEN_LABELS[k] ?? k).join(", ")}</b></p>
            <ul style="margin:0;">${lines.join("")}</ul>
          </div>
        `;
      }
    }
  }

  // 4) Build and send email
  const subject = allergenConflictHtml
    ? `⚠️ Allergen conflict — New Akli Order — ${fullName}`
    : `🥗 New Akli Order — ${fullName}`;
  const html = `
    ${allergenConflictHtml}
    <h2>New meal plan order</h2>

    <h3>👤 Client</h3>
    <ul>
      <li><b>Name:</b> ${fullName}</li>
      <li><b>Email:</b> ${user?.email ?? "—"}</li>
      <li><b>Phone:</b> ${user?.phone_number ?? "—"}</li>
    </ul>

    <h3>📦 Delivery</h3>
    <ul>
      <li><b>Address:</b> ${deliveryAddress ?? "—"}</li>
    </ul>

    <h3>💳 Payment</h3>
    <ul>
      <li><b>Method:</b> ${paymentProvider ?? "—"}</li>
    </ul>

    <h3>📋 Plan Details</h3>
    <ul>
      <li><b>Plan ID:</b> ${record?.id ?? "—"}</li>
      <li><b>Start date:</b> ${record?.start_date ?? "—"}</li>
      <li><b>End date:</b> ${record?.end_date ?? "—"}</li>
      <li><b>Created at:</b> ${record?.created_at ?? "—"}</li>
    </ul>
  `;

  await sendEmail([ADMIN_EMAIL], subject, html);

  // 5) Send the client their own order confirmation — best-effort: if this
  // fails, the order itself already succeeded and the admin was already
  // notified above, so we don't want a client-email hiccup to surface as an
  // error on this webhook.
  if (user?.email) {
    try {
      const clientHtml = clientConfirmationHtml({
        firstName: user.name ?? "",
        orderId: record?.id ?? "—",
        dayCount: (days ?? []).length,
        startDate: record?.start_date ?? null,
        endDate: record?.end_date ?? null,
        deliveryAddress,
        slotWindow,
        paymentMethod: paymentProvider,
        totalAmount,
      });
      await sendEmail([user.email], "Your Akli order is confirmed 🎉", clientHtml);
    } catch (e) {
      console.error("Failed to send client confirmation email:", e instanceof Error ? e.message : e);
    }
  }

  return new Response("ok", { status: 200 });
});