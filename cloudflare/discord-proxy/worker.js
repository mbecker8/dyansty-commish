// Dynasty Commish: Discord API proxy (Cloudflare Worker).
//
// Discord's Cloudflare blocks Render's shared outbound IPs (HTTP 429), so the app's calls to Discord
// go through this Worker instead: the two sign-in calls, and Commissioner Bot's two read-only calls
// for the league's channels (league/discord_client.py). It forwards nothing else, and only for
// requests carrying the shared key, so it can't be used as an open proxy.
//
// Setup: docs/discord-signin.md. Secret: PROXY_KEY (the same value as DISCORD_PROXY_KEY on Render).

const DISCORD_API = "https://discord.com/api";
const ALLOWED = new Set(["POST /oauth2/token", "GET /users/@me"]);
// Commissioner Bot's reads: a server's channel list and a channel's messages. GET only.
const BOT_ROUTES = [/^\/v10\/guilds\/\d+\/channels$/, /^\/v10\/channels\/\d+\/messages$/];
const PAGING = ["limit", "before", "after"];
const FORWARDED_HEADERS = ["authorization", "content-type", "accept"];

function sameKey(given, expected) {
  // Constant-time comparison, so the key can't be guessed a character at a time.
  if (!expected || given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

function allowed(method, path) {
  return ALLOWED.has(`${method} ${path}`) || (method === "GET" && BOT_ROUTES.some((r) => r.test(path)));
}

function pagingQuery(url) {
  // Only the paging parameters reach Discord.
  const params = new URLSearchParams();
  for (const name of PAGING) if (url.searchParams.has(name)) params.set(name, url.searchParams.get(name));
  const query = params.toString();
  return query ? "?" + query : "";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!allowed(request.method, url.pathname)) {
      return new Response("Not found", { status: 404 });
    }
    if (!sameKey(request.headers.get("x-proxy-key") || "", env.PROXY_KEY || "")) {
      return new Response("Forbidden", { status: 403 });
    }
    const headers = new Headers({ "user-agent": "DiscordBot (https://dynasty-commish.onrender.com, 1.0)" });
    for (const name of FORWARDED_HEADERS) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    const upstream = await fetch(DISCORD_API + url.pathname + pagingQuery(url), {
      method: request.method,
      headers,
      body: request.method === "POST" ? await request.text() : undefined,
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
    });
  },
};
