# Discord sign-in: setup and troubleshooting

Everyone signs in with Discord. The app asks only for the `identify` scope, so all it learns is
each person's Discord ID and username. This page covers the whole production setup: the Discord
app, the Cloudflare Worker proxy and the Render settings. Local development only needs step 1.

## How it works

1. The browser goes to Discord's authorize page. The user approves, and Discord sends them back to
   `https://<site>/auth/discord/callback` with a one-time code.
2. The server makes **two calls to Discord's API**: it swaps the code for a token
   (`POST /oauth2/token`), then reads who the user is (`GET /users/@me`). The token isn't kept.
3. The app lets the person in if their Discord ID is on a Manager or listed in
   `COMMISSIONER_DISCORD_IDS`.

### Why production needs a proxy

Discord's API sits behind Cloudflare, which blocks IP addresses that send too many requests.
Render's services share **outbound IP addresses** with many other apps, some of them busy Discord
bots, so Cloudflare sometimes blocks the whole IP. Sign-in then fails with **HTTP 429** and a
Cloudflare HTML page. It isn't caused by anything our app did: a sign-in makes only two calls.
Upgrading the Render plan doesn't help, because paid instances share outbound IPs too.

So in production the two server calls in step 2 go through a small **Cloudflare Worker**
(`cloudflare/discord-proxy/worker.js`). It runs on Cloudflare's network, so its requests don't come
from Render's shared IP. The Worker:
- forwards only `POST /oauth2/token` and `GET /users/@me`, and returns 404 for anything else;
- requires the shared key in an `X-Proxy-Key` header, so nobody else can use it as a proxy;
- passes Discord's answer back unchanged, including its status code.

The browser's own visit to Discord in step 1 never goes through the proxy.

## 1. The Discord application

One application serves both local development and production.

1. Go to https://discord.com/developers/applications and open the app. Its client ID is the one in
   `secrets/discord.env`.
2. Under **OAuth2 → Redirects**, keep both of these:
   - `http://127.0.0.1:8000/auth/discord/callback` (local)
   - `https://dynasty-commish.onrender.com/auth/discord/callback` (production)

   They must match exactly: `https` for production, and no trailing slash.
3. **Save Changes.** Don't click **Reset Secret** unless you mean to. Resetting it breaks every
   place that uses the old secret: `secrets/discord.env` and Render.

Locally, `bin/local-review.sh` reads `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` from
`secrets/discord.env` (gitignored). Local sign-in calls discord.com directly, with no proxy.

## 2. The Cloudflare Worker (production only)

You need a free Cloudflare account: no domain, no credit card. The free Workers plan allows
100,000 requests a day, and sign-in uses two per person per sign-in.

### 2a. Make a shared key

On your laptop:

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep that value handy. It goes into both Cloudflare (`PROXY_KEY`) and Render (`DISCORD_PROXY_KEY`).
Treat it like a password.

### 2b. Create the account

1. Sign up at https://dash.cloudflare.com/sign-up with your email and a password.
2. Verify your email address from the message Cloudflare sends you.
3. If it asks what you want to do or offers to add a website, skip it. You don't need a domain.

### 2c. Create the Worker

The Cloudflare dashboard moves things around now and then, so the labels may differ slightly.

1. In the left sidebar, open **Compute (Workers) → Workers & Pages**, then click **Create**.
2. Choose **Create Worker** (the "Hello World" starter).
3. Name it **`dynasty-commish-discord`**. The first time, Cloudflare also asks you to pick your
   `workers.dev` subdomain; any name works.
4. Click **Deploy**. That deploys the placeholder; the real code comes next.
5. Click **Edit code**. Delete everything in `worker.js`, paste in the whole of
   `cloudflare/discord-proxy/worker.js` from this repo, then click **Deploy**.
6. Go back to the Worker, open **Settings → Variables and Secrets**, and click **Add**:
   - Type: **Secret**
   - Name: `PROXY_KEY`
   - Value: the key from 2a

   Save or deploy.
7. Copy the Worker's URL from its overview page. It looks like
   `https://dynasty-commish-discord.<your-subdomain>.workers.dev`.

