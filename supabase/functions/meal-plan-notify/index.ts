import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY_MEALPLAN")!;
const ADMIN_EMAIL = Deno.env.get("ADMIN_EMAIL")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET"); // recommended

async function sendEmail(subject: string, html: string) {
  const res = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${RESEND_API_KEY}`,
    },
    body: JSON.stringify({
      from: "Akli <onboarding@resend.dev>",
      to: [ADMIN_EMAIL],
      subject,
      html,
    }),
  });

  const text = await res.text();
  console.log("Resend status:", res.status);
  console.log("Resend body:", text);

  if (!res.ok) throw new Error(`Resend error ${res.status}: ${text}`);
}

serve(async (req) => {
  // 1) verify webhook secret (if set)
  const gotSecret = req.headers.get("x-webhook-secret");
  if (WEBHOOK_SECRET && gotSecret !== WEBHOOK_SECRET) {
    return new Response("Unauthorized", { status: 401 });
  }

  // 2) parse payload
  const payload = await req.json();

  const type = payload?.type;
  const schema = payload?.schema;
  const table = payload?.table;

  // Supabase DB webhooks send: { type, schema, table, record, old_record }
  if (schema !== "public" || table !== "meal_plan" || type !== "INSERT") {
    return new Response("ignored", { status: 200 });
  }

  const record = payload?.record ?? {};
  const subject = `New meal_plan created (id=${record?.id ?? ""})`;
  const html = `
    <h3>New meal_plan created</h3>
    <ul>
      <li><b>id:</b> ${record?.id ?? ""}</li>
      <li><b>user_id:</b> ${record?.user_id ?? ""}</li>
      <li><b>start_date:</b> ${record?.start_date ?? ""}</li>
      <li><b>end_date:</b> ${record?.end_date ?? ""}</li>
      <li><b>created_at:</b> ${record?.created_at ?? ""}</li>
    </ul>
  `;

  await sendEmail(subject, html);
  return new Response("ok", { status: 200 });
});
