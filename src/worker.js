// Battery Digest - Cloudflare Worker
// Handles: static assets, email subscriptions, daily newsletter cron

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // API routes
    if (url.pathname === "/api/subscribe" && request.method === "POST") {
      return handleSubscribe(request, env);
    }
    if (url.pathname === "/api/unsubscribe") {
      return handleUnsubscribe(request, env);
    }

    // Everything else: serve static assets
    return env.ASSETS.fetch(request);
  },

  // Cron trigger: send daily newsletter at 9 AM
  async scheduled(event, env, ctx) {
    ctx.waitUntil(sendDailyNewsletter(env));
  },
};

// --- Subscribe ---
async function handleSubscribe(request, env) {
  const headers = { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" };

  try {
    const body = await request.json();
    const email = (body.email || "").trim().toLowerCase();

    if (!email || !email.includes("@") || !email.includes(".")) {
      return new Response(JSON.stringify({ error: "Invalid email address" }), { status: 400, headers });
    }

    // Store in KV: key = email, value = timestamp
    await env.SUBSCRIBERS.put(email, JSON.stringify({
      subscribed_at: new Date().toISOString(),
      active: true,
    }));

    return new Response(JSON.stringify({ success: true, message: "Subscribed successfully!" }), { status: 200, headers });
  } catch (e) {
    return new Response(JSON.stringify({ error: "Bad request" }), { status: 400, headers });
  }
}

// --- Unsubscribe ---
async function handleUnsubscribe(request, env) {
  const url = new URL(request.url);
  const email = (url.searchParams.get("email") || "").trim().toLowerCase();
  const headers = { "Content-Type": "text/html" };

  if (!email) {
    return new Response("<h1>Invalid unsubscribe link</h1>", { status: 400, headers });
  }

  await env.SUBSCRIBERS.delete(email);

  return new Response(`
    <!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Unsubscribed</title>
    <style>body{font-family:sans-serif;max-width:500px;margin:100px auto;text-align:center;color:#333;}</style>
    </head><body>
    <h1>Unsubscribed</h1>
    <p>You have been removed from Battery Digest. You will no longer receive daily emails.</p>
    <p><a href="/">Back to Battery Digest</a></p>
    </body></html>
  `, { status: 200, headers });
}

// --- Daily Newsletter ---
async function sendDailyNewsletter(env) {
  const resendKey = env.RESEND_API_KEY;
  if (!resendKey) {
    console.error("RESEND_API_KEY not set");
    return;
  }

  // Get today's date
  const today = new Date().toISOString().split("T")[0];

  // Fetch the digest page to extract stories
  const digestUrl = `https://battery-digest.yubinxing.workers.dev/digest/${today}-battery-news.html`;
  const homepageUrl = "https://battery-digest.yubinxing.workers.dev";

  // Fetch the homepage to get today's stories from the HTML
  let stories = [];
  try {
    const resp = await fetch(`${homepageUrl}/`);
    const html = await resp.text();

    // Extract stories from the first digest entry (today's)
    const storyRegex = /<li data-num="(\d+)">\s*<a href="[^"]*">([^<]+)<\/a>/g;
    let match;
    let count = 0;
    while ((match = storyRegex.exec(html)) !== null && count < 3) {
      stories.push({ num: match[1], title: match[2].trim() });
      count++;
    }
  } catch (e) {
    console.error("Failed to fetch homepage:", e);
    return;
  }

  if (stories.length === 0) {
    console.log("No stories found for today, skipping email");
    return;
  }

  // Build email HTML
  const emailHtml = buildEmailHtml(today, stories, digestUrl, homepageUrl);

  // Get all subscribers
  const subscribers = [];
  let cursor = null;
  do {
    const list = await env.SUBSCRIBERS.list({ cursor, limit: 1000 });
    for (const key of list.keys) {
      subscribers.push(key.name);
    }
    cursor = list.list_complete ? null : list.cursor;
  } while (cursor);

  console.log(`Sending newsletter to ${subscribers.length} subscribers`);

  // Send to each subscriber (Resend free tier: 100/day)
  for (const email of subscribers) {
    try {
      await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${resendKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          from: "Battery Digest <digest@battery-digest.yubinxing.workers.dev>",
          to: email,
          subject: `Battery Digest - ${formatDate(today)}`,
          html: emailHtml.replace("{{UNSUB_EMAIL}}", encodeURIComponent(email)),
        }),
      });
    } catch (e) {
      console.error(`Failed to send to ${email}:`, e);
    }
  }
}

function formatDate(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
}

function buildEmailHtml(date, stories, digestUrl, homepageUrl) {
  const storyItems = stories.map((s, i) => `
    <tr>
      <td style="padding:12px 20px;border-bottom:1px solid #eee;">
        <span style="color:#999;font-size:14px;margin-right:10px;">${String(i + 1).padStart(2, "0")}</span>
        <span style="font-size:16px;font-weight:500;color:#1a1a1a;">${s.title}</span>
      </td>
    </tr>
  `).join("");

  return `
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f0f0;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0f0;padding:40px 20px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:10px;overflow:hidden;">
        <!-- Header -->
        <tr>
          <td style="padding:30px 30px 20px;border-bottom:1px solid #eee;">
            <h1 style="margin:0;font-size:20px;color:#1a1a1a;">Battery Digest</h1>
            <p style="margin:5px 0 0;color:#888;font-size:14px;">${formatDate(date)}</p>
          </td>
        </tr>
        <!-- Stories -->
        <tr>
          <td style="padding:10px 10px;">
            <table width="100%" cellpadding="0" cellspacing="0">
              ${storyItems}
            </table>
          </td>
        </tr>
        <!-- CTA -->
        <tr>
          <td style="padding:20px 30px;text-align:center;">
            <a href="${digestUrl}" style="display:inline-block;background:#2563eb;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:500;font-size:15px;">Read Full Digest</a>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:20px 30px;border-top:1px solid #eee;text-align:center;color:#999;font-size:12px;">
            <p style="margin:0;">Battery tech intelligence. 3 stories. 5 minutes.</p>
            <p style="margin:8px 0 0;">
              <a href="${homepageUrl}/api/unsubscribe?email={{UNSUB_EMAIL}}" style="color:#999;text-decoration:underline;">Unsubscribe</a>
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>`;
}