### 2d. Check the Worker

Put the URL and key in place and run:

```sh
# No key: refused by the Worker
curl -s -o /dev/null -w "%{http_code}\n" https://dynasty-commish-discord.<sub>.workers.dev/users/@me
# → 403

# With the key but no Discord token: Discord itself answers
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Proxy-Key: <key>" \
  https://dynasty-commish-discord.<sub>.workers.dev/users/@me
# → 401 (the call reached Discord through the Worker)
```

403 and then 401 means the Worker is working. Getting 403 both times means the key doesn't match
`PROXY_KEY`.

**Command-line alternative:** in `cloudflare/discord-proxy/`, run `npx wrangler login`,
`npx wrangler deploy`, then `npx wrangler secret put PROXY_KEY`. It does the same as the steps above.

## 3. Render settings

In Render, open **dynasty-commish → Environment**. None of these values are committed; `render.yaml`
only declares them.

| Key | Value |
|---|---|
| `DISCORD_CLIENT_ID` | the app's client ID, the same as in `secrets/discord.env` |
| `DISCORD_CLIENT_SECRET` | the app's client secret, the same as in `secrets/discord.env` |
| `DISCORD_API_BASE` | the Worker URL from 2c, e.g. `https://dynasty-commish-discord.<sub>.workers.dev` |
| `DISCORD_PROXY_KEY` | the key from 2a |
| `COMMISSIONER_DISCORD_IDS` | commissioners' Discord IDs, comma-separated (see #44) |

Save. Render redeploys, which takes a few minutes. The app trims stray spaces and line breaks from
the Discord values. Leave `DISCORD_API_BASE` unset and the app calls discord.com directly.

## 4. Test it

1. Open https://dynasty-commish.onrender.com. On the free plan the first load can take about a
   minute while the service wakes up.
2. The login page must **not** say "Discord sign-in is not configured on this server yet".
3. Click **Sign in with Discord** and approve.
4. You should land signed in. If your ID isn't linked yet, you'll see a page showing your Discord ID,
   which also means sign-in itself worked.

## Troubleshooting

Every failed sign-in is logged in **Render → dynasty-commish → Logs** as one line starting
`Discord sign-in failed:`. It shows Discord's status code, the start of its answer, and the
redirect the app used.

| What you see | Cause | Fix |
|---|---|---|
| Discord's page says **Invalid OAuth2 redirect_uri** | The production redirect isn't registered, or doesn't match exactly | Step 1.2 |
| Log shows **(401) invalid_client** | Client ID or secret on Render is wrong | Paste both again from `secrets/discord.env` |
| Log shows **(400) invalid_grant** | The code expired or was used twice, or the redirect doesn't match | Sign in again from a fresh page; check step 1.2 |
| Log shows **(429)** with a Cloudflare HTML page; the user sees "temporarily blocking sign-ins" | Discord's Cloudflare is blocking the server's IP | Set up the Worker (steps 2–3). Without it, wait an hour or so and retry. |
| Log shows **(403)** and the body `Forbidden` | The Worker rejected the key | `DISCORD_PROXY_KEY` on Render must equal the Worker's `PROXY_KEY` |
| Log shows **(404)** and the body `Not found` | `DISCORD_API_BASE` has an extra path | Use the bare Worker URL, with nothing after `.workers.dev` |
| Page says your account **isn't linked** | Sign-in worked, but the ID isn't on a Manager or in `COMMISSIONER_DISCORD_IDS` | Add it in Admin → Managers, or to the env var |

## Changing the key

1. Make a new key (2a).
2. Set it as the Worker's `PROXY_KEY` (2c step 6), then as `DISCORD_PROXY_KEY` on Render.
3. Sign-ins in the minute or so between the two changes fail with 403. Try again once Render has
   redeployed.

## Updating the Worker

When `cloudflare/discord-proxy/worker.js` changes, paste it into the dashboard editor again and click
**Deploy**, or run `npx wrangler deploy`. CI runs `worker.test.mjs` on every push.
