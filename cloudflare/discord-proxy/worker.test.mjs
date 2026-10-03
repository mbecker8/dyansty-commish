// Run: node cloudflare/discord-proxy/worker.test.mjs
import assert from "node:assert/strict";
import worker from "./worker.js";

const env = { PROXY_KEY: "k3y" };
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url, init });
  return new Response('{"access_token":"tok"}', { status: 200, headers: { "content-type": "application/json" } });
};
const call = (method, path, headers = {}, body) =>
  worker.fetch(new Request("https://proxy.example" + path, { method, headers, body }), env);

// Only the two sign-in calls are forwarded.
assert.equal((await call("GET", "/guilds/1", { "x-proxy-key": "k3y" })).status, 404);
assert.equal((await call("GET", "/oauth2/token", { "x-proxy-key": "k3y" })).status, 404);
// Without the right key, nothing is forwarded.
assert.equal((await call("GET", "/users/@me")).status, 403);
assert.equal((await call("GET", "/users/@me", { "x-proxy-key": "nope" })).status, 403);
assert.equal((await worker.fetch(new Request("https://p/users/@me", { headers: { "x-proxy-key": "" } }), {})).status, 403);
assert.equal(calls.length, 0);

// The token exchange goes to Discord with its body and Basic auth, and never the proxy key.
const token = await call("POST", "/oauth2/token", {
  "x-proxy-key": "k3y", authorization: "Basic abc", "content-type": "application/x-www-form-urlencoded",
}, "grant_type=authorization_code&code=c");
assert.equal(token.status, 200);
assert.equal(await token.text(), '{"access_token":"tok"}');
assert.equal(calls[0].url, "https://discord.com/api/oauth2/token");
assert.equal(calls[0].init.body, "grant_type=authorization_code&code=c");
assert.equal(calls[0].init.headers.get("authorization"), "Basic abc");
assert.equal(calls[0].init.headers.get("x-proxy-key"), null);

// Discord's status (e.g. a 429) comes back unchanged.
globalThis.fetch = async () => new Response("slow down", { status: 429, headers: { "content-type": "text/html" } });
const me = await call("GET", "/users/@me", { "x-proxy-key": "k3y", authorization: "Bearer tok" });
assert.equal(me.status, 429);

// Commissioner Bot's reads: GET only, digits only, and only the paging parameters reach Discord.
calls.length = 0;
globalThis.fetch = async (url, init) => {
  calls.push({ url, init });
  return new Response("[]", { status: 200, headers: { "content-type": "application/json" } });
};
const bot = { "x-proxy-key": "k3y", authorization: "Bot t0k" };
assert.equal((await call("GET", "/v10/guilds/123/channels", bot)).status, 200);
assert.equal(calls[0].url, "https://discord.com/api/v10/guilds/123/channels");
assert.equal(calls[0].init.headers.get("authorization"), "Bot t0k");
assert.equal((await call("GET", "/v10/channels/45/messages?limit=100&before=9&with=x", bot)).status, 200);
assert.equal(calls[1].url, "https://discord.com/api/v10/channels/45/messages?limit=100&before=9");
assert.equal((await call("POST", "/v10/channels/45/messages", bot, "{}")).status, 404);
assert.equal((await call("GET", "/v10/channels/abc/messages", bot)).status, 404);
assert.equal((await call("GET", "/v10/guilds/123/members", bot)).status, 404);
assert.equal((await call("GET", "/v10/guilds/123/channels")).status, 403);
assert.equal(calls.length, 2);
console.log("worker ok");
