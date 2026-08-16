# Hopwatch

Finds and books Wizz Air itineraries on **Wizz Multipass** — including
self-transfer routings the airline will never sell you as one ticket, like
`OTP → WAW → EIN`.

Multipass lets you book any available seat for a flat €10, but only from 72
hours before departure. So the interesting question isn't "what's cheap", it's
**"what is about to become bookable, and when exactly"** — and then being awake
at that moment. This is the thing that's awake.

## What it does

```
┌── layer 1 ── route graph ────────── Wizz's whole network, 188 stations
│   layer 2 ── schedule feasibility ─ which paths fly, when they unlock
│   layer 3 ── Multipass availability ─ does a €10 seat actually exist (logged in)
└── layer 4 ── watch → alert → book ─ Discord push, one-tap approve
```

Layers 1–2 use Wizz's public endpoints and need no account. Layers 3–4 drive a
logged-in browser, which you seed by hand once.

## Quick start

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[service,dev]'
.venv/bin/playwright install chromium
```

Try it without any of the booking machinery first:

```bash
.venv/bin/hopwatch search Bucharest Eindhoven --from today --to +6d
```

Then set up the service:

```bash
.venv/bin/hopwatch init-config
.venv/bin/hopwatch login
.venv/bin/hopwatch serve
```

`login` opens a real browser and waits while **you** sign in. Nothing in this
program reads, stores, or types your password. The session is saved to a
profile directory and reused headlessly afterwards.

The web UI is at <http://127.0.0.1:8765>.

## The interface

A local web UI (FastAPI + a no-build frontend) and a Discord bot, sharing one
process:

- **Watches** — standing "get me from A to B between these dates" requests.
- **Candidates** — every itinerary found, sorted by what unlocks next, with
  live countdowns and the staggered-window warning.
- **Waiting for you** — bookings prepared up to the confirm button, with a
  screenshot of exactly what's about to be bought.
- **Activity** — live event feed over SSE.

Discord gets the same alerts as push, with **Approve & book / Skip** buttons so
you can decide from your phone at 04:00. Slash commands: `/status`, `/wants`,
`/upcoming`. Setup is in [docs/discord-setup.md](docs/discord-setup.md).

Only Discord user IDs listed in `approver_ids` may press Approve — anyone who
can see the channel can see the button, so the list fails closed: empty means
nobody, not everybody.

## How booking works

Nothing is ever bought without you saying so, in that moment:

1. A candidate's window opens; an authenticated check finds a Multipass seat.
2. The bot drives the booking to the final confirm button and **stops**.
3. It screenshots that page and pings you on Discord and in the UI.
4. You tap Approve. Only then is confirm clicked.
5. If nobody answers within `approval_hold_min` (default 12 minutes), the hold
   lapses and the booking is abandoned.

An approval that arrives after the hold lapsed is **refused**, not honoured
late — the UI returns 409 and Discord says "too late". A stale tap must never
book a flight.

Two hard rules in the automation:

- **It never types payment credentials.** If the flow reaches a card number,
  CVC, or a payment iframe, it aborts and hands the session to you. Multipass
  shouldn't ask — and if it does, a human should be looking at the screen.
- **A multi-leg trip is fully prepared before any leg is confirmed**, so your
  approval covers the whole trip rather than committing you to leg 1 with leg 2
  still unknown.

Spending caps live in config: `max_open_bookings`, `max_bookings_per_day`.

## The staggered window problem

This is the thing the tool exists to get right.

Each leg is its **own** Multipass booking with its **own** 72-hour window. For a
connection departing the next morning, leg 2 unlocks up to a day after leg 1.
Buy leg 1 the moment it opens and you're holding a ticket to a connecting
airport with no guarantee of the onward seat.

So an itinerary is only safely committable at `max(departure − 72h)` across all
legs, not `min`. That's what the reported window opens at, and the gap is
flagged everywhere it's shown:

```
⚠ leg 1 unlocks 8h18 before the last leg does — its seats can sell out
  while you wait. Do not book leg 1 early.
