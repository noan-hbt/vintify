# Vintify — Discord bot for Vinted alerts

A full Discord bot to **automate listing searches** and get **real-time alerts** (new listings,
price drops, sales).

It is built on [`vinted_scraper.py`](../vinted_scraper.py) (at the repository root) and uses
**only its public endpoints**: no Vinted session, no token, **no offers or purchases**.

> ⚠️ The Vinted API is private and undocumented. Use this bot in moderation (intervals ≥ 30 s)
> and in line with Vinted's terms of service.

> The bot's interface (slash commands and messages) is in French.

---

## Features

| Command | Description |
|---|---|
| `/veille ajouter <requete>` | Watches a search (text **or a Vinted URL**): every **new listing** is posted with photo, price, size, seller and link. Options: `prix_min`, `prix_max`, `marque` (autocomplete), `intervalle`, `canal`, `mention` (`@here`, `everyone` or a role). |
| `/veille liste` | All watches and tracked items (state, frequency, target, alerts sent, errors). |
| `/veille retirer <id>` | Deletes a watch. |
| `/veille pause <id>` / `/veille reprendre <id>` | Pauses / resumes a watch. |
| `/suivi ajouter <url>` | Tracks **one specific item**: alerts on a **price drop**, a **sale/removal**, or when it is back in stock. |
| `/suivi retirer <id>` | Stops tracking an item. |
| `/recherche` | One-off search (recent results, with photo and link). |
| `/stats` | Uptime, active watches, searches, alerts sent, errors. |
| `/aide` | Command summary. |

Details:

- **No Vinted session token**: automatic anonymous bootstrap (cookies + CSRF handled by
  `vinted_scraper`), with built-in retries and backoff.
- **Persistent deduplication**: each listing is posted only once, even after a restart (ID
  history in `state.json`).
- **Immediate first scan** when a watch is created (catch-up), then only new listings.
- **Flexible alert target**: chosen channel, current channel or DM; optional mention (`@here`,
  `@everyone`, role name).
- **Item tracking**: price compared on every cycle, one alert per event, automatically disabled
  if the listing disappears.
- **Atomic JSON persistence** (temp file + `os.replace`): safe against a hard stop.
- **Test modes without Discord**: `--search` and `--selftest`.

## Layout

```text
discord_bot/
├── bot.py                 # entry point (Discord + test CLI)
├── vintify_bot/
│   ├── config.py          # configuration from environment variables
│   ├── storage.py         # JSON persistence (watches, tracked items, stats)
│   ├── vinted.py          # async Vinted service (to_thread + lock)
│   ├── embeds.py          # builds the (French) embeds
│   ├── poller.py          # monitoring engine (watches + tracked items)
│   └── cogs.py            # slash commands
├── requirements.txt
├── Procfile               # worker: python bot.py
└── .env.example
```

---

## Quick start (local)

```bash
# from the repository root
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on Linux/macOS)
pip install -r discord_bot\requirements.txt

cd discord_bot
copy .env.example .env            # then fill in DISCORD_TOKEN
python bot.py
```

Without a token (quick scraping test):

```bash
python bot.py --search "nike tn" --price-to 60
python bot.py --selftest
```

## Creating the Discord bot

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab → **Reset Token** → copy the token (`DISCORD_TOKEN` variable).
3. **OAuth2 → URL Generator** tab:
   - scopes **`bot`** + **`applications.commands`**;
   - permissions: **View Channels**, **Send Messages**, **Embed Links** (and **Mention
     Everyone** if you use `@here`/`@everyone`);
   - open the generated URL to invite the bot to your server.
4. Start the bot: the slash commands appear once they are synced (a few seconds after startup).

---

## Deploying on Railway

1. **Push the repository to GitHub** (the bot lives in `discord_bot/`; the repository root
   holds `vinted_scraper.py` and `railway.json`).
2. In Railway: **New Project → Deploy from GitHub repo** → pick the repository. `railway.json`
   is detected automatically:
   - `rootDirectory: discord_bot` → Nixpacks Python build;
   - `startCommand: python bot.py` (same as the `Procfile`).
3. **Environment variables** (Railway → Variables):
   - `DISCORD_TOKEN`: the bot token (required);
   - `VINTED_DOMAIN`: `fr` by default (optional);
   - `LOG_LEVEL`, `VEILLE_INTERVALLE_DEFAUT`, `VEILLE_PAGE_SIZE`, … (optional, see
     `.env.example`).
4. **Persistent volume** (recommended): Railway → **Volumes** tab → create a volume mounted on
   **`/data`**, then set **`DATA_DIR=/data`**. Without a volume, watches and listing history are
   lost on every redeploy.
5. **Deploy.** Check that the logs show: `Commandes slash synchronisées : N`.

Redeploys and crashes: Railway restarts the worker automatically
(`restartPolicyType: ON_FAILURE`).

### Railway notes

- The `Procfile` is a safety net: if you delete it, the `startCommand` in `railway.json` takes
  over.
- The free plan is enough for personal use (a handful of watches).
- If you deploy **only** the `discord_bot/` folder (without the root), copy `vinted_scraper.py`
  into it: by default `bot.py` imports it from the repository root.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | — | Discord bot token (**required**) |
| `VINTED_DOMAIN` | `fr` | Vinted domain (`fr`, `be`, `de`, `es`, `it`, `pl`, `co.uk`, `com`, …) |
| `DATA_DIR` | `data` | Persistence directory (Railway volume → `/data`) |
| `VEILLE_INTERVALLE_DEFAUT` | `60` | Default watch interval (s, min 30) |
| `SUIVI_INTERVALLE_DEFAUT` | `300` | Default item tracking interval (s, min 30) |
| `VEILLE_PAGE_SIZE` | `24` | Listings read per cycle (max 96) |
| `VEILLE_HISTORIQUE_MAX` | `1500` | Seen IDs kept per watch |
| `RECHERCHE_MAX_AFFICHEE` | `5` | Max results shown by `/recherche` |
| `VEILLE_SCAN_INITIAL` | `1` | Immediate scan when a watch is created |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Limitations and good practice

- The Vinted API may change or block depending on the IP address: the bot retries
  automatically (retries + backoff, CSRF refresh).
- Do not create dozens of watches at 30 s: stay reasonable (interval ≥ 60 s recommended,
  moderate total request count).
- Displayed prices come from the API: purchase fees may vary.
- Item tracking detects price changes between two cycles: a very short-lived drop can go
  unnoticed depending on the interval.
- **Brands**: Vinted no longer allows searching a brand by name without a session. Only
  popular brands (Nike, adidas, H&M, Levi's…) can be resolved by name; for the others, apply
  the brand filter on vinted.fr and paste the resulting URL (it contains `brand_ids[]=…`).
- **Item tracking**: if the detail API is blocked, price and availability are read from the
  item's public page (JSON-LD) — same values, but size and seller are then unavailable.
- **Testing without the bot**: `python bot.py --search "…"` (live search) and
  `python bot.py --selftest` (env + persistence + search). `python _integration_test.py` (in
  `discord_bot/`) checks the polling engine (deduplication, price drop, listing not found)
  against the real network.
