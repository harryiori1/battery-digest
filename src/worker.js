// Battery Digest - Cloudflare Worker
// Handles: static assets, email subscriptions, daily newsletter

// KV key (same namespace as subscribers) remembering the last digest mailed.
// Keys starting with "_" are internal state, never treated as subscribers.
const LAST_SENT_KEY = "_meta:newsletter_last_sent";

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
    // Manual trigger. Requires the NEWSLETTER_TOKEN secret (wrangler secret put).
    // ?force=1 bypasses the already-sent / freshness checks and re-mails the
    // digest currently on the home page to everyone.
    if (url.pathname === "/api/send-newsletter" && request.method === "POST") {
      const auth = request.headers.get("Authorization") || "";
      if (!env.NEWSLETTER_TOKEN || auth !== `Bearer ${env.NEWSLETTER_TOKEN}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      try {
        const result = await sendDailyNewsletter(env, { force: url.searchParams.get("force") === "1" });
        return new Response(JSON.stringify(result), {
          headers: { "Content-Type": "application/json" },
        });
      } catch (e) {
        return new Response(JSON.stringify({ error: e.message, stack: e.stack }), {
          status: 500, headers: { "Content-Type": "application/json" },
        });
      }
    }

    // Everything else: serve static assets
    return env.ASSETS.fetch(request);
  },

  // Cron trigger (hourly, see wrangler.jsonc). Mails each new digest exactly
  // once, within an hour of the deploy that contains it going live. A fixed
  // send time did not work: the GitHub Actions schedule that builds the site
  // is routinely hours late, so the mail kept going out with the old digest.
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
async function sendDailyNewsletter(env, { force = false } = {}) {
  const log = [];
  const resendKey = env.RESEND_API_KEY;
  if (!resendKey) {
    return { error: "RESEND_API_KEY not set" };
  }

  const homepageUrl = "https://battery-digest.yubinxing.workers.dev";

  // Fetch the homepage and extract stories from ALL digest entries
  // Then pick the most recent one (first on the page)
  let stories = [];
  let digestLink = "";
  try {
    const resp = await env.ASSETS.fetch(new Request("http://placeholder/index.html"));
    const html = await resp.text();

    log.push(`Fetched index.html: ${html.length} chars`);

    // Extract the first digest link
    const linkMatch = html.match(/href="(\/digest\/[^"]+)"/);
    if (linkMatch) {
      digestLink = homepageUrl + linkMatch[1];
    }
    log.push(`Digest link: ${digestLink}`);

    // Extract stories - match across whitespace/newlines
    const storyRegex = /data-num="(\d+)"[\s\S]*?<a[^>]*>([^<]+)<\/a>/g;
    let match;
    while ((match = storyRegex.exec(html)) !== null && stories.length < 3) {
      stories.push({ num: match[1], title: match[2].trim() });
    }
    log.push(`Found ${stories.length} stories`);
  } catch (e) {
    return { error: "Failed to fetch homepage", detail: e.message, log };
  }

  if (stories.length === 0) {
    return { error: "No stories found", log };
  }

  // The slug starts with the digest date: /digest/YYYY-MM-DD-...
  const dateMatch = digestLink.match(/\/digest\/(\d{4}-\d{2}-\d{2})-/);
  if (!dateMatch) {
    return { error: "Could not read digest date from link", digestLink, log };
  }
  const digestDate = dateMatch[1];

  if (!force) {
    const lastSent = await env.SUBSCRIBERS.get(LAST_SENT_KEY);
    if (lastSent === digestLink) {
      return { skipped: "already sent", digestLink, log };
    }
    // Never mail an old digest (a no-news day leaves the previous one on top).
    const todayUtc = new Date().toISOString().split("T")[0];
    const ageDays = Math.round((Date.parse(todayUtc) - Date.parse(digestDate)) / 86400000);
    if (ageDays > 1) {
      return { skipped: `digest ${digestDate} is ${ageDays} days old`, digestLink, log };
    }
  }

  // Build email HTML, dated by the digest rather than the send time
  const emailHtml = buildEmailHtml(digestDate, stories, digestLink, homepageUrl);

  // Get all subscribers
  const subscribers = [];
  let cursor = null;
  do {
    const list = await env.SUBSCRIBERS.list({ cursor, limit: 1000 });
    for (const key of list.keys) {
      if (key.name.startsWith("_")) continue; // internal state, not a subscriber
      subscribers.push(key.name);
    }
    cursor = list.list_complete ? null : list.cursor;
  } while (cursor);

  log.push(`Subscribers: ${subscribers.length} - ${subscribers.join(", ")}`);

  // Mark as sent before mailing so a partial failure can never cause a second blast.
  await env.SUBSCRIBERS.put(LAST_SENT_KEY, digestLink);

  // Send to each subscriber (Resend free tier: 100/day)
  const results = [];
  for (const email of subscribers) {
    try {
      const resp = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${resendKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          from: "Battery Digest <digest@batterydigest.org>",
          to: email,
          subject: `Battery Digest - ${formatDate(digestDate)}`,
          html: emailHtml.replace("{{UNSUB_EMAIL}}", encodeURIComponent(email)),
        }),
      });
      const resendResult = await resp.json();
      results.push({ email, status: resp.status, result: resendResult });
    } catch (e) {
      results.push({ email, error: e.message });
    }
  }

  return { success: true, digestDate, digestLink, log, stories, results };
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
