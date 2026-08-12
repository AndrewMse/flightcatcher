# Discord setup

Ten minutes, once. The payoff is the Approve button: when a Multipass window
opens at 04:00 and the seat is contested, the gap between "found it" and "yes,
book it" is one tap on your phone.

## 1. Make a server for it

Skip if you already have somewhere private to put the bot.

In Discord: **＋** (bottom of the server list) → **Create My Own** → **For me
and my friends**. Call it anything. A private server with only you in it is the
right shape here — the alerts contain your travel plans, and the approve button
spends your money.

## 2. Create the application

Go to <https://discord.com/developers/applications> → **New Application**. Name
it `FlightCatcher`.

In the left sidebar, open **Bot**:

- Click **Reset Token**, then **Copy**. This is the only time it is shown. If
  you lose it, reset it again — it is a password for the bot.
- **Public Bot**: turn this **off**. You don't want anyone else adding it.
- **Privileged Gateway Intents**: leave all three **off**. FlightCatcher uses
  `Intents.default()` and needs none of them. This is the usual thing people
  turn on unnecessarily.

## 3. Invite it to your server

Left sidebar → **OAuth2** → **OAuth2 URL Generator**.

**Scopes** — tick exactly two:
- `bot`
- `applications.commands`  ← without this, the slash commands never appear

**Bot Permissions** — tick exactly four:
- View Channels
- Send Messages
- Embed Links
- Attach Files  ← for the confirm-step screenshot

That comes to permission integer **52224**. You can shortcut the whole page by
pasting this, with your Application ID (from **General Information**) swapped in:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=52224
```

Open it, pick your server, authorise.

## 4. Collect two IDs

Enable **Settings → Advanced → Developer Mode** in the Discord app first.

- **Channel ID** — right-click the channel you want alerts in → **Copy Channel ID**
- **Your user ID** — right-click your own name anywhere → **Copy User ID**
- **Server ID** (optional) — right-click the server icon → **Copy Server ID**

Your user ID is the important one: **only IDs on the approver list can press
Approve.** An empty list means nobody can, and FlightCatcher will say so at
startup. It fails closed on purpose — anyone who can see the channel can see
the button.

## 5. Configure

The token goes in the environment, not the config file:

```bash
export FLIGHTCATCHER_DISCORD_TOKEN='paste-the-token'
```

For a systemd deployment, put it in `/etc/flightcatcher/env` (chmod 600) — the
unit file already reads it.

Then in `~/.config/flightcatcher/config.toml`:

```toml
[discord]
enabled = true
channel_id = 1234567890123456789   # where alerts are posted
approver_ids = [9876543210987654321]  # your user ID — only these may approve
guild_id = 1122334455667788990     # optional: instant slash commands
mention = "<@9876543210987654321>"  # optional: ping you on alerts
```

`guild_id` matters more than it looks: without it, slash commands are
registered globally and can take up to an hour to show up. With it, they appear
immediately.

## 6. Check it

```bash
flightcatcher serve
```

You should see `Connected as FlightCatcher#1234` in the log and in the UI's
activity feed. In Discord, type `/` in your channel — `/status`, `/wants` and
`/upcoming` should be listed. `/status` is the quickest end-to-end test: it
reports whether the Wizz session is alive and how much check budget is left.

## What the alerts look like

**Seat found** — posted the first time a candidate's availability check comes
back positive. Route, duration, time left in the window, cost in euros and trip
credits, and a self-transfer warning if it has a stop.

**Approve this booking?** — posted once the bot has driven the booking to the
final confirm button and stopped there. Carries a screenshot of the exact page
it is about to confirm, the cost, a countdown to when the hold lapses, and two
buttons.

Tapping **Approve & book** confirms. Tapping **Skip** abandons it. Doing
nothing lets the hold expire, which also abandons it — silence is never taken
as consent.

## Troubleshooting

**Slash commands don't appear.** Almost always a missing `applications.commands`
scope — re-invite with the URL above. Otherwise set `guild_id` and restart.

**"You are not on the approver list."** Your user ID isn't in `approver_ids`.
Note that's your *user* ID, not the channel or server ID — they look identical.

**Bot shows online but never posts.** Wrong `channel_id`, or it lacks View
Channel in that specific channel. Check the channel's own permission overrides,
not just the server-wide role.

**"Too late — this booking is no longer waiting."** Working as intended. The
hold expired before you tapped. Raise `watcher.approval_hold_min` if you want
longer, but the seat is genuinely contested during that time.

**Token stopped working.** Anyone with the token can post as your bot. If it
leaked, reset it in the portal — that invalidates the old one immediately.
