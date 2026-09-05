// Watermark cron worker.
// Pings the Render backend's /ingest endpoint on a schedule. This drives
// data polling AND keeps the Render free-tier instance from cold-starting
// mid-demo (a named risk in BUILD_PLAN.md section 14).

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(pingIngest(env));
  },

  // Lets you trigger it manually by visiting the worker's URL, useful for
  // testing before the cron schedule fires on its own.
  async fetch(request, env, ctx) {
    const result = await pingIngest(env);
    return new Response(JSON.stringify(result), {
      headers: { "content-type": "application/json" },
    });
  },
};

async function pingIngest(env) {
  const url = `${env.RENDER_API_URL}/ingest`;
  try {
    const res = await fetch(url, {
      method: "GET",
      headers: { "User-Agent": "watermark-keep-alive" },
    });
    console.log("Watermark ingest ping:", res.status);
    return { ok: res.ok, status: res.status };
  } catch (err) {
    console.error("Watermark ingest ping failed:", err);
    return { ok: false, error: String(err) };
  }
}
