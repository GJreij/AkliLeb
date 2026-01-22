import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY")!;
const ADMIN_EMAIL = Deno.env.get("ADMIN_EMAIL")!;
const WEBHOOK_SECRET = Deno.env.get("WEBHOOK_SECRET"); // optional but recommended

function pickTrueFlag(record: any) {
  if (record?.self_built_diet) return "self_built_diet";
  if (record?.non_akli_partner) return "non_akli_partner";
  if (record?.akli_partner) return "akli_partner";
  return "none";
}

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
  console.log("Resend response status:", res.status);
  console.log("Resend response body:", text);

  if (!res.ok) {
    throw new Error(`Resend error ${res.status}: ${text}`);
  }
}



serve(async (req) => {
  console.log("WEBHOOK HIT");

  const gotSecret = req.headers.get("x-webhook-secret");
  console.log("headers x-webhook-secret:", gotSecret);

  if (WEBHOOK_SECRET && gotSecret !== WEBHOOK_SECRET) {
    console.log("Webhook secret mismatch");
    return new Response("Unauthorized", { status: 401 });
  }

  const payload = await req.json();
  console.log("headers x-webhook-secret:", req.headers.get("x-webhook-secret"));

  console.log("payload.type:", payload?.type);
  console.log("payload.schema:", payload?.schema);
  console.log("payload.table:", payload?.table);

  console.log("record.onboarding:", payload?.record?.onboarding);
  console.log("old_record.onboarding:", payload?.old_record?.onboarding);

  // Supabase Database Webhooks payload usually contains:
  // { type: 'INSERT'|'UPDATE'|'DELETE', schema, table, record, old_record }
  const type = payload?.type;
  const schema = payload?.schema;
  const table = payload?.table;

  if (schema !== "public" || table !== "user") {
    return new Response("ignored", { status: 200 });
  }

  const record = payload?.record;
  const oldRecord = payload?.old_record;

  // 1) New user inserted
  if (type === "INSERT") {
    const subject = `New user signup: ${record?.email ?? record?.id ?? ""}`;
    const html = `
      <h3>New user signed up</h3>
      <ul>
        <li><b>Name:</b> ${record?.name ?? ""} ${record?.last_name ?? ""}</li>
        <li><b>Email:</b> ${record?.email ?? ""}</li>
        <li><b>Phone:</b> ${record?.phone_number ?? ""}</li>
        <li><b>Created at:</b> ${record?.created_at ?? ""}</li>
      </ul>
    `;
    console.log("About to send email, subject:", subject);

    await sendEmail(subject, html);
    return new Response("ok", { status: 200 });
  }

  // 2) onboarding changed to true (false/null -> true)
  if (type === "UPDATE") {
    const newOnboarding = record?.onboarding === true;
    const oldOnboarding = oldRecord?.onboarding === true;

    if (newOnboarding && !oldOnboarding) {
      const mode = pickTrueFlag(record);

      const subject =
        `Onboarding completed: ${(record?.name ?? "")} ${(record?.last_name ?? "")}`.trim();

      const html = `
        <h3>Client completed onboarding</h3>
        <ul>
          <li><b>Name:</b> ${record?.name ?? ""} ${record?.last_name ?? ""}</li>
          <li><b>Phone:</b> ${record?.phone_number ?? ""}</li>
          <li><b>Email:</b> ${record?.email ?? ""}</li>
          <li><b>Delivery address:</b> ${record?.delivery_address ?? ""}</li>
          <li><b>Mode:</b> ${mode}</li>
        </ul>
      `;
      console.log("About to send email, subject:", subject);

      await sendEmail(subject, html);
    }

    return new Response("ok", { status: 200 });
  }

  return new Response("ignored", { status: 200 });
});