```

A 1-stop trip also costs **two** trip credits and €20, and is a self-transfer:
separate bookings, no rebooking protection if leg 1 runs late.

## What layer 3 knows, and what it's guessing

Availability checking drives the logged-in booking page and reads the
`/Api/search/search` response the app fetches for itself, rather than scraping
fare cards. That's far more durable, and it yields real arrival times — which
the public timetable omits.

**The one genuinely unverified part** is the exact shape of a Multipass fare in
that response. I don't have a pass, so `MULTIPASS_MARKERS` in `multipass.py` is
an informed guess matched broadly across fare fields. Every check stores its raw
fare JSON, so the first real check tells you the truth:

```bash
.venv/bin/hopwatch probe --origin OTP --destination EIN --date 2026-09-20
```

That dumps the live API responses, selector hit counts, page HTML and a
screenshot. Correct `selectors.py` and `MULTIPASS_MARKERS` from what you see.
All site-specific strings are quarantined in `selectors.py` for exactly this
reason — when Wizz redesigns, that's the only file that should need editing.

The detection deliberately errs toward over-matching: a false positive surfaces
a booking for you to reject, a false negative silently loses the window.

## Staying logged in

The bot never stores or types your Wizz password. `hopwatch login` opens a
browser, you sign in yourself, and the session is reused after that. **Tick
"Remember me" when you log in** — without it Wizz issues a short-lived session
cookie and you'll be signing in constantly.

Three things keep that session alive:

- **Keepalive.** The watcher can go hours between authenticated checks, and an
  idle Wizz session expires in between — leaving it dead at the exact moment a
  booking window opens. A light touch every `keepalive_min` (default 20)
  prevents that.
- **Cookie snapshots.** Chromium only flushes cookies to the profile on a clean
  close, so a crash or `kill -9` loses a session that never actually expired.
  Cookies are snapshotted to `session-cookies.json` (mode 600 — a live session
  is a credential) and restored automatically if the profile comes up empty.
- **One loud alert.** When the session really has expired, you get one Discord
  message with the command to fix it, not one every twenty minutes.

If you're still being logged out constantly, check "Remember me" first, then
confirm the profile directory is writable and surviving restarts — a profile on
a tmpfs or inside a container without a volume loses everything on exit.

## Running it always-on

`deploy/` has a systemd unit and a Docker setup. The Playwright base image is
worth using — Chromium's system libraries are the fiddly part on a Pi.

```bash
sudo cp deploy/hopwatch.service /etc/systemd/system/
sudo systemctl enable --now hopwatch
```

Two things to be careful with:

- **The browser profile is a credential.** It holds a logged-in session that can
  spend money. `StateDirectoryMode=0700`, and it's gitignored.
- **The web UI has no authentication** and binds to localhost. The approve
  endpoint spends money — put a reverse proxy with auth in front before exposing
  it anywhere.

Keep the Discord token in the environment (`HOPWATCH_DISCORD_TOKEN`), not
in the config file.

## CLI

| Command | What it does |
|---|---|
| `search FROM TO` | Dated, timing-feasible itineraries |
| `routes FROM TO` | Route options, ignoring dates |
| `places QUERY` | Resolve a code, metro code, city or country |
| `login` | Open a browser and sign in yourself |
| `probe` | Dump what the live site serves, to calibrate selectors |
| `serve` | Run the watcher, web UI and Discord bot |
| `status` | Summarise stored state without starting anything |
| `init-config` | Write an example config file |
| `refresh` | Force-refresh cached API version and network |

Search flags: `--min-layover`, `--max-stops`, `--max-detour`, `--max-trip-hours`,
`--after-hour` / `--before-hour`, `--allow-ground-transfer`,
`--sort window|departure|duration`, `--bookable-now`, `--json`, `--offline`.

## Notes on the Wizz backend

Findings that cost some time to work out:

- **The API version rotates.** Endpoints live at `be.wizzair.com/{version}/Api/…`
  and `{version}` changes every few weeks. It's scraped from the homepage and
  cached; a 404 forces a re-scrape.
- **There's an anti-forgery handshake.** The first response sets a
  `RequestVerificationToken` cookie; every later request must echo it in an
  `X-RequestVerificationToken` header or you get
  `400 {"handlerError":"InvalidProtocol"}`. One-shot `curl` never sees this;
  anything with a cookie jar breaks after exactly one request.
- **`timetable` takes one route per call.** A second `flightList` entry is read
  as the *return* leg, not a second route.
- **Metro codes leak through.** A query for `OTP` can return `BBU` results. The
  station in the *response* is the real one. Same for `WSW` (WAW/WMI), `LON`,
  `PAR`, `MIL`, `ROM`, `STO`.
- **`priceType: "checkPrice"` means `amount: 0`**, a placeholder, not a free
  flight. Fall back to `originalPrice`.
- **`search/search` is gated** — 429 to anything without a browser session.
  That's why layer 3 needs Playwright rather than more HTTP.

## Failure honesty

The recurring principle: *"I could not check this"* and *"there is nothing
here"* must never look alike to something that decides whether to spend money.

- A leg whose timetable can't be fetched produces `!! INCOMPLETE`, not an empty
  result.
- A search response with no flight-list key is an error to retry; one with an
  empty list is a real "nothing on sale".
- A partially-confirmed multi-leg booking is reported as **PARTIAL BOOKING**
  with an explicit "you may be stranded, act now", never as a plain failure.
- Availability that can't be classified is `unknown`, not `sold_out`.

## Politeness

Unauthenticated requests are spaced 1.5s apart, capped at 3 concurrent, cached
on disk. Authenticated checks are the scarce resource and run under a hard
budget (`max_checks_per_hour`, default 30, with a minimum gap), spent only on
candidates whose window is actually open.

Automated access is against Wizz's terms of service. The account attached to
your Multipass is what's at risk, so the defaults are deliberately slow.

## Tests

```bash
.venv/bin/pytest
```

124 tests, no network and no browser — the route map is a synthetic fixture, the
HTTP layer uses a mock transport, and the parts that need a real Multipass
account are tested either side of the browser boundary.
